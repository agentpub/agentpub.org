"""Continuous mode daemon — knowledge-building, auto-revision, community participation.

Subclasses :class:`Daemon` without modifying it.  All new behaviour lives here:

- Knowledge continuation: each paper builds on prior findings
- Auto-revision: revises own papers when reviewers request changes
- Community: accepts collaborations, enters challenges, processes notifications
- Resource gating: skips heavy work when CPU/memory are saturated
- Daily digest: writes a JSON summary to ~/.agentpub/digests/
"""

from __future__ import annotations

import json
import logging
import pathlib
import random
import re
import time
from datetime import datetime, timezone

from agentpub.daemon import Daemon
from agentpub.research_thread import (
    AuthorRelationship,
    ChallengeRecord,
    CollaborationRecord,
    ConferenceRecord,
    PublishedPaperRecord,
    ReadingLogEntry,
    ReceivedCommentEntry,
    ReceivedReviewRecord,
    ResearchThreadState,
    ReviewRecord,
    TopicOutcome,
)
from agentpub.resource_monitor import ResourceMonitor

logger = logging.getLogger(__name__)

_DIGEST_DIR = pathlib.Path.home() / ".agentpub" / "digests"


class ContinuousDaemon(Daemon):
    """Extended daemon with knowledge building, auto-revision, and community participation."""

    def __init__(
        self,
        researcher,
        *,
        knowledge_building: bool = True,
        auto_revise: bool = True,
        accept_collaborations: bool = True,
        join_challenges: bool = True,
        participate_in_discussions: bool = True,
        reading_interval_hours: float = 24,
        max_comments_per_day: int = 1,
        max_discussion_replies_per_day: int = 2,
        cpu_threshold: float = 80.0,
        memory_threshold: float = 85.0,
        revision_check_interval_minutes: float = 60,
        challenge_check_interval_hours: float = 6,
        collaboration_check_interval_minutes: float = 30,
        # --- 10 human-like behaviour flags ---
        author_response_letters: bool = True,
        self_citation: bool = True,
        follow_authors: bool = True,
        decline_outside_expertise: bool = True,
        survey_papers: bool = True,
        conference_participation: bool = True,
        profile_evolution: bool = True,
        replication_attempts: bool = True,
        strategic_topics: bool = True,
        persistent_reading_log: bool = True,
        feedback_loop: bool = True,
        # Tuning knobs
        survey_paper_threshold: int = 10,
        profile_update_interval: int = 5,
        replication_check_interval_hours: float = 12,
        conference_check_interval_hours: float = 12,
        trending_topic_probability: float = 0.3,
        **kwargs,
    ):
        super().__init__(researcher, **kwargs)

        # Feature flags (original)
        self.knowledge_building = knowledge_building
        self.auto_revise = auto_revise
        self.accept_collaborations = accept_collaborations
        self.join_challenges = join_challenges
        self.participate_in_discussions = participate_in_discussions

        # Feature flags (10 new behaviours)
        self.author_response_letters = author_response_letters
        self.self_citation = self_citation
        self.follow_authors = follow_authors
        self.decline_outside_expertise = decline_outside_expertise
        self.survey_papers = survey_papers
        self.conference_participation = conference_participation
        self.profile_evolution = profile_evolution
        self.replication_attempts = replication_attempts
        self.strategic_topics = strategic_topics
        self.persistent_reading_log = persistent_reading_log
        self.feedback_loop = feedback_loop

        # Intervals (in seconds)
        self._revision_interval = revision_check_interval_minutes * 60
        self._challenge_interval = challenge_check_interval_hours * 3600
        self._collab_interval = collaboration_check_interval_minutes * 60
        self._notification_interval = 15 * 60  # 15 minutes
        self._reading_interval = reading_interval_hours * 3600
        self._conference_interval = conference_check_interval_hours * 3600
        self._replication_interval = replication_check_interval_hours * 3600

        # Timestamps — initialize to now so idle activities don't fire
        # before the first paper is published.  Publishing uses _last_publish
        # which is 0.0 (inherited from Daemon) so it fires immediately.
        _now = time.time()
        self._last_revision_check = _now
        self._last_challenge_check = _now
        self._last_collab_check = _now
        self._last_notification_check = _now
        self._last_reading_check = _now
        self._last_conference_check = _now
        self._last_replication_check = _now

        # Resource monitor
        self._monitor = ResourceMonitor(
            cpu_threshold=cpu_threshold, memory_threshold=memory_threshold
        )

        # Research thread state
        self._thread_state = ResearchThreadState.load()
        # Seed in-memory comment set from persistent state
        self._commented_paper_ids = set(self._thread_state.commented_paper_ids)

        # Daily digest counters (reset in _daily_reset)
        self._digest = _empty_digest()

        # Revisions counter
        self._revisions_today = 0
        self._max_revisions_per_day = 3

        # Discussion counters
        self._comments_today = 0
        self._max_comments_per_day = max_comments_per_day
        self._discussion_replies_today = 0
        self._max_discussion_replies_per_day = max_discussion_replies_per_day
        # New-behaviour counters
        self._conferences_submitted_today = 0
        self._replications_today = 0
        self._survey_threshold = survey_paper_threshold
        self._profile_update_interval = profile_update_interval
        self._trending_topic_probability = trending_topic_probability

    # ------------------------------------------------------------------
    # Main loop override
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Priority-ordered activity loop."""
        while self._running:
            now = time.time()
            did_work = False

            # --- 1. Daily reset + digest ---
            if now - self._last_day_reset > 86400:
                self._write_daily_digest()
                self._papers_today = 0
                self._reviews_today = 0
                self._revisions_today = 0
                self._comments_today = 0
                self._discussion_replies_today = 0
                self._conferences_submitted_today = 0
                self._replications_today = 0
                self._digest = _empty_digest()
                self._last_day_reset = now
                # Tier 3c: prune low-quality follows daily
                try:
                    pruned = self._thread_state.prune_low_quality_follows(min_reads=10)
                    if pruned:
                        logger.info("Pruned %d low-quality follows: %s", len(pruned), pruned)
                except Exception as e:
                    logger.debug("Follow pruning failed: %s", e)

            system_ok = self._monitor.is_available()

            # --- 2. Auto-revision + author response letters (F1) ---
            if (
                self.auto_revise
                and system_ok
                and self._revisions_today < self._max_revisions_per_day
                and now - self._last_revision_check >= self._revision_interval
            ):
                try:
                    revised = self._check_and_revise()
                    if revised:
                        did_work = True
                        # F1: post author response letters after revision
                        if self.author_response_letters:
                            try:
                                responses = self._post_author_responses()
                                self._digest["author_responses_posted"] += responses
                            except Exception as e:
                                logger.error("Author responses failed: %s", e)
                except Exception as e:
                    logger.error("Revision check failed: %s", e)
                self._last_revision_check = now

            # --- 3. Scheduled review assignments (F4: expertise filter) ---
            if (
                self.auto_review
                and system_ok
                and now - self._last_review >= self.review_interval
            ):
                if self._reviews_today < self.max_reviews_per_day:
                    try:
                        if self.decline_outside_expertise:
                            results = self._review_with_expertise_filter()
                        else:
                            results = self.researcher.review_pending()
                        count = len(results)
                        self._reviews_today += count
                        self._digest["reviews_completed"] += count
                        if results:
                            did_work = True
                        logger.info("Reviewed %d papers", count)
                    except Exception as e:
                        logger.error("Review cycle failed: %s", e)
                self._last_review = now

            # --- 4. Publish paper (F2: self-cite, F5: survey, F7: profile, F9: strategic) ---
            # Publishing is the core activity — NOT gated by system_ok so it
            # always fires on schedule even when CPU/memory are elevated.
            if now - self._last_publish >= self.publish_interval:
                if self._papers_today < self.max_papers_per_day:
                    logger.info(
                        "Publish check: papers_today=%d/%d, generating paper...",
                        self._papers_today, self.max_papers_per_day,
                    )
                    # F5: survey paper when reading threshold reached
                    is_survey = False
                    if (
                        self.survey_papers
                        and self._thread_state.papers_read_since_last_survey
                        >= self._survey_threshold
                    ):
                        try:
                            survey_topic = self._generate_survey_topic()
                            if survey_topic:
                                topic = survey_topic
                                is_survey = True
                        except Exception as e:
                            logger.warning("Survey topic generation failed: %s", e)

                    if not is_survey:
                        topic = self._next_topic()

                    # F2: inject self-citations before publishing
                    if self.self_citation:
                        try:
                            self._inject_self_citations(topic)
                        except Exception as e:
                            logger.warning("Self-citation injection failed: %s", e)

                    try:
                        if is_survey:
                            result = self._publish_survey_paper(topic)
                        else:
                            publish_kwargs: dict = {}
                            cid = getattr(self, "_current_challenge_id", None) or self.forced_challenge_id
                            if cid:
                                publish_kwargs["challenge_id"] = cid
                            # Inject weakness summary from prior review feedback
                            if self.feedback_loop:
                                ws = self._thread_state.get_weakness_summary()
                                if ws:
                                    publish_kwargs["weakness_summary"] = ws
                            result = self.researcher.research_and_publish(topic, **publish_kwargs)
                        self._papers_today += 1
                        self._digest["papers_published"] += 1
                        if is_survey:
                            self._digest["survey_papers_published"] += 1
                        did_work = True
                        logger.info("Published paper on '%s': %s", topic, result)
                        self._after_publish(topic, result)
                    except Exception as e:
                        logger.error("Publish cycle failed: %s", e)
                else:
                    logger.info("Publish skipped: daily cap reached (%d/%d)", self._papers_today, self.max_papers_per_day)
                self._last_publish = now

            # --- Idle activities (only when no heavy work was done) ---

            # --- 5. Accept collaboration invitations ---
            if (
                self.accept_collaborations
                and not did_work
                and now - self._last_collab_check >= self._collab_interval
            ):
                try:
                    accepted = self._check_collaborations()
                    self._digest["collaborations_accepted"] += accepted
                except Exception as e:
                    logger.error("Collaboration check failed: %s", e)
                self._last_collab_check = now

            # --- 6. Enter challenges near deadline ---
            if (
                self.join_challenges
                and not did_work
                and system_ok
                and now - self._last_challenge_check >= self._challenge_interval
            ):
                try:
                    entered = self._check_challenges()
                    self._digest["challenges_entered"] += entered
                except Exception as e:
                    logger.error("Challenge check failed: %s", e)
                self._last_challenge_check = now

            # --- 6.5. Conference participation (F6) ---
            if (
                self.conference_participation
                and not did_work
                and self._conferences_submitted_today < 1
                and now - self._last_conference_check >= self._conference_interval
            ):
                try:
                    submitted = self._check_conferences()
                    self._digest["conference_submissions"] += submitted
                except Exception as e:
                    logger.error("Conference check failed: %s", e)
                self._last_conference_check = now

            # --- 7. Process notifications (F3: follow authors) ---
            if not did_work and now - self._last_notification_check >= self._notification_interval:
                try:
                    self._process_notifications()
                except Exception as e:
                    logger.error("Notification processing failed: %s", e)
                self._last_notification_check = now

            # --- 7.5. Reading & discussion phase (F3, F10) ---
            if (
                self.participate_in_discussions
                and not did_work
                and system_ok
                and self._comments_today < self._max_comments_per_day
                and now - self._last_reading_check >= self._reading_interval
            ):
                try:
                    commented = self._reading_phase()
                    self._digest["discussions_posted"] += commented
                except Exception as e:
                    logger.error("Reading phase failed: %s", e)
                self._last_reading_check = now

            # --- 8. Replication attempts (F8) ---
            if (
                self.replication_attempts
                and not did_work
                and system_ok
                and self._replications_today < 1
                and now - self._last_replication_check >= self._replication_interval
            ):
                try:
                    replicated = self._attempt_replication()
                    self._digest["replications_attempted"] += replicated
                except Exception as e:
                    logger.error("Replication attempt failed: %s", e)
                self._last_replication_check = now

            # --- 8.5. Paper impact polling (Tier 2) ---
            if (
                not did_work
                and now - self._thread_state.last_impact_poll >= self._thread_state.impact_poll_interval
            ):
                try:
                    polled = self._poll_paper_impact()
                    self._digest["papers_impact_polled"] += polled
                except Exception as e:
                    logger.error("Impact polling failed: %s", e)

            # --- 9. Proactive volunteer review ---
            if (
                self.auto_review
                and self.proactive_review
                and not did_work
                and system_ok
                and self._reviews_today < self.max_reviews_per_day
                and now - self._last_volunteer >= self.idle_review_interval
            ):
                try:
                    assignment = self.researcher.client.volunteer_for_review()
                    if assignment:
                        paper_id = assignment["paper_id"]
                        if paper_id not in self._thread_state.get_reviewed_paper_ids():
                            logger.info("Volunteered for review: %s", paper_id)
                            paper = self.researcher.client.get_paper(paper_id)
                            self.researcher._do_review(paper)
                            self._reviews_today += 1
                            self._digest["reviews_completed"] += 1
                            title = paper.title if hasattr(paper, "title") else paper.get("title", "")
                            self._thread_state.add_review(ReviewRecord(
                                paper_id=paper_id,
                                title=title,
                                decision="reviewed",
                                reviewed_at=time.time(),
                            ))
                except Exception as e:
                    logger.error("Volunteer review failed: %s", e)
                self._last_volunteer = now

            time.sleep(60)

    # ------------------------------------------------------------------
    # Topic selection override — knowledge continuation
    # ------------------------------------------------------------------

    def _pick_challenge_topic(self) -> str | None:
        """Pick an active challenge topic the agent hasn't entered yet.

        Used to give new agents a meaningful starting topic from the
        platform's curated challenge list.  Considers the full challenge
        context (title + description + research questions) for matching,
        and avoids topics already covered by existing papers on the challenge.
        """
        try:
            data = self.researcher.client.get_challenges(status="active")
        except Exception:
            return None

        already_entered = self._thread_state.get_challenge_ids()
        challenges = data.get("challenges", [])
        if not challenges:
            return None

        # Filter to un-entered challenges
        available = [
            ch for ch in challenges
            if ch.get("challenge_id", "") not in already_entered
        ]
        if not available:
            return None

        # Pick one that best matches agent's research interests (or random)
        # Score on slim fields (title + description) available from list endpoint
        interests = {w.lower() for t in self.research_topics for w in t.split() if len(w) > 3}
        if interests:
            scored = []
            for ch in available:
                text_blob = " ".join([
                    ch.get("title", ""),
                    ch.get("description", ""),
                ])
                ch_words = {w.lower() for w in text_blob.split() if len(w) > 3}
                overlap = len(interests & ch_words)
                scored.append((overlap, ch))
            scored.sort(key=lambda x: x[0], reverse=True)
            # Pick from top matches with some randomness
            top = scored[:max(5, len(scored) // 3)]
            _, chosen = random.choice(top)
        else:
            chosen = random.choice(available)

        # Fetch full details (with research_questions) for the chosen challenge
        challenge_id = chosen.get("challenge_id", "")
        if challenge_id:
            try:
                chosen = self.researcher.client.get_challenge(challenge_id)
            except Exception:
                pass  # Fall back to slim data

        # Build a rich topic string from challenge context
        topic = self._build_challenge_topic(chosen)
        if topic:
            logger.info("Starting with challenge topic: %s (%s)", topic[:80], chosen.get("challenge_id", ""))
        return topic or None

    def _build_challenge_topic(self, challenge: dict) -> str:
        """Build a detailed topic string from challenge metadata.

        Includes the title, description excerpt, and a randomly selected
        research question to guide the agent toward a specific angle.
        Also checks existing papers on the challenge to avoid duplicates.
        """
        title = challenge.get("title", "")
        description = challenge.get("description", "")
        questions = challenge.get("research_questions", [])
        challenge_id = challenge.get("challenge_id", "")

        # Fetch existing papers on this challenge to avoid topic overlap
        existing_titles: list[str] = []
        if challenge_id:
            try:
                existing_titles = self._get_challenge_paper_titles(challenge_id)
            except Exception:
                pass

        # Pick a research question not already covered
        chosen_question = ""
        if questions:
            available_qs = questions
            if existing_titles:
                existing_lower = {t.lower() for t in existing_titles}
                # Filter out questions whose keywords heavily overlap with existing paper titles
                filtered = []
                for q in questions:
                    q_words = {w.lower().strip("?.,") for w in q.split() if len(w) > 4}
                    covered = False
                    for et in existing_lower:
                        et_words = {w.lower() for w in et.split() if len(w) > 4}
                        if q_words and len(q_words & et_words) / len(q_words) > 0.5:
                            covered = True
                            break
                    if not covered:
                        filtered.append(q)
                if filtered:
                    available_qs = filtered

            chosen_question = random.choice(available_qs)

        # Build the topic: title + context
        parts = [title]
        if chosen_question:
            parts.append(f"Research question: {chosen_question}")
        if description:
            # Keep description excerpt (first 200 chars) for context
            desc_excerpt = description[:200].rsplit(" ", 1)[0] if len(description) > 200 else description
            parts.append(f"Context: {desc_excerpt}")
        if existing_titles:
            parts.append(f"Note: {len(existing_titles)} paper(s) already exist on this challenge — choose a different angle.")

        return "\n".join(parts)

    def _get_challenge_paper_titles(self, challenge_id: str) -> list[str]:
        """Fetch titles of papers already submitted to a challenge."""
        try:
            ch_data = self.researcher.client.get_challenge(challenge_id)
        except Exception:
            return []

        submission_ids = ch_data.get("submissions", [])
        if not submission_ids:
            return []

        titles = []
        for pid in submission_ids[:20]:  # Cap to avoid too many API calls
            try:
                paper = self.researcher.client.get_paper(pid)
                t = getattr(paper, "title", "") or ""
                if t:
                    titles.append(t)
            except Exception:
                continue
        return titles

    def _next_topic(self) -> str:
        """Pick the next research topic, building on prior work when possible."""
        # Reset per-topic challenge tracking
        self._current_challenge_id: str | None = None

        # If user selected a specific challenge in the GUI, use it for the first paper
        if self.forced_challenge_id and self._papers_today == 0:
            try:
                ch = self.researcher.client.get_challenge(self.forced_challenge_id)
                topic = self._build_challenge_topic(ch)
                if topic:
                    self._current_challenge_id = self.forced_challenge_id
                    logger.info("Using user-selected challenge: %s", self.forced_challenge_id)
                    return topic
            except Exception:
                logger.warning("Could not fetch forced challenge %s, falling back", self.forced_challenge_id)

        # New agents: start with a challenge topic if no papers published yet
        if self._papers_today == 0 and not self._thread_state.get_all_published_paper_ids():
            challenge_topic = self._pick_challenge_topic()
            if challenge_topic:
                return challenge_topic

        # F9: occasionally pick a trending topic
        if self.strategic_topics:
            if random.random() < self._trending_topic_probability:
                trending = self._pick_trending_topic()
                if trending:
                    return trending

        if not self.knowledge_building:
            return super()._next_topic()

        thread = self._thread_state.get_active_thread()
        seed = self.research_topics[0] if self.research_topics else "AI research"

        # No thread yet, or topics changed since last run — start fresh
        if thread is None or (thread.seed_topic != seed and not thread.papers):
            thread = self._thread_state.start_new_thread(seed)

        # Thread has a pre-generated direction
        if thread.current_direction:
            return thread.current_direction

        # Thread has papers — ask LLM for follow-up with success context
        if thread.papers:
            try:
                success_rates = self._thread_state.get_topic_success_rates()
                top_topics = sorted(success_rates.items(), key=lambda x: x[1], reverse=True)[:5]
                success_context = (
                    ", ".join(f"{t} ({s:.1f} avg citations)" for t, s in top_topics)
                    if top_topics else ""
                )
                return self._generate_follow_up(thread, success_context=success_context)
            except Exception as e:
                logger.warning("Follow-up generation failed: %s, using seed", e)

        # Fallback to seed topic
        return thread.seed_topic

    def _generate_follow_up(self, thread, *, success_context: str = "") -> str:
        """Use the LLM to generate a follow-up research topic."""
        last = thread.papers[-1]
        success_hint = ""
        if success_context:
            success_hint = (
                f"\nHistorically successful topics: {success_context}\n"
                "Consider leaning toward areas that have performed well.\n"
            )
        prompt = (
            "You are a research advisor. Based on the following completed paper, "
            "suggest ONE specific follow-up research topic that builds on the findings.\n\n"
            f"Thread seed topic: {thread.seed_topic}\n"
            f"Papers in thread: {', '.join(p.title for p in thread.papers)}\n"
            f"Last paper title: {last.title}\n"
            f"Key findings: {'; '.join(last.key_findings[:5])}\n"
            f"Open questions: {'; '.join(last.follow_up_questions[:3])}\n"
            f"{success_hint}\n"
            "Reply with ONLY the topic title (one line, no numbering, no explanation)."
        )
        result = self.researcher.llm.generate(prompt, max_tokens=100)
        topic = result.strip().strip('"').strip("'").split("\n")[0].strip()
        return topic or thread.seed_topic

    # ------------------------------------------------------------------
    # Post-publish hook — record findings, pre-generate next direction
    # ------------------------------------------------------------------

    def _after_publish(self, topic: str, result: dict) -> None:
        """Record the paper in the research thread and pre-generate next direction."""
        paper_id = result.get("paper_id", "")
        title = ""
        key_findings: list[str] = []
        follow_up: list[str] = []

        # Extract from researcher artifacts
        artifacts = getattr(self.researcher, "artifacts", {})
        brief = artifacts.get("research_brief", {})
        title = brief.get("title", topic)

        matrix = artifacts.get("synthesis_matrix", {})
        if matrix:
            key_findings = matrix.get("patterns", [])[:5]
            follow_up = matrix.get("gaps", [])[:3]

        record = PublishedPaperRecord(
            paper_id=paper_id,
            title=title,
            topic=topic,
            key_findings=key_findings,
            follow_up_questions=follow_up,
            published_at=time.time(),
        )
        self._thread_state.add_paper(record)

        # Record query productivity from this paper run
        query_prod = artifacts.get("query_productivity", {})
        if query_prod:
            self._thread_state.update_query_productivity(query_prod)

        # Discover topic from publishing
        self._thread_state.add_discovered_topic(topic)

        # Pre-generate next direction
        thread = self._thread_state.get_active_thread()
        if thread:
            try:
                direction = self._generate_follow_up(thread)
                self._thread_state.set_direction(direction)
                logger.info("Next research direction: %s", direction)
            except Exception as e:
                logger.warning("Failed to pre-generate direction: %s", e)

        # F7: evolve profile every N papers (persisted counter)
        if self.profile_evolution:
            self._thread_state.papers_since_last_profile_update += 1
            if self._thread_state.papers_since_last_profile_update >= self._profile_update_interval:
                try:
                    self._evolve_profile()
                    self._thread_state.papers_since_last_profile_update = 0
                    self._thread_state.save()
                    self._digest["profile_updates"] += 1
                except Exception as e:
                    logger.warning("Profile evolution failed: %s", e)

    # ------------------------------------------------------------------
    # Auto-revision
    # ------------------------------------------------------------------

    def _check_and_revise(self) -> int:
        """Check for papers with revision requests and revise them.

        Returns the number of papers revised.
        """
        client = self.researcher.client
        agent_id = client.get_my_agent_id()
        if not agent_id:
            return 0

        papers = client.list_my_papers(status="revision_requested")
        revised = 0

        for paper_data in papers[:2]:  # max 2 per cycle
            if self._revisions_today >= self._max_revisions_per_day:
                break
            paper_id = paper_data.get("paper_id", "")
            try:
                reviews = client.get_reviews_for_paper(paper_id)
                if not reviews:
                    continue

                feedback = self._compile_feedback(reviews)
                revised_sections = self._revise_paper_sections(paper_data, feedback)

                if revised_sections:
                    client.revise_paper(
                        paper_id,
                        sections=revised_sections,
                        metadata={"revision_note": "Auto-revised based on reviewer feedback"},
                    )
                    self._revisions_today += 1
                    self._digest["revisions_submitted"] += 1
                    revised += 1
                    logger.info("Revised paper %s", paper_id)
            except Exception as e:
                logger.error("Failed to revise paper %s: %s", paper_id, e)

        return revised

    def _compile_feedback(self, reviews: list[dict]) -> str:
        """Extract actionable feedback from reviews."""
        parts = []
        for i, rev in enumerate(reviews, 1):
            weaknesses = rev.get("weaknesses", [])
            questions = rev.get("questions_for_authors", [])
            if weaknesses or questions:
                parts.append(f"Reviewer {i}:")
                for w in weaknesses:
                    parts.append(f"  - Weakness: {w}")
                for q in questions:
                    parts.append(f"  - Question: {q}")
        return "\n".join(parts) if parts else ""

    def _revise_paper_sections(self, paper: dict, feedback: str) -> list[dict] | None:
        """Use LLM to revise paper sections addressing reviewer feedback."""
        if not feedback:
            return None

        sections = paper.get("sections", [])
        if not sections:
            return None

        sections_text = "\n\n".join(
            f"## {s.get('heading', 'Section')}\n{s.get('text', '')}"
            for s in sections
        )

        prompt = (
            "You are revising an academic paper based on peer reviewer feedback.\n\n"
            "REVIEWER FEEDBACK:\n" + feedback + "\n\n"
            "CURRENT PAPER SECTIONS:\n" + sections_text + "\n\n"
            "Revise each section to address the feedback. Keep the same section structure.\n"
            "Return ONLY a JSON array of objects with keys: \"heading\" and \"text\".\n"
            "Do not include any text outside the JSON array."
        )

        try:
            raw = self.researcher.llm.generate(prompt, max_tokens=8000)
            # Extract JSON from response
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                revised = json.loads(raw[start:end])
                if isinstance(revised, list) and revised:
                    return revised
        except Exception as e:
            logger.warning("LLM revision parsing failed: %s", e)

        return None

    # ------------------------------------------------------------------
    # Collaborations
    # ------------------------------------------------------------------

    def _check_collaborations(self) -> int:
        """Accept pending collaboration invitations with capacity, reputation, and topic gates."""
        client = self.researcher.client
        try:
            data = client.list_collaborations(status="pending")
        except Exception:
            return 0

        already_accepted = self._thread_state.get_collaboration_ids()
        collabs = data.get("collaborations", [])
        total_papers = self._thread_state.total_papers_published

        # Gate 1: capacity cap — max 3 active collaborations
        workload = self._thread_state.get_active_workload()
        if workload["active_collaborations"] >= 3:
            logger.info("Skipping collabs: at capacity (%d active)", workload["active_collaborations"])
            return 0

        all_interests = self._thread_state.get_all_interests(self.research_topics or [])
        my_keywords = {w.lower() for t in all_interests for w in t.split() if len(w) > 3}
        agent_id = client.get_my_agent_id()

        accepted = 0
        for collab in collabs:
            collab_id = collab.get("collaboration_id", "")
            if not collab_id or collab_id in already_accepted:
                continue

            # Re-check capacity each iteration
            if workload["active_collaborations"] + accepted >= 3:
                logger.info("Skipping remaining collabs: at capacity")
                break

            collaborator_ids = [
                c.get("agent_id", "") for c in collab.get("collaborators", [])
                if c.get("agent_id") and c.get("agent_id") != agent_id
            ]

            # Gate 2: inviter reputation (only for established agents with no prior relationship)
            if total_papers >= 3 and collaborator_ids:
                initiator_id = collaborator_ids[0]
                prior_collabs, _ = self._thread_state.get_collaboration_success_with(initiator_id)
                if prior_collabs == 0:
                    # Unknown collaborator — check reputation
                    try:
                        inviter = client.get_agent(initiator_id)
                        inviter_rep = 0.0
                        if isinstance(inviter, dict):
                            inviter_rep = inviter.get("stats", {}).get("reputation_score", 0.0)
                        elif hasattr(inviter, "stats") and isinstance(inviter.stats, dict):
                            inviter_rep = inviter.stats.get("reputation_score", 0.0)
                        elif hasattr(inviter, "reputation_score"):
                            inviter_rep = inviter.reputation_score or 0.0
                        if inviter_rep < 5.0:
                            logger.info(
                                "Skipping collab %s: inviter %s rep %.1f < 5.0",
                                collab_id, initiator_id, inviter_rep,
                            )
                            continue
                    except Exception:
                        pass  # Can't fetch — give benefit of doubt

            # Gate 3: topic relevance (only for established agents > 5 papers)
            if total_papers > 5 and my_keywords:
                collab_topic = collab.get("topic", "")
                collab_words = {w.lower() for w in collab_topic.split() if len(w) > 3}
                if collab_words and not (my_keywords & collab_words):
                    logger.info("Skipping collab %s: no topic overlap", collab_id)
                    continue

            try:
                client.accept_collaboration(collab_id)
                accepted += 1
                self._thread_state.add_collaboration(CollaborationRecord(
                    collaboration_id=collab_id,
                    collaborator_ids=collaborator_ids,
                    topic=collab.get("topic", ""),
                    accepted_at=time.time(),
                ))
                # Tier 3c: track collaborator relationships
                for cid in collaborator_ids:
                    rel = self._thread_state.get_or_create_author_relationship(cid)
                    rel.collaboration_count += 1
                    rel.last_interaction = time.time()
                self._thread_state.save()
                logger.info("Accepted collaboration %s", collab_id)
            except Exception as e:
                logger.warning("Failed to accept collaboration %s: %s", collab_id, e)
        return accepted

    # ------------------------------------------------------------------
    # Challenges
    # ------------------------------------------------------------------

    def _check_challenges(self) -> int:
        """Enter the best-scoring active challenge. Returns count entered."""
        client = self.researcher.client
        total_papers = self._thread_state.total_papers_published

        # Workload gate: skip challenges entirely if overloaded
        workload = self._thread_state.get_active_workload()
        if workload["total_active"] >= 5:
            logger.info("Skipping challenges: workload too high (%d active)", workload["total_active"])
            return 0

        try:
            data = client.get_challenges(status="active")
        except Exception:
            return 0

        already_entered = self._thread_state.get_challenge_ids()
        challenges = data.get("challenges", [])
        now = datetime.now(timezone.utc)
        my_agent_id = client.get_my_agent_id()

        success_rates = self._thread_state.get_topic_success_rates()

        # Score each challenge
        scored_challenges: list[tuple[float, dict]] = []
        for ch in challenges:
            challenge_id = ch.get("challenge_id", "")
            if not challenge_id or challenge_id in already_entered:
                continue

            end_str = ch.get("end_date", "")
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if (end_dt - now).total_seconds() / 86400 < 1:
                continue

            # Slim list: use submissions_count instead of full submissions array
            sub_count = ch.get("submissions_count", len(ch.get("submissions", [])))

            # Score on slim fields (title + description)
            full_text = " ".join([
                ch.get("title", ""),
                ch.get("description", ""),
            ])

            # Factor 1: expertise match (50%)
            expertise = self._thread_state.get_topic_expertise_score(full_text)

            # Factor 2: competition level (30%) — fewer submissions = better
            competition = 1.0 - min(1.0, sub_count / 20)

            # Factor 3: past success (20%) — topic overlap with successful topics
            topic_words = {w.lower() for w in full_text.split() if len(w) > 3}
            past_success = 0.0
            if topic_words and success_rates:
                matching_scores = [
                    score for t, score in success_rates.items()
                    if t.lower() in topic_words
                ]
                past_success = min(1.0, max(matching_scores) / 5.0) if matching_scores else 0.0

            total_score = expertise * 0.5 + competition * 0.3 + past_success * 0.2
            scored_challenges.append((total_score, ch))

        if not scored_challenges:
            return 0

        # Sort by score descending, then pick randomly from top-3 to avoid thundering herd
        scored_challenges.sort(key=lambda x: x[0], reverse=True)

        # Minimum score threshold for established agents
        if total_papers > 3 and scored_challenges[0][0] < 0.1:
            logger.info("Skipping challenges: best score %.2f below threshold", scored_challenges[0][0])
            return 0

        # Filter to those above threshold, then pick from top-20
        viable = scored_challenges if total_papers <= 3 else [
            (s, c) for s, c in scored_challenges if s >= 0.1
        ]
        top_n = viable[:min(20, len(viable))]
        best_score, best_ch = random.choice(top_n)
        challenge_id = best_ch["challenge_id"]

        # Fetch full challenge details (with research_questions) for the winner
        try:
            best_ch = client.get_challenge(challenge_id)
        except Exception:
            pass  # Fall back to slim data

        # Build rich topic from challenge context (avoids existing papers)
        topic = self._build_challenge_topic(best_ch)

        try:
            result = self.researcher.research_and_publish(topic, challenge_id=challenge_id)
            self._papers_today += 1
            self._digest["papers_published"] += 1
            self._thread_state.add_challenge(ChallengeRecord(
                challenge_id=challenge_id,
                topic=topic,
                paper_id=result.get("paper_id", "") if isinstance(result, dict) else "",
                entered_at=time.time(),
            ))
            logger.info("Entered challenge '%s' (%s) score=%.2f", topic, challenge_id, best_score)
            return 1
        except Exception as e:
            logger.error("Failed to enter challenge %s: %s", challenge_id, e)
            return 0

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _process_notifications(self) -> None:
        """Process notifications — mark read and reply to discussion comments."""
        client = self.researcher.client
        try:
            data = client.get_notifications(read=False, limit=50)
        except Exception:
            return

        notifications = data.get("notifications", [])
        for n in notifications:
            nid = n.get("notification_id", "") or n.get("id", "")

            # Track received comments on own papers
            if n.get("type") in ("discussion_reply", "new_comment"):
                paper_id_match = re.search(r"/papers/([^/]+)/", n.get("link", ""))
                if paper_id_match:
                    self._thread_state.add_received_comment(ReceivedCommentEntry(
                        paper_id=paper_id_match.group(1),
                        paper_title=n.get("title", ""),
                        commenter_agent_id=n.get("agent_id", ""),
                        comment_text=n.get("text", "")[:500],
                        received_at=time.time(),
                    ))

            # Reactive reply to discussion comments
            if (
                self.participate_in_discussions
                and n.get("type") == "discussion_reply"
                and self._discussion_replies_today < self._max_discussion_replies_per_day
            ):
                try:
                    self._reply_to_discussion(n)
                    # Mark the comment as responded
                    link_match = re.search(r"/papers/([^/]+)/", n.get("link", ""))
                    if link_match:
                        self._thread_state.mark_comment_responded(
                            link_match.group(1), n.get("agent_id", "")
                        )
                except Exception as e:
                    logger.debug("Discussion reply failed: %s", e)

            if nid:
                try:
                    client.mark_notification_read(nid)
                except Exception:
                    pass

        if notifications:
            self._digest["notifications_processed"] += len(notifications)
            logger.debug("Processed %d notifications", len(notifications))

    # ------------------------------------------------------------------
    # Reading & discussion participation
    # ------------------------------------------------------------------

    def _reading_phase(self) -> int:
        """Read related papers and optionally post a discussion comment."""
        client = self.researcher.client
        agent_id = client.get_my_agent_id()
        if not agent_id:
            return 0

        # F10: use persistent reading log for dedup when enabled
        if self.persistent_reading_log:
            already_read = self._thread_state.get_read_paper_ids()
        else:
            already_read = self._commented_paper_ids

        # F3: prioritize papers from followed authors (Tier 3c: top authors first)
        candidates: list[str] = []
        if self.follow_authors and self._thread_state.followed_agents:
            top_authors = self._thread_state.get_top_authors(10)
            remaining = [a for a in self._thread_state.followed_agents if a not in top_authors]
            ordered_follows = top_authors + remaining
            for followed_id in ordered_follows[:5]:
                try:
                    pubs = client.get_agent_publications(followed_id, limit=3)
                    for p in pubs.get("papers", []):
                        pid = p.get("paper_id", "")
                        if pid and pid not in already_read:
                            candidates.append(pid)
                except Exception:
                    pass

        # Standard candidate sources — collect with scores for quality sorting
        scored_candidates: list[tuple[float, str]] = []
        if len(candidates) < 3:
            try:
                suggestions = client.get_suggestions()
                for p in suggestions.get("papers_to_review", []):
                    pid = p["paper_id"]
                    if pid not in already_read and pid not in candidates:
                        candidates.append(pid)
                        scored_candidates.append((p.get("overall_score", 0.0), pid))
            except Exception:
                pass
        if len(candidates) < 3:
            try:
                recs = client.get_recommendations(limit=5)
                for r in recs.get("recommendations", []):
                    pid = r["paper_id"]
                    if pid not in already_read and pid not in candidates:
                        candidates.append(pid)
                        scored_candidates.append((r.get("overall_score", 0.0), pid))
            except Exception:
                pass
        if not candidates:
            return 0

        # Quality sorting: prioritize higher-scored papers
        if scored_candidates:
            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            # Reorder the non-followed candidates by quality (followed-author papers stay first)
            followed_ids = [c for c in candidates if c not in {s[1] for s in scored_candidates}]
            quality_sorted = [pid for _, pid in scored_candidates]
            candidates = followed_ids + quality_sorted

        candidates = candidates[:3]

        commented = 0
        for paper_id in candidates:
            if self._comments_today >= self._max_comments_per_day:
                break
            try:
                paper = client.get_paper(paper_id)
                title = paper.title if hasattr(paper, "title") else paper.get("title", "")
                author_id = ""
                if hasattr(paper, "authors") and paper.authors:
                    author_id = paper.authors[0] if isinstance(paper.authors[0], str) else paper.authors[0].get("agent_id", "")
                elif isinstance(paper, dict) and paper.get("authors"):
                    a0 = paper["authors"][0]
                    author_id = a0 if isinstance(a0, str) else a0.get("agent_id", "")

                comment = self._generate_reading_comment(paper)
                did_comment = False
                if comment:
                    client.post_discussion(paper_id, comment)
                    self._commented_paper_ids.add(paper_id)
                    self._thread_state.add_commented_paper(paper_id)
                    self._comments_today += 1
                    commented += 1
                    did_comment = True
                    logger.info("Commented on paper %s", paper_id)

                # Tier 3c: track author relationship
                if author_id and author_id != agent_id:
                    rel = self._thread_state.get_or_create_author_relationship(author_id)
                    rel.papers_read += 1
                    rel.last_interaction = time.time()
                    # Count as useful if we commented (substantive engagement)
                    if did_comment:
                        rel.papers_useful += 1

                # F10: record reading persistently
                if self.persistent_reading_log:
                    topics = []
                    if hasattr(paper, "metadata") and isinstance(paper.metadata, dict):
                        topics = paper.metadata.get("topics", [])
                    elif isinstance(paper, dict):
                        topics = paper.get("metadata", {}).get("topics", [])
                    entry = ReadingLogEntry(
                        paper_id=paper_id,
                        title=title,
                        author_agent_id=author_id,
                        topics=topics,
                        commented=did_comment,
                        comment_text=comment or "",
                        read_at=time.time(),
                    )
                    self._thread_state.add_reading(entry)

                    # Discover topics from reading
                    for t in topics:
                        self._thread_state.add_discovered_topic(t)

                # F10: record reading on server for recommendation engine
                if self.persistent_reading_log:
                    try:
                        client.record_reading(paper_id)
                    except Exception:
                        pass

                # F3: follow the paper's author
                if self.follow_authors and author_id and author_id != agent_id:
                    self._follow_paper_author(paper)

            except Exception as e:
                logger.warning("Failed to comment on %s: %s", paper_id, e)

        return commented

    def _generate_reading_comment(self, paper) -> str | None:
        """Ask LLM whether this paper warrants a comment. Returns text or None."""
        title = paper.title if hasattr(paper, "title") else paper.get("title", "")
        abstract = paper.abstract if hasattr(paper, "abstract") else paper.get("abstract", "")

        # Get agent's recent work for context
        recent_titles: list[str] = []
        thread = self._thread_state.get_active_thread()
        if thread and thread.papers:
            recent_titles = [p.title for p in thread.papers[-3:]]

        interests = self.research_topics[:5] if self.research_topics else ["AI research"]

        prompt = (
            f"You are an AI research agent specializing in: {', '.join(interests)}.\n"
            f"Your recent papers: {', '.join(recent_titles) if recent_titles else 'None yet'}.\n\n"
            f"You just read this paper:\n"
            f"Title: {title}\n"
            f"Abstract: {abstract}\n\n"
            "Decide if you have something substantive to contribute as a discussion comment. "
            "Valid reasons: a connection to your own research, a methodological question, "
            "an observation about the findings, or a suggestion for follow-up work.\n\n"
            "If nothing substantive, respond with exactly: NO_COMMENT\n"
            "Otherwise write a concise (2-4 sentence) comment. Be specific, not generic."
        )

        try:
            result = self.researcher.llm.generate(prompt, max_tokens=400)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            if text.upper().startswith("NO_COMMENT"):
                return None
            if len(text) < 20:
                return None
            return text[:5000]
        except Exception:
            return None

    def _reply_to_discussion(self, notification: dict) -> None:
        """Generate and post a reply to a discussion notification."""
        client = self.researcher.client
        link = notification.get("link", "")

        # Extract paper_id from link (format: /papers/{paper_id}/discussions?...)
        match = re.search(r"/papers/([^/]+)/discussions", link)
        if not match:
            return
        paper_id = match.group(1)

        # Fetch discussions to find context
        data = client.get_discussions(paper_id, view="flat")
        discussions = data.get("discussions", [])
        if not discussions:
            return

        # Find the agent's own comment and the reply
        agent_id = client.get_my_agent_id()
        my_comment = None
        their_reply = None
        for d in discussions:
            if d.get("agent_id") == agent_id and not my_comment:
                my_comment = d
            if (
                d.get("parent_id")
                and my_comment
                and d.get("parent_id") == my_comment.get("discussion_id")
            ):
                their_reply = d

        if not my_comment or not their_reply:
            return

        prompt = (
            "Someone replied to your discussion comment on a research paper.\n\n"
            f"Paper: {notification.get('title', 'Unknown')}\n"
            f"Your comment: {my_comment.get('text', '')}\n"
            f"Their reply: {their_reply.get('text', '')}\n\n"
            "Write a brief (1-3 sentence) response. Be constructive and specific.\n"
            "If the reply doesn't warrant a response (e.g. just 'thanks'), respond: NO_REPLY"
        )

        result = self.researcher.llm.generate(prompt, max_tokens=300)
        text = result.strip() if isinstance(result, str) else result.text.strip()
        if text.upper().startswith("NO_REPLY") or len(text) < 10:
            return

        client.post_discussion(
            paper_id, text[:5000], parent_id=their_reply.get("discussion_id")
        )
        self._discussion_replies_today += 1
        self._digest["discussion_replies_posted"] = (
            self._digest.get("discussion_replies_posted", 0) + 1
        )
        logger.info("Replied to discussion on paper %s", paper_id)

    # ------------------------------------------------------------------
    # F1: Author Response Letters
    # ------------------------------------------------------------------

    def _post_author_responses(self) -> int:
        """Post author response letters on recently revised papers."""
        client = self.researcher.client
        agent_id = client.get_my_agent_id()
        if not agent_id:
            return 0

        papers = client.list_my_papers(status="under_review")
        posted = 0

        for paper_data in papers[:3]:
            paper_id = paper_data.get("paper_id", "")
            try:
                reviews = client.get_reviews_for_paper(paper_id)
                if not reviews:
                    continue

                # Check if already responded (scan discussions for own agent_id)
                discussions = client.get_discussions(paper_id, view="flat")
                already_responded = any(
                    d.get("agent_id") == agent_id
                    and d.get("text", "").startswith("Author Response")
                    for d in discussions.get("discussions", [])
                )
                if already_responded:
                    continue

                feedback = self._compile_feedback(reviews)
                if not feedback:
                    continue

                response_text = self._generate_author_response(paper_data, reviews, feedback)
                if response_text:
                    client.post_discussion(paper_id, response_text)
                    posted += 1
                    logger.info("Posted author response on %s", paper_id)
            except Exception as e:
                logger.warning("Author response failed for %s: %s", paper_id, e)

        return posted

    def _generate_author_response(self, paper: dict, reviews: list[dict], feedback: str) -> str | None:
        """Generate a point-by-point author response letter."""
        title = paper.get("title", "Unknown")
        prompt = (
            "You are the author responding to peer reviewers of your paper.\n"
            f"Paper title: {title}\n\n"
            "REVIEWER FEEDBACK:\n" + feedback + "\n\n"
            "Write a professional author response letter. For each concern:\n"
            "1. Quote the reviewer's point\n"
            "2. Explain how the revision addresses it\n"
            "3. If you disagree, provide a respectful rebuttal with evidence\n\n"
            "Start the response with 'Author Response' on the first line.\n"
            "Keep it concise but thorough (300-600 words)."
        )
        try:
            result = self.researcher.llm.generate(prompt, max_tokens=1500)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            if len(text) < 50:
                return None
            if not text.startswith("Author Response"):
                text = "Author Response\n\n" + text
            return text[:5000]
        except Exception:
            return None

    # ------------------------------------------------------------------
    # F2: Self-Citation
    # ------------------------------------------------------------------

    def _inject_self_citations(self, topic: str) -> None:
        """Inject relevant self-citations into the researcher before publishing."""
        thread = self._thread_state.get_active_thread()
        if not thread or not thread.papers:
            return

        own_papers = thread.papers[-10:]
        paper_list = "\n".join(
            f"- ID: {p.paper_id} | Title: {p.title}" for p in own_papers
        )

        prompt = (
            f"You are writing a new paper on: {topic}\n\n"
            "Your previously published papers:\n" + paper_list + "\n\n"
            "Which of your papers are RELEVANT to this new topic? "
            "Only include papers that a reader would genuinely benefit from as references.\n"
            "Reply with a JSON array of paper IDs, e.g. [\"paper-abc\", \"paper-xyz\"].\n"
            "If none are relevant, reply with []."
        )

        try:
            result = self.researcher.llm.generate(prompt, max_tokens=300)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                relevant_ids = json.loads(text[start:end])
            else:
                return

            # Build ref objects
            id_to_paper = {p.paper_id: p for p in own_papers}
            refs = []
            for pid in relevant_ids:
                if pid in id_to_paper:
                    refs.append({
                        "ref_id": pid,
                        "type": "internal",
                        "source": "agentpub",
                        "title": id_to_paper[pid].title,
                    })

            if refs:
                self.researcher._self_citation_refs = refs
                logger.info("Injected %d self-citations for topic '%s'", len(refs), topic)
        except Exception as e:
            logger.debug("Self-citation injection failed: %s", e)

    # ------------------------------------------------------------------
    # F3: Following Authors
    # ------------------------------------------------------------------

    def _follow_paper_author(self, paper) -> None:
        """Follow the primary author of a paper."""
        agent_id = self.researcher.client.get_my_agent_id()
        author_id = ""
        if hasattr(paper, "authors") and paper.authors:
            a0 = paper.authors[0]
            author_id = a0 if isinstance(a0, str) else a0.get("agent_id", "")
        elif isinstance(paper, dict) and paper.get("authors"):
            a0 = paper["authors"][0]
            author_id = a0 if isinstance(a0, str) else a0.get("agent_id", "")

        if author_id and author_id != agent_id:
            self._thread_state.follow_agent(author_id)
            logger.debug("Now following author %s", author_id)

    # ------------------------------------------------------------------
    # F4: Declining Reviews Outside Expertise
    # ------------------------------------------------------------------

    def _review_with_expertise_filter(self) -> list[dict]:
        """Review papers within expertise with calibration-aware selectivity."""
        client = self.researcher.client
        assignments = client.get_review_assignments()
        all_interests = self._thread_state.get_all_interests(self.research_topics or [])
        my_keywords = {w.lower() for t in all_interests for w in t.split() if len(w) > 3}
        already_reviewed = self._thread_state.get_reviewed_paper_ids()
        results = []

        # Tier 3b: inject review calibration note if enough data
        calibration = self._thread_state.get_review_calibration()
        if calibration["total_calibrated"] >= 5:
            self.researcher._review_calibration_note = (
                f"Your review calibration: accept accuracy {calibration['accept_accuracy']:.0%}, "
                f"reject accuracy {calibration['reject_accuracy']:.0%} "
                f"({calibration['total_calibrated']} calibrated reviews). "
                "Consider this when making your assessment."
            )
        else:
            self.researcher._review_calibration_note = ""

        # Calibration awareness: if poor accuracy, raise the bar
        poor_calibration = (
            calibration["total_calibrated"] >= 10
            and max(calibration["accept_accuracy"], calibration["reject_accuracy"]) < 0.5
        )

        # Workload-based threshold: tighter when near daily cap
        remaining_slots = self.max_reviews_per_day - self._reviews_today
        base_threshold = 0.05
        if poor_calibration:
            base_threshold = 0.15
        elif remaining_slots <= 1:
            base_threshold = 0.15

        for assignment in assignments:
            paper_id = assignment.paper_id if hasattr(assignment, "paper_id") else assignment.get("paper_id", "")
            if not paper_id:
                continue
            if paper_id in already_reviewed:
                logger.debug("Skipping already-reviewed paper %s", paper_id)
                continue
            try:
                paper = client.get_paper(paper_id)
                paper_topics = set()
                if hasattr(paper, "metadata") and isinstance(paper.metadata, dict):
                    paper_topics = {t.lower() for t in paper.metadata.get("topics", [])}
                elif isinstance(paper, dict):
                    paper_topics = {t.lower() for t in paper.get("metadata", {}).get("topics", [])}

                title = paper.title if hasattr(paper, "title") else paper.get("title", "")
                title_words = {w.lower() for w in title.split() if len(w) > 3}
                paper_keywords = paper_topics | title_words

                # Jaccard similarity instead of any-word overlap
                jaccard = self._jaccard(my_keywords, paper_keywords) if my_keywords else 0.0

                # Allow new agents (no keywords) to review anything
                if my_keywords and jaccard < base_threshold:
                    logger.info(
                        "Declining review for %s (jaccard=%.3f < %.3f)",
                        paper_id, jaccard, base_threshold,
                    )
                    self._digest["reviews_declined_expertise"] += 1
                    continue

                self.researcher._do_review(paper)
                results.append({"paper_id": paper_id, "status": "reviewed"})

                self._thread_state.add_review(ReviewRecord(
                    paper_id=paper_id,
                    title=title,
                    decision="reviewed",
                    reviewed_at=time.time(),
                ))

                for t in paper_topics:
                    self._thread_state.add_discovered_topic(t)

                if self.follow_authors:
                    self._follow_paper_author(paper)

            except Exception as e:
                logger.warning("Review with expertise filter failed for %s: %s", paper_id, e)

        return results

    # ------------------------------------------------------------------
    # F5: Literature Reviews / Survey Papers
    # ------------------------------------------------------------------

    def _generate_survey_topic(self) -> str | None:
        """Synthesize a survey topic from recent readings."""
        recent = self._thread_state.get_recent_readings(20)
        if not recent:
            return None

        titles = [e.title for e in recent if e.title]
        interests = self.research_topics[:5] if self.research_topics else ["AI research"]

        prompt = (
            "You are a research agent planning a literature review / survey paper.\n\n"
            f"Your research interests: {', '.join(interests)}\n"
            f"Papers you've recently read:\n"
            + "\n".join(f"- {t}" for t in titles[:20])
            + "\n\nBased on these readings, suggest ONE specific survey topic that "
            "synthesizes common themes or debates across these papers.\n"
            "Reply with ONLY the topic title (one line, no numbering, no explanation)."
        )

        try:
            result = self.researcher.llm.generate(prompt, max_tokens=100)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            topic = text.strip('"').strip("'").split("\n")[0].strip()
            return topic if topic else None
        except Exception:
            return None

    def _publish_survey_paper(self, topic: str) -> dict:
        """Publish a survey/literature review paper."""
        if not topic.lower().startswith("literature review"):
            topic = f"Literature Review: {topic}"
        result = self.researcher.research_and_publish(topic)
        self._thread_state.reset_survey_counter()
        return result

    # ------------------------------------------------------------------
    # F6: Conference Participation
    # ------------------------------------------------------------------

    @staticmethod
    def _jaccard(set_a: set[str], set_b: set[str]) -> float:
        """Jaccard similarity between two keyword sets."""
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    def _check_conferences(self) -> int:
        """Submit to conferences with Jaccard topic matching and quality filter. Returns count submitted."""
        client = self.researcher.client
        try:
            data = client.list_conferences(status="call_for_papers")
        except Exception:
            return 0

        already_submitted = self._thread_state.get_conference_ids()
        conferences = data.get("conferences", [])
        now = datetime.now(timezone.utc)

        # Gather eligible papers: published/accepted or cited
        thread = self._thread_state.get_active_thread()
        eligible_papers: list[tuple[str, set[str]]] = []  # (paper_id, keywords)
        if thread and thread.papers:
            for p in thread.papers[-15:]:
                status_ok = p.final_status in ("published", "accepted", "")
                cited = p.citation_count > 0
                if status_ok or cited:
                    keywords = {w.lower() for w in p.topic.split() if len(w) > 3}
                    keywords.update(w.lower() for w in p.title.split() if len(w) > 3)
                    eligible_papers.append((p.paper_id, keywords))

        if not eligible_papers:
            return 0

        submitted = 0
        for conf in conferences:
            conf_id = conf.get("conference_id", "")
            if not conf_id or conf_id in already_submitted:
                continue
            deadline_str = conf.get("submission_deadline", "") or conf.get("end_date", "")
            if not deadline_str:
                continue
            try:
                deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if deadline <= now:
                continue

            # Build conference keyword set
            conf_topics = {t.lower() for t in conf.get("topics", [])}
            conf_title_words = {w.lower() for w in conf.get("title", "").split() if len(w) > 3}
            conf_keywords = conf_topics | conf_title_words

            # Conference selectivity: compute acceptance rate from available fields
            total_subs = conf.get("total_submissions", 0)
            accepted = conf.get("accepted_papers", 0)
            if total_subs > 0:
                acceptance_rate = accepted / total_subs
            else:
                acceptance_rate = 1.0  # unknown → low bar
            selective = acceptance_rate < 0.3

            # Find best matching paper by Jaccard
            best_paper_id = ""
            best_jaccard = 0.0
            for pid, ptopics in eligible_papers:
                j = self._jaccard(ptopics, conf_keywords)
                if j > best_jaccard:
                    best_jaccard = j
                    best_paper_id = pid

            # Minimum Jaccard threshold
            min_jaccard = 0.15 if selective else 0.1
            if not best_paper_id or best_jaccard < min_jaccard:
                continue

            try:
                client.submit_to_conference(conf_id, best_paper_id)
                self._conferences_submitted_today += 1
                submitted += 1
                self._thread_state.add_conference_submission(ConferenceRecord(
                    conference_id=conf_id,
                    conference_name=conf.get("title", ""),
                    paper_id=best_paper_id,
                    submitted_at=time.time(),
                ))
                logger.info(
                    "Submitted %s to conference %s (jaccard=%.2f)",
                    best_paper_id, conf_id, best_jaccard,
                )
                return submitted  # max 1 per cycle
            except Exception as e:
                logger.warning("Conference submission failed: %s", e)

        return submitted

    # ------------------------------------------------------------------
    # F7: Profile Evolution
    # ------------------------------------------------------------------

    def _evolve_profile(self) -> None:
        """Update agent bio and research interests based on recent work."""
        thread = self._thread_state.get_active_thread()
        if not thread or not thread.papers:
            return

        recent = thread.papers[-10:]
        titles = [p.title for p in recent]
        findings = []
        for p in recent[-5:]:
            findings.extend(p.key_findings[:3])

        prompt = (
            "You are updating a researcher's public profile based on their recent work.\n\n"
            f"Recent paper titles:\n" + "\n".join(f"- {t}" for t in titles) + "\n\n"
            f"Key findings:\n" + "\n".join(f"- {f}" for f in findings[:10]) + "\n\n"
            "Generate:\n"
            "1. A bio (2-3 sentences, third person, e.g. 'This agent specializes in...')\n"
            "2. A list of 3-7 research interests (short phrases)\n\n"
            "Reply as JSON: {\"bio\": \"...\", \"research_interests\": [\"...\", ...]}"
        )

        try:
            result = self.researcher.llm.generate(prompt, max_tokens=500)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                bio = data.get("bio", "")
                interests = data.get("research_interests", [])
                if bio and interests:
                    self.researcher.client.update_agent_profile({
                        "profile.bio": bio,
                        "profile.research_interests": interests,
                    })
                    logger.info("Profile evolved: bio=%s..., interests=%s", bio[:50], interests)
        except Exception as e:
            logger.warning("Profile evolution LLM failed: %s", e)

    # ------------------------------------------------------------------
    # F8: Replication Attempts
    # ------------------------------------------------------------------

    def _attempt_replication(self) -> int:
        """Attempt to replicate the most impactful paper in the agent's domain."""
        client = self.researcher.client
        agent_id = client.get_my_agent_id()
        if not agent_id:
            return 0

        total_papers = self._thread_state.total_papers_published

        # Search for papers in our domain
        query = self.research_topics[0] if self.research_topics else "AI research"
        try:
            results = client.search(query, top_k=20)
        except Exception:
            return 0

        # Get existing replications to avoid re-doing
        replicated_originals: set[str] = set()
        try:
            reps = client.list_replications(agent_id=agent_id)
            for r in reps.get("replications", []):
                replicated_originals.add(r.get("original_paper_id", ""))
        except Exception:
            pass

        # Sort by citation count (most impactful first)
        def _get_cite_count(p) -> int:
            # Try top-level citation_count first, then nested citation_stats
            if hasattr(p, "citation_count") and p.citation_count:
                return p.citation_count
            if hasattr(p, "citation_stats") and isinstance(p.citation_stats, dict):
                return p.citation_stats.get("cited_by_count", 0)
            if isinstance(p, dict):
                ct = p.get("citation_count", 0)
                if ct:
                    return ct
                return p.get("citation_stats", {}).get("cited_by_count", 0)
            return 0

        sorted_results = sorted(results, key=_get_cite_count, reverse=True)

        # Build candidate list, then pick randomly from top to avoid thundering herd
        viable_targets: list = []
        for paper in sorted_results:
            pid = paper.paper_id if hasattr(paper, "paper_id") else paper.get("paper_id", "")
            if pid in replicated_originals:
                continue
            # Skip own papers
            author_id = ""
            if hasattr(paper, "authors") and paper.authors:
                a0 = paper.authors[0]
                author_id = a0 if isinstance(a0, str) else a0.get("agent_id", "")
            if author_id == agent_id:
                continue
            # Skip uncited papers for established agents
            if total_papers > 3 and _get_cite_count(paper) == 0:
                continue
            # Skip papers with 2+ existing replications (avoid pile-on)
            rep_count = 0
            try:
                pid_val = paper.paper_id if hasattr(paper, "paper_id") else paper.get("paper_id", "")
                reps_data = client.list_replications(paper_id=pid_val)
                rep_count = len(reps_data.get("replications", []))
            except Exception:
                pass
            if rep_count >= 2:
                continue
            viable_targets.append(paper)
            if len(viable_targets) >= 20:
                break

        if not viable_targets:
            return 0

        # Pick randomly from top candidates to spread replication effort
        target = random.choice(viable_targets)

        if not target:
            return 0

        try:
            # Start replication
            rep_data = client.start_replication(target.paper_id)
            replication_id = rep_data.get("replication_id", "")
            if not replication_id:
                return 0

            # Generate replication findings via LLM
            title = target.title
            abstract = target.abstract if hasattr(target, "abstract") else ""

            prompt = (
                "You are attempting to replicate a research paper.\n\n"
                f"Title: {title}\n"
                f"Abstract: {abstract}\n\n"
                "Based on the methodology described, generate a replication report:\n"
                "1. Replication status: 'replicated', 'partially_replicated', or 'failed_to_replicate'\n"
                "2. Findings: what you observed when following the methodology\n"
                "3. Methodology notes: any deviations or challenges\n\n"
                "Reply as JSON: {\"status\": \"...\", \"findings\": \"...\", \"methodology_notes\": \"...\"}"
            )

            result = self.researcher.llm.generate(prompt, max_tokens=1000)
            text = result.strip() if isinstance(result, str) else result.text.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                rep_result = json.loads(text[start:end])
                client.submit_replication_result(
                    replication_id,
                    status=rep_result.get("status", "partially_replicated"),
                    findings=rep_result.get("findings", ""),
                    methodology_notes=rep_result.get("methodology_notes", ""),
                )
                self._replications_today += 1
                logger.info("Replication attempt completed for %s", target.paper_id)
                return 1
        except Exception as e:
            logger.warning("Replication attempt failed: %s", e)

        return 0

    # ------------------------------------------------------------------
    # F9: Strategic Topic Selection
    # ------------------------------------------------------------------

    def _pick_trending_topic(self) -> str | None:
        """Pick a trending topic from the platform that matches agent interests."""
        client = self.researcher.client
        try:
            data = client.get_trending(window="week")
        except Exception:
            return None

        # Prefer personalised suggestions
        suggested = data.get("suggested_for_you", [])
        if suggested:
            pick = random.choice(suggested)
            topic = pick if isinstance(pick, str) else pick.get("topic", "")
            if topic:
                return topic

        # Fallback: filter trending_topics by overlap with interests
        trending = data.get("trending_topics", [])
        if not trending:
            return None

        all_interests = self._thread_state.get_all_interests(self.research_topics or [])
        my_keywords = {w.lower() for t in all_interests for w in t.split() if len(w) > 3}
        matching = []
        for t in trending:
            topic_str = t if isinstance(t, str) else t.get("topic", "")
            topic_words = {w.lower() for w in topic_str.split() if len(w) > 3}
            if my_keywords & topic_words:
                matching.append(topic_str)

        if matching:
            # Tier 3a: weight matching topics by past success (70% best / 30% random)
            success_rates = self._thread_state.get_topic_success_rates()
            scored = []
            for topic_str in matching:
                words = {w.lower() for w in topic_str.split() if len(w) > 3}
                best_score = max(
                    (success_rates.get(t, 0.0) for t in success_rates if t.lower() in words),
                    default=0.0,
                )
                scored.append((best_score, topic_str))
            scored.sort(reverse=True)
            if scored and scored[0][0] > 0 and random.random() < 0.7:
                return scored[0][1]
            return random.choice(matching)

        return None

    # ------------------------------------------------------------------
    # Tier 2: Paper Impact Polling
    # ------------------------------------------------------------------

    def _poll_paper_impact(self) -> int:
        """Poll impact metrics for recently published papers. Returns count polled."""
        client = self.researcher.client
        recent = self._thread_state.get_recent_papers(20)
        if not recent:
            return 0

        polled = 0
        for paper_rec in recent:
            try:
                paper = client.get_paper(paper_rec.paper_id)
                # Extract citation count and status
                if hasattr(paper, "citation_count"):
                    cites = paper.citation_count
                elif isinstance(paper, dict):
                    cites = paper.get("citation_count", 0)
                else:
                    cites = 0

                if hasattr(paper, "status"):
                    status = paper.status
                elif isinstance(paper, dict):
                    status = paper.get("status", "")
                else:
                    status = ""

                # Count discussions
                disc_count = 0
                try:
                    disc_data = client.get_discussions(paper_rec.paper_id, view="flat")
                    disc_count = len(disc_data.get("discussions", []))
                except Exception:
                    pass

                self._thread_state.update_paper_impact(
                    paper_rec.paper_id,
                    citation_count=cites,
                    discussion_count=disc_count,
                    final_status=status,
                )

                # Tier 3a: upsert topic outcome
                self._thread_state.upsert_topic_outcome(TopicOutcome(
                    topic=paper_rec.topic,
                    paper_id=paper_rec.paper_id,
                    citation_count=cites,
                    discussion_count=disc_count,
                    final_status=status,
                    measured_at=time.time(),
                ))

                # Tier 4: poll received reviews for feedback loop
                if self.feedback_loop:
                    try:
                        reviews = client.get_reviews_for_paper(paper_rec.paper_id)
                        existing_reviewer_ids = {
                            r.reviewer_agent_id
                            for r in self._thread_state.received_reviews
                            if r.paper_id == paper_rec.paper_id
                        }
                        for rev in reviews:
                            reviewer_id = rev.get("reviewer_agent_id", rev.get("agent_id", ""))
                            if reviewer_id in existing_reviewer_ids:
                                continue  # deduplicate
                            self._thread_state.add_received_review(ReceivedReviewRecord(
                                paper_id=paper_rec.paper_id,
                                reviewer_agent_id=reviewer_id,
                                decision=rev.get("decision", ""),
                                scores=rev.get("scores", {}),
                                weaknesses=rev.get("weaknesses", []),
                                strengths=rev.get("strengths", []),
                                summary=rev.get("summary", ""),
                                received_at=time.time(),
                            ))
                    except Exception as e:
                        logger.debug("Review poll failed for %s: %s", paper_rec.paper_id, e)

                polled += 1
            except Exception as e:
                logger.debug("Impact poll failed for %s: %s", paper_rec.paper_id, e)

        # Tier 3b: update reviewed paper statuses
        for review_rec in self._thread_state.reviewed_papers:
            if review_rec.paper_final_status:
                continue
            try:
                paper = client.get_paper(review_rec.paper_id)
                status = ""
                if hasattr(paper, "status"):
                    status = paper.status
                elif isinstance(paper, dict):
                    status = paper.get("status", "")
                if status in ("published", "accepted", "rejected", "withdrawn"):
                    review_rec.paper_final_status = status
            except Exception:
                pass

        self._thread_state.last_impact_poll = time.time()
        self._thread_state.save()
        return polled

    # ------------------------------------------------------------------
    # Daily digest
    # ------------------------------------------------------------------

    def _write_daily_digest(self) -> None:
        """Persist a JSON digest of today's activity."""
        if not any(v for v in self._digest.values() if isinstance(v, int) and v > 0):
            return  # nothing to write

        _DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _DIGEST_DIR / f"digest-{date_str}.json"

        # Thread progress
        thread = self._thread_state.get_active_thread()
        self._digest["thread_progress"] = {
            "thread_id": thread.thread_id if thread else "",
            "papers_in_thread": len(thread.papers) if thread else 0,
            "next_direction": thread.current_direction if thread else "",
        }
        self._digest["resource_stats"] = self._monitor.get_stats()
        self._digest["date"] = date_str

        # Include latest platform suggestions snapshot
        try:
            suggestions = self.researcher.client.get_suggestions()
            self._digest["suggestions"] = {
                "top_topics": [t["topic"] for t in suggestions.get("research_topics", [])[:5]],
                "papers_to_review": len(suggestions.get("papers_to_review", [])),
                "active_challenges": len(suggestions.get("active_challenges", [])),
                "credit_balance": suggestions.get("credit_balance", 0),
            }
        except Exception:
            pass

        # Enriched metrics computed from persistent state
        self._digest["discovered_topics_count"] = len(self._thread_state.discovered_topics)
        self._digest["unanswered_comments"] = len(self._thread_state.get_unanswered_comments())
        self._digest["expertise_breadth"] = len(
            self._thread_state.get_all_interests(self.research_topics or [])
        )
        self._digest["challenges_entered_total"] = len(self._thread_state.challenge_history)
        self._digest["conferences_submitted_total"] = len(self._thread_state.conference_history)
        self._digest["collaborations_total"] = len(self._thread_state.collaboration_history)
        self._digest["followed_authors_count"] = len(self._thread_state.followed_agents)

        # Top cited papers
        recent = self._thread_state.get_recent_papers(50)
        top_cited = sorted(recent, key=lambda p: p.citation_count, reverse=True)[:5]
        self._digest["top_cited_papers"] = [
            {"paper_id": p.paper_id, "title": p.title, "citations": p.citation_count}
            for p in top_cited if p.citation_count > 0
        ]

        # Review calibration snapshot
        calibration = self._thread_state.get_review_calibration()
        if calibration["total_calibrated"] > 0:
            self._digest["review_calibration"] = calibration

        try:
            path.write_text(json.dumps(self._digest, indent=2, default=str), encoding="utf-8")
            logger.info("Daily digest written: %s", path)
        except Exception as e:
            logger.warning("Failed to write digest: %s", e)


def _empty_digest() -> dict:
    """Return a fresh daily digest counter dict."""
    return {
        "papers_published": 0,
        "reviews_completed": 0,
        "revisions_submitted": 0,
        "collaborations_accepted": 0,
        "challenges_entered": 0,
        "notifications_processed": 0,
        "discussions_posted": 0,
        "discussion_replies_posted": 0,
        "author_responses_posted": 0,
        "reviews_declined_expertise": 0,
        "survey_papers_published": 0,
        "conference_submissions": 0,
        "profile_updates": 0,
        "replications_attempted": 0,
        "papers_impact_polled": 0,
    }
