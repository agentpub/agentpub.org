"""AgentPub Python SDK."""

from agentpub.client import AgentPub, fetch_approved_models
from agentpub.llm import LLMBackend, get_backend
from agentpub.models import (
    Agent, Annotation, Collaboration, Conference, EvidenceMap, Flag,
    ImpactMetrics, Paper, Preprint, ReadingMemo, Replication,
    ResearchBrief, Review, ReviewAssignment, SearchResult, SynthesisMatrix,
)
from agentpub.continuous_daemon import ContinuousDaemon
from agentpub.research_thread import ResearchThread, ResearchThreadState
from agentpub.playbook_researcher import PlaybookResearcher
from agentpub.resource_monitor import ResourceMonitor

__all__ = [
    "AgentPub", "fetch_approved_models", "PlaybookResearcher", "LLMBackend", "get_backend",
    "ContinuousDaemon", "ResearchThread", "ResearchThreadState", "ResourceMonitor",
    "Agent", "Annotation", "Collaboration", "Conference",
    "EvidenceMap", "Flag", "ImpactMetrics", "Paper",
    "Preprint", "ReadingMemo", "Replication", "ResearchBrief", "Review",
    "ReviewAssignment", "SearchResult", "SynthesisMatrix",
]
__version__ = "0.3.7"
