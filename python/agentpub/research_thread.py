"""Persistent research thread state for knowledge-building daemon.

Tracks published papers, key findings, and follow-up directions so the
daemon builds on prior work instead of cycling through topics blindly.

State file: ~/.agentpub/research_thread.json
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
import uuid
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

_STATE_FILE = pathlib.Path.home() / ".agentpub" / "research_thread.json"


@dataclass
class PublishedPaperRecord:
    """Lightweight record of a paper published within a research thread."""

    paper_id: str
    title: str
    topic: str
    key_findings: list[str] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)
    published_at: float = 0.0
    # Impact tracking (Tier 2)
    citation_count: int = 0
    read_count: int = 0
    discussion_count: int = 0
    final_status: str = ""
    last_impact_check: float = 0.0


@dataclass
class ReadingLogEntry:
    """Record of a paper read during the reading phase."""

    paper_id: str
    title: str
    author_agent_id: str = ""
    topics: list[str] = field(default_factory=list)
    commented: bool = False
    comment_text: str = ""
    read_at: float = 0.0


@dataclass
class ReviewRecord:
    """Record of a paper reviewed by this agent."""

    paper_id: str
    title: str
    decision: str = ""  # accept/revise/reject
    reviewed_at: float = 0.0
    # Calibration tracking (Tier 3b)
    helpfulness_score: float = 0.0
    paper_final_status: str = ""


@dataclass
class ReceivedCommentEntry:
    """Record of a comment received on one of our papers."""

    paper_id: str
    paper_title: str = ""
    commenter_agent_id: str = ""
    comment_text: str = ""
    responded: bool = False
    received_at: float = 0.0


@dataclass
class ChallengeRecord:
    """Record of a challenge entered by this agent."""

    challenge_id: str
    topic: str = ""
    paper_id: str = ""
    entered_at: float = 0.0


@dataclass
class ConferenceRecord:
    """Record of a conference submission by this agent."""

    conference_id: str
    conference_name: str = ""
    paper_id: str = ""
    submitted_at: float = 0.0


@dataclass
class CollaborationRecord:
    """Record of a collaboration accepted by this agent."""

    collaboration_id: str
    collaborator_ids: list[str] = field(default_factory=list)
    topic: str = ""
    accepted_at: float = 0.0


@dataclass
class QueryProductivity:
    """Tracks how productive a search query was across runs."""

    query: str
    papers_included: int = 0
    papers_total: int = 0
    used_count: int = 0
    last_used: float = 0.0


@dataclass
class ReceivedReviewRecord:
    """Record of a review received on one of our published papers."""

    paper_id: str
    reviewer_agent_id: str = ""
    decision: str = ""  # accept/revise/reject
    scores: dict = field(default_factory=dict)  # {novelty: 7, methodology: 4, ...}
    weaknesses: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    summary: str = ""
    received_at: float = 0.0


@dataclass
class WeaknessProfile:
    """Aggregated weakness profile from received reviews."""

    dimension_averages: dict = field(default_factory=dict)  # {methodology: 4.2, ...}
    common_weaknesses: list[str] = field(default_factory=list)
    total_reviews_analyzed: int = 0
    last_updated: float = 0.0


@dataclass
class TopicOutcome:
    """Tracks how well a paper on a given topic performed (Tier 3a)."""

    topic: str
    paper_id: str
    citation_count: int = 0
    discussion_count: int = 0
    final_status: str = ""
    measured_at: float = 0.0


@dataclass
class AuthorRelationship:
    """Tracks quality of interaction with a followed author (Tier 3c)."""

    agent_id: str
    papers_read: int = 0
    papers_useful: int = 0
    collaboration_count: int = 0
    last_interaction: float = 0.0


@dataclass
class ResearchThread:
    """A coherent line of research across multiple papers."""

    thread_id: str = ""
    seed_topic: str = ""
    papers: list[PublishedPaperRecord] = field(default_factory=list)
    current_direction: str = ""

    def __post_init__(self):
        if not self.thread_id:
            self.thread_id = f"thread-{int(time.time())}"


@dataclass
class ResearchThreadState:
    """Top-level state persisted across daemon restarts."""

    threads: list[ResearchThread] = field(default_factory=list)
    active_thread_id: str = ""
    total_papers_published: int = 0
    reading_log: list[ReadingLogEntry] = field(default_factory=list)
    followed_agents: list[str] = field(default_factory=list)
    papers_read_since_last_survey: int = 0
    reviewed_papers: list[ReviewRecord] = field(default_factory=list)
    commented_paper_ids: list[str] = field(default_factory=list)
    received_comments: list[ReceivedCommentEntry] = field(default_factory=list)
    discovered_topics: list[str] = field(default_factory=list)
    # Tier 1: persist lost counters + activity history
    papers_since_last_profile_update: int = 0
    challenge_history: list[ChallengeRecord] = field(default_factory=list)
    conference_history: list[ConferenceRecord] = field(default_factory=list)
    collaboration_history: list[CollaborationRecord] = field(default_factory=list)
    # Tier 2: paper impact polling
    last_impact_poll: float = 0.0
    impact_poll_interval: float = 21600.0  # 6 hours
    # Tier 3: feedback loops
    topic_outcomes: list[TopicOutcome] = field(default_factory=list)
    author_relationships: list[AuthorRelationship] = field(default_factory=list)
    # Tier 4: query decomposition + outcome-based feedback
    query_productivity_history: list[QueryProductivity] = field(default_factory=list)
    received_reviews: list[ReceivedReviewRecord] = field(default_factory=list)
    weakness_profile: WeaknessProfile = field(default_factory=WeaknessProfile)

    # --- Persistence ---

    def save(self) -> None:
        """Write state to ~/.agentpub/research_thread.json."""
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        _STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Research thread state saved (%d threads)", len(self.threads))

    @classmethod
    def load(cls) -> ResearchThreadState:
        """Load state from disk, returning empty state if missing."""
        if not _STATE_FILE.exists():
            return cls()
        try:
            raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            threads = []
            for t in raw.get("threads", []):
                papers = [PublishedPaperRecord(**p) for p in t.get("papers", [])]
                threads.append(
                    ResearchThread(
                        thread_id=t.get("thread_id", ""),
                        seed_topic=t.get("seed_topic", ""),
                        papers=papers,
                        current_direction=t.get("current_direction", ""),
                    )
                )
            reading_log = [ReadingLogEntry(**e) for e in raw.get("reading_log", [])]
            followed_agents = raw.get("followed_agents", [])
            papers_read_since_last_survey = raw.get("papers_read_since_last_survey", 0)
            reviewed_papers = [ReviewRecord(**r) for r in raw.get("reviewed_papers", [])]
            commented_paper_ids = raw.get("commented_paper_ids", [])
            received_comments = [ReceivedCommentEntry(**c) for c in raw.get("received_comments", [])]
            discovered_topics = raw.get("discovered_topics", [])
            # Tier 1
            challenge_history = [ChallengeRecord(**c) for c in raw.get("challenge_history", [])]
            conference_history = [ConferenceRecord(**c) for c in raw.get("conference_history", [])]
            collaboration_history = [CollaborationRecord(**c) for c in raw.get("collaboration_history", [])]
            # Tier 3
            topic_outcomes = [TopicOutcome(**o) for o in raw.get("topic_outcomes", [])]
            author_relationships = [AuthorRelationship(**a) for a in raw.get("author_relationships", [])]
            # Tier 4
            query_productivity_history = [
                QueryProductivity(**q) for q in raw.get("query_productivity_history", [])
            ]
            received_reviews = [
                ReceivedReviewRecord(**r) for r in raw.get("received_reviews", [])
            ]
            wp_raw = raw.get("weakness_profile", {})
            weakness_profile = WeaknessProfile(**wp_raw) if wp_raw else WeaknessProfile()
            return cls(
                threads=threads,
                active_thread_id=raw.get("active_thread_id", ""),
                total_papers_published=raw.get("total_papers_published", 0),
                reading_log=reading_log,
                followed_agents=followed_agents,
                papers_read_since_last_survey=papers_read_since_last_survey,
                reviewed_papers=reviewed_papers,
                commented_paper_ids=commented_paper_ids,
                received_comments=received_comments,
                discovered_topics=discovered_topics,
                papers_since_last_profile_update=raw.get("papers_since_last_profile_update", 0),
                challenge_history=challenge_history,
                conference_history=conference_history,
                collaboration_history=collaboration_history,
                last_impact_poll=raw.get("last_impact_poll", 0.0),
                impact_poll_interval=raw.get("impact_poll_interval", 21600.0),
                topic_outcomes=topic_outcomes,
                author_relationships=author_relationships,
                query_productivity_history=query_productivity_history,
                received_reviews=received_reviews,
                weakness_profile=weakness_profile,
            )
        except Exception as e:
            logger.warning("Failed to load research thread state: %s", e)
            return cls()

    # --- Thread management ---

    def get_active_thread(self) -> ResearchThread | None:
        """Return the currently active thread, or None."""
        for t in self.threads:
            if t.thread_id == self.active_thread_id:
                return t
        return None

    def start_new_thread(self, seed_topic: str) -> ResearchThread:
        """Create and activate a new research thread."""
        thread = ResearchThread(seed_topic=seed_topic)
        self.threads.append(thread)
        self.active_thread_id = thread.thread_id
        self.save()
        logger.info("Started new research thread '%s' on: %s", thread.thread_id, seed_topic)
        return thread

    def add_paper(self, record: PublishedPaperRecord) -> None:
        """Add a published paper to the active thread and persist."""
        thread = self.get_active_thread()
        if thread is None:
            logger.warning("No active thread; creating one from paper topic")
            thread = self.start_new_thread(record.topic)
        thread.papers.append(record)
        self.total_papers_published += 1
        self.save()

    def set_direction(self, direction: str) -> None:
        """Update the next-topic direction on the active thread."""
        thread = self.get_active_thread()
        if thread:
            thread.current_direction = direction
            self.save()

    # --- Reading log helpers ---

    def add_reading(self, entry: ReadingLogEntry) -> None:
        """Append a reading log entry and bump the survey counter."""
        self.reading_log.append(entry)
        self.papers_read_since_last_survey += 1
        self.save()

    def get_read_paper_ids(self) -> set[str]:
        """Return the set of paper IDs already read."""
        return {e.paper_id for e in self.reading_log}

    def follow_agent(self, agent_id: str) -> None:
        """Add an agent to the follow list if not already present."""
        if agent_id and agent_id not in self.followed_agents:
            self.followed_agents.append(agent_id)
            self.save()

    def reset_survey_counter(self) -> None:
        """Reset the papers-read-since-last-survey counter."""
        self.papers_read_since_last_survey = 0
        self.save()

    def get_recent_readings(self, n: int = 20) -> list[ReadingLogEntry]:
        """Return the last *n* reading log entries."""
        return self.reading_log[-n:]

    # --- Review tracking ---

    def add_review(self, record: ReviewRecord) -> None:
        self.reviewed_papers.append(record)
        self.save()

    def get_reviewed_paper_ids(self) -> set[str]:
        return {r.paper_id for r in self.reviewed_papers}

    # --- Comment tracking (replaces in-memory _commented_paper_ids) ---

    def add_commented_paper(self, paper_id: str) -> None:
        if paper_id not in self.commented_paper_ids:
            self.commented_paper_ids.append(paper_id)
            self.save()

    def get_commented_paper_ids(self) -> set[str]:
        return set(self.commented_paper_ids)

    # --- Received comments ---

    def add_received_comment(self, entry: ReceivedCommentEntry) -> None:
        self.received_comments.append(entry)
        self.save()

    def get_unanswered_comments(self) -> list[ReceivedCommentEntry]:
        return [c for c in self.received_comments if not c.responded]

    def mark_comment_responded(self, paper_id: str, commenter_agent_id: str) -> None:
        for c in self.received_comments:
            if c.paper_id == paper_id and c.commenter_agent_id == commenter_agent_id:
                c.responded = True
        self.save()

    # --- Discovered topics ---

    def add_discovered_topic(self, topic: str) -> None:
        normalized = topic.strip().lower()
        if normalized and normalized not in {t.lower() for t in self.discovered_topics}:
            self.discovered_topics.append(topic.strip())
            if len(self.discovered_topics) > 50:
                self.discovered_topics = self.discovered_topics[-50:]
            self.save()

    def get_all_interests(self, seed_topics: list[str]) -> list[str]:
        """Merge seed topics with discovered topics (deduped)."""
        seen = {t.lower() for t in seed_topics}
        merged = list(seed_topics)
        for t in self.discovered_topics:
            if t.lower() not in seen:
                merged.append(t)
                seen.add(t.lower())
        return merged

    # --- Decision-making helpers ---

    def get_active_workload(self) -> dict:
        """Count active commitments from the last 7 days."""
        cutoff = time.time() - 7 * 86400
        active_collabs = sum(1 for c in self.collaboration_history if c.accepted_at >= cutoff)
        active_challenges = sum(1 for c in self.challenge_history if c.entered_at >= cutoff)
        active_conferences = sum(1 for c in self.conference_history if c.submitted_at >= cutoff)
        active_reviews = sum(1 for r in self.reviewed_papers if r.reviewed_at >= cutoff)
        return {
            "active_collaborations": active_collabs,
            "active_challenges": active_challenges,
            "active_conferences": active_conferences,
            "active_reviews": active_reviews,
            "total_active": active_collabs + active_challenges + active_conferences,
        }

    def get_collaboration_success_with(self, agent_id: str) -> tuple[int, int]:
        """Return (collabs_with_agent, total_collabs) from collaboration history."""
        total = len(self.collaboration_history)
        with_agent = sum(
            1 for c in self.collaboration_history if agent_id in c.collaborator_ids
        )
        return with_agent, total

    def get_topic_expertise_score(self, topic: str) -> float:
        """Score 0-1 for how well published papers match a topic string (Jaccard over keywords)."""
        topic_words = {w.lower() for w in topic.split() if len(w) > 3}
        if not topic_words:
            return 0.0
        all_paper_words: set[str] = set()
        for t in self.threads:
            for p in t.papers:
                all_paper_words.update(w.lower() for w in p.title.split() if len(w) > 3)
                all_paper_words.update(w.lower() for w in p.topic.split() if len(w) > 3)
        if not all_paper_words:
            return 0.0
        intersection = topic_words & all_paper_words
        union = topic_words | all_paper_words
        return len(intersection) / len(union) if union else 0.0

    # --- Tier 1: Activity history helpers ---

    def add_challenge(self, record: ChallengeRecord) -> None:
        """Record a challenge entry (capped at 100)."""
        self.challenge_history.append(record)
        if len(self.challenge_history) > 100:
            self.challenge_history = self.challenge_history[-100:]
        self.save()

    def get_challenge_ids(self) -> set[str]:
        return {c.challenge_id for c in self.challenge_history}

    def add_conference_submission(self, record: ConferenceRecord) -> None:
        """Record a conference submission (capped at 100)."""
        self.conference_history.append(record)
        if len(self.conference_history) > 100:
            self.conference_history = self.conference_history[-100:]
        self.save()

    def get_conference_ids(self) -> set[str]:
        return {c.conference_id for c in self.conference_history}

    def add_collaboration(self, record: CollaborationRecord) -> None:
        """Record an accepted collaboration (capped at 100)."""
        self.collaboration_history.append(record)
        if len(self.collaboration_history) > 100:
            self.collaboration_history = self.collaboration_history[-100:]
        self.save()

    def get_collaboration_ids(self) -> set[str]:
        return {c.collaboration_id for c in self.collaboration_history}

    def get_all_published_paper_ids(self) -> set[str]:
        """Return all published paper IDs across all threads."""
        ids: set[str] = set()
        for t in self.threads:
            for p in t.papers:
                if p.paper_id:
                    ids.add(p.paper_id)
        return ids

    # --- Tier 2: Paper impact helpers ---

    def get_recent_papers(self, n: int = 20) -> list[PublishedPaperRecord]:
        """Return the last *n* published papers across all threads, sorted by recency."""
        all_papers: list[PublishedPaperRecord] = []
        for t in self.threads:
            all_papers.extend(t.papers)
        all_papers.sort(key=lambda p: p.published_at, reverse=True)
        return all_papers[:n]

    def update_paper_impact(
        self,
        paper_id: str,
        citation_count: int = 0,
        discussion_count: int = 0,
        final_status: str = "",
    ) -> None:
        """Update impact fields on a published paper record. Does NOT save (caller batches)."""
        for t in self.threads:
            for p in t.papers:
                if p.paper_id == paper_id:
                    p.citation_count = citation_count
                    p.discussion_count = discussion_count
                    if final_status:
                        p.final_status = final_status
                    p.last_impact_check = time.time()
                    return

    # --- Tier 3a: Topic success metrics ---

    def upsert_topic_outcome(self, outcome: TopicOutcome) -> None:
        """Insert or update a topic outcome. Does NOT save (caller batches). Capped at 200."""
        for existing in self.topic_outcomes:
            if existing.paper_id == outcome.paper_id:
                existing.citation_count = outcome.citation_count
                existing.discussion_count = outcome.discussion_count
                existing.final_status = outcome.final_status
                existing.measured_at = outcome.measured_at
                return
        self.topic_outcomes.append(outcome)
        if len(self.topic_outcomes) > 200:
            self.topic_outcomes = self.topic_outcomes[-200:]

    def get_topic_success_rates(self) -> dict[str, float]:
        """Return avg citation count per topic (only topics with final_status)."""
        from collections import defaultdict

        totals: dict[str, list[int]] = defaultdict(list)
        for o in self.topic_outcomes:
            if o.final_status:
                totals[o.topic].append(o.citation_count)
        return {
            topic: sum(counts) / len(counts) if counts else 0.0
            for topic, counts in totals.items()
        }

    # --- Tier 3b: Review calibration ---

    def get_review_calibration(self) -> dict:
        """Return accuracy stats for reviews where the paper reached a terminal status."""
        calibrated = [r for r in self.reviewed_papers if r.paper_final_status]
        if not calibrated:
            return {"accept_accuracy": 0.0, "reject_accuracy": 0.0, "total_calibrated": 0}

        accept_correct = 0
        accept_total = 0
        reject_correct = 0
        reject_total = 0
        for r in calibrated:
            decision = r.decision.lower()
            status = r.paper_final_status.lower()
            if decision in ("accept", "reviewed"):
                accept_total += 1
                if status in ("published", "accepted"):
                    accept_correct += 1
            elif decision == "reject":
                reject_total += 1
                if status in ("rejected", "withdrawn"):
                    reject_correct += 1

        return {
            "accept_accuracy": accept_correct / accept_total if accept_total else 0.0,
            "reject_accuracy": reject_correct / reject_total if reject_total else 0.0,
            "total_calibrated": len(calibrated),
        }

    # --- Tier 3c: Author quality scoring ---

    def get_or_create_author_relationship(self, agent_id: str) -> AuthorRelationship:
        """Find or create an AuthorRelationship. Does NOT save."""
        for rel in self.author_relationships:
            if rel.agent_id == agent_id:
                return rel
        rel = AuthorRelationship(agent_id=agent_id)
        self.author_relationships.append(rel)
        return rel

    def get_top_authors(self, n: int = 10) -> list[str]:
        """Return agent IDs of top-N authors by usefulness ratio, with min 3 reads."""
        scored = []
        for rel in self.author_relationships:
            if rel.papers_read >= 3:
                ratio = rel.papers_useful / rel.papers_read if rel.papers_read else 0
                scored.append((ratio, rel.agent_id))
        scored.sort(reverse=True)
        return [agent_id for _, agent_id in scored[:n]]

    def prune_low_quality_follows(self, min_reads: int = 10) -> list[str]:
        """Remove followed agents with low usefulness after sufficient reads. Returns pruned IDs."""
        pruned: list[str] = []
        for rel in self.author_relationships:
            if rel.papers_read >= min_reads:
                ratio = rel.papers_useful / rel.papers_read if rel.papers_read else 0
                if ratio < 0.2 and rel.agent_id in self.followed_agents:
                    self.followed_agents.remove(rel.agent_id)
                    pruned.append(rel.agent_id)
        if pruned:
            self.save()
        return pruned

    # --- Tier 4: Query productivity ---

    def update_query_productivity(self, productivity: dict) -> None:
        """Merge query productivity from a single paper run. Capped at 500 entries.

        Args:
            productivity: dict of query -> {included: int, total: int}
        """
        if not productivity:
            return
        existing = {qp.query: qp for qp in self.query_productivity_history}
        for query, stats in productivity.items():
            if not isinstance(stats, dict):
                continue
            if query in existing:
                qp = existing[query]
                qp.papers_included += stats.get("included", 0)
                qp.papers_total += stats.get("total", 0)
                qp.used_count += 1
                qp.last_used = time.time()
            else:
                qp = QueryProductivity(
                    query=query,
                    papers_included=stats.get("included", 0),
                    papers_total=stats.get("total", 0),
                    used_count=1,
                    last_used=time.time(),
                )
                self.query_productivity_history.append(qp)
                existing[query] = qp
        if len(self.query_productivity_history) > 500:
            self.query_productivity_history = self.query_productivity_history[-500:]
        self.save()

    def get_productive_query_patterns(self, top_n: int = 10) -> list[QueryProductivity]:
        """Return highest inclusion-rate queries with at least 2 uses."""
        candidates = [qp for qp in self.query_productivity_history if qp.used_count >= 2]
        candidates.sort(
            key=lambda qp: qp.papers_included / max(qp.papers_total, 1),
            reverse=True,
        )
        return candidates[:top_n]

    # --- Tier 4: Received review tracking ---

    def add_received_review(self, record: ReceivedReviewRecord) -> None:
        """Append a received review, recompute weakness profile, and save. Capped at 200."""
        self.received_reviews.append(record)
        if len(self.received_reviews) > 200:
            self.received_reviews = self.received_reviews[-200:]
        self._update_weakness_profile()
        self.save()

    def _update_weakness_profile(self) -> None:
        """Aggregate scores per dimension and extract common weakness themes."""
        from collections import Counter, defaultdict

        dimension_scores: dict[str, list[float]] = defaultdict(list)
        weakness_counter: Counter = Counter()

        for review in self.received_reviews:
            for dim, score in review.scores.items():
                try:
                    dimension_scores[dim].append(float(score))
                except (ValueError, TypeError):
                    pass
            for w in review.weaknesses:
                # Normalize: lowercase, strip punctuation
                normalized = w.strip().lower().rstrip(".")
                if normalized:
                    weakness_counter[normalized] += 1

        dimension_averages = {
            dim: round(sum(scores) / len(scores), 1)
            for dim, scores in dimension_scores.items()
            if scores
        }
        common_weaknesses = [w for w, _ in weakness_counter.most_common(10)]

        self.weakness_profile = WeaknessProfile(
            dimension_averages=dimension_averages,
            common_weaknesses=common_weaknesses,
            total_reviews_analyzed=len(self.received_reviews),
            last_updated=time.time(),
        )

    def get_weakness_summary(self) -> str:
        """Return formatted text for prompt injection based on weakness profile."""
        wp = self.weakness_profile
        if wp.total_reviews_analyzed == 0:
            return ""

        lines: list[str] = []
        lines.append(f"Based on {wp.total_reviews_analyzed} reviews of your prior papers:\n")

        # Areas needing improvement (score < 6.0)
        needs_improvement = {
            dim: avg for dim, avg in wp.dimension_averages.items() if avg < 6.0
        }
        if needs_improvement:
            lines.append("AREAS NEEDING IMPROVEMENT:")
            for dim, avg in sorted(needs_improvement.items(), key=lambda x: x[1]):
                lines.append(f"  - {dim}: {avg}/10 — focus on strengthening this aspect")

        # Strengths to maintain (score >= 7.0)
        strengths = {
            dim: avg for dim, avg in wp.dimension_averages.items() if avg >= 7.0
        }
        if strengths:
            lines.append("STRENGTHS TO MAINTAIN:")
            for dim, avg in sorted(strengths.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  - {dim}: {avg}/10")

        # Common reviewer criticisms (top 5)
        if wp.common_weaknesses:
            lines.append("COMMON REVIEWER CRITICISMS:")
            for w in wp.common_weaknesses[:5]:
                lines.append(f"  - {w}")

        return "\n".join(lines)

    # --- Thread context ---

    def get_thread_context(self) -> dict:
        """Return a summary of the active thread for LLM prompts."""
        thread = self.get_active_thread()
        if not thread or not thread.papers:
            return {}
        last = thread.papers[-1]
        return {
            "thread_id": thread.thread_id,
            "seed_topic": thread.seed_topic,
            "papers_count": len(thread.papers),
            "last_title": last.title,
            "last_findings": last.key_findings,
            "last_follow_up": last.follow_up_questions,
            "prior_titles": [p.title for p in thread.papers],
            "current_direction": thread.current_direction,
        }
