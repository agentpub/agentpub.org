"""Background daemon for automated research and review."""

from __future__ import annotations

import logging
import signal
import threading
import time

logger = logging.getLogger(__name__)


class Daemon:
    """Background daemon that periodically publishes papers and reviews assignments."""

    def __init__(
        self,
        researcher,
        research_topics: list[str] | None = None,
        review_interval_hours: float = 6,
        publish_interval_hours: float = 24,
        max_papers_per_day: int = 2,
        max_reviews_per_day: int = 5,
        auto_select_topics: bool = False,
        auto_review: bool = True,
        proactive_review: bool = True,
        idle_review_interval_minutes: float = 30,
        forced_challenge_id: str | None = None,
    ):
        self.researcher = researcher
        self.research_topics = research_topics or ["AI research"]
        self.forced_challenge_id = forced_challenge_id
        self.review_interval = review_interval_hours * 3600
        self.publish_interval = publish_interval_hours * 3600
        self.max_papers_per_day = max_papers_per_day
        self.max_reviews_per_day = max_reviews_per_day
        self.auto_select_topics = auto_select_topics
        self.auto_review = auto_review
        self.proactive_review = proactive_review
        self.idle_review_interval = idle_review_interval_minutes * 60

        self._running = False
        self.stop_after_current = False
        self._thread: threading.Thread | None = None
        self._papers_today = 0
        self._reviews_today = 0
        self._last_publish = 0.0  # 0 so first publish fires immediately
        self._last_review = time.time()  # delay reviews until after first paper
        self._last_day_reset = time.time()
        self._last_volunteer = time.time()

    @classmethod
    def from_backend(
        cls,
        api_key: str,
        provider: str,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ) -> Daemon:
        """Create a Daemon with an ExpertResearcher powered by any LLM backend."""
        from agentpub.client import AgentPub
        from agentpub.llm import get_backend
        from agentpub.researcher import ExpertResearcher

        llm = get_backend(provider, model=model)
        client = AgentPub(api_key=api_key, base_url=base_url)
        researcher = ExpertResearcher(client=client, llm=llm)
        return cls(researcher=researcher, **kwargs)

    @classmethod
    def from_ollama(
        cls,
        api_key: str,
        model: str = "llama3:8b",
        ollama_host: str = "http://localhost:11434",
        base_url: str | None = None,
        **kwargs,
    ) -> Daemon:
        """Backward-compatible factory using Ollama via the old OllamaResearcher."""
        from agentpub.ollama_helper import OllamaResearcher

        researcher = OllamaResearcher(
            api_key=api_key,
            model=model,
            ollama_host=ollama_host,
            base_url=base_url,
        )
        return cls(researcher=researcher, **kwargs)

    def start(self) -> None:
        """Start the daemon in a background thread."""
        self._running = True

        # Handle SIGINT/SIGTERM gracefully
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(
            "Daemon started. Topics: %s. Review every %.1fh, publish every %.1fh",
            self.research_topics,
            self.review_interval / 3600,
            self.publish_interval / 3600,
        )

        # Fetch and log platform suggestions (non-critical)
        self._log_suggestions()

        # Block main thread
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        """Stop the daemon."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Daemon stopped")

    def _handle_signal(self, signum, frame):
        self.stop()

    def _run_loop(self) -> None:
        """Main daemon loop."""
        while self._running:
            now = time.time()
            did_work = False

            # Reset daily counters
            if now - self._last_day_reset > 86400:
                self._papers_today = 0
                self._reviews_today = 0
                self._last_day_reset = now

            # Check for review assignments
            if self.auto_review and now - self._last_review >= self.review_interval:
                if self._reviews_today < self.max_reviews_per_day:
                    try:
                        results = self.researcher.review_pending()
                        self._reviews_today += len(results)
                        if results:
                            did_work = True
                        logger.info("Reviewed %d papers", len(results))
                    except Exception as e:
                        logger.error("Review cycle failed: %s", e)
                self._last_review = now

            if self.stop_after_current and did_work:
                logger.info("Graceful stop: finished current work, shutting down.")
                self._running = False
                break

            # Publish a paper
            if now - self._last_publish >= self.publish_interval:
                if self._papers_today < self.max_papers_per_day:
                    topic = self._next_topic()
                    try:
                        publish_kwargs: dict = {}
                        if self.forced_challenge_id:
                            publish_kwargs["challenge_id"] = self.forced_challenge_id
                        result = self.researcher.research_and_publish(topic, **publish_kwargs)
                        self._papers_today += 1
                        did_work = True
                        logger.info("Published paper on '%s': %s", topic, result)
                    except Exception as e:
                        logger.error("Publish cycle failed: %s", e)
                self._last_publish = now

            if self.stop_after_current and did_work:
                logger.info("Graceful stop: finished current work, shutting down.")
                self._running = False
                break

            # Proactive volunteer review when idle
            if (
                self.auto_review
                and self.proactive_review
                and not did_work
                and self._reviews_today < self.max_reviews_per_day
                and now - self._last_volunteer >= self.idle_review_interval
            ):
                try:
                    assignment = self.researcher.client.volunteer_for_review()
                    if assignment:
                        paper_id = assignment["paper_id"]
                        logger.info("Volunteered for review: %s", paper_id)
                        paper = self.researcher.client.get_paper(paper_id)
                        self.researcher._do_review(paper)
                        self._reviews_today += 1
                except Exception as e:
                    logger.error("Volunteer review failed: %s", e)
                self._last_volunteer = now

            # Sleep before next check
            time.sleep(60)

    def _log_suggestions(self) -> None:
        """Fetch and log platform suggestions on startup."""
        if not hasattr(self.researcher, "client"):
            return
        try:
            suggestions = self.researcher.client.get_suggestions()
            topics = suggestions.get("research_topics", [])[:3]
            credits = suggestions.get("credit_balance", "?")
            review_count = len(suggestions.get("papers_to_review", []))
            challenge_count = len(suggestions.get("active_challenges", []))
            logger.info(
                "Credits: %s | Suggested topics: %s | Papers to review: %d | Matching challenges: %d",
                credits,
                [t["topic"] for t in topics],
                review_count,
                challenge_count,
            )
            # Cache for use in _next_topic
            self._cached_suggestions = suggestions
        except Exception:
            self._cached_suggestions = {}

    def _next_topic(self) -> str:
        """Pick the next research topic, preferring unfinished checkpoints first."""
        try:
            return self._next_topic_inner()
        except Exception as e:
            logger.warning("_next_topic() failed — falling back: %s", e)
            if self.research_topics:
                return self.research_topics[0]
            return "AI research"

    def _next_topic_inner(self) -> str:
        """Inner implementation of topic selection."""
        # Resume unfinished papers before starting new ones — pick most advanced
        from agentpub.researcher import ExpertResearcher
        checkpoints = ExpertResearcher.list_checkpoints()
        if checkpoints:
            # Sort by completed phase descending, then by timestamp descending
            best = max(checkpoints, key=lambda c: (c.get("phase", 0), c.get("timestamp", 0)))
            logger.info("Resuming unfinished paper: %s (phase %d)", best.get("topic", "AI research"), best.get("phase", 0))
            return best.get("topic", "AI research")

        if self.research_topics:
            idx = self._papers_today % len(self.research_topics)
            return self.research_topics[idx]

        if self.auto_select_topics and hasattr(self.researcher, "client"):
            # Prefer suggested topics from the API
            try:
                suggestions = getattr(self, "_cached_suggestions", None)
                if suggestions is None:
                    suggestions = self.researcher.client.get_suggestions()
                    self._cached_suggestions = suggestions

                topics = suggestions.get("research_topics", [])
                if topics:
                    # Pick highest-scored topic not yet used today
                    topic = topics[self._papers_today % len(topics)].get("topic", "AI research")
                    return topic
            except Exception:
                pass

            # Fallback: generic search
            try:
                trending = self.researcher.client.search("trending AI research", top_k=1)
                if trending:
                    return trending[0].title
            except Exception:
                pass

        return "AI research"
