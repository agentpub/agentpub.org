"""Lightweight system resource monitor for daemon gating.

If ``psutil`` is installed, gates daemon activities on CPU/memory thresholds.
If ``psutil`` is missing, gating is disabled (always returns available).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


class ResourceMonitor:
    """Check whether the system has enough headroom for heavy work."""

    def __init__(self, cpu_threshold: float = 80.0, memory_threshold: float = 85.0):
        self.cpu_threshold = cpu_threshold
        self.memory_threshold = memory_threshold

    def is_available(self) -> bool:
        """Return True if CPU *and* memory are below their thresholds.

        Always returns True when ``psutil`` is not installed.
        """
        if not _HAS_PSUTIL:
            return True
        try:
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory().percent
            ok = cpu < self.cpu_threshold and mem < self.memory_threshold
            if not ok:
                logger.info(
                    "Resource gate: CPU=%.1f%% (max %.0f%%), MEM=%.1f%% (max %.0f%%) — skipping",
                    cpu, self.cpu_threshold, mem, self.memory_threshold,
                )
            return ok
        except Exception as e:
            logger.warning("Resource check failed (%s), assuming available", e)
            return True

    def get_stats(self) -> dict:
        """Return current CPU/memory stats (or empty dict without psutil)."""
        if not _HAS_PSUTIL:
            return {"psutil_available": False}
        try:
            return {
                "psutil_available": True,
                "cpu_percent": psutil.cpu_percent(interval=0.5),
                "memory_percent": psutil.virtual_memory().percent,
                "cpu_threshold": self.cpu_threshold,
                "memory_threshold": self.memory_threshold,
            }
        except Exception:
            return {"psutil_available": True, "error": "read failed"}
