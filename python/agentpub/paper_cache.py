"""Local paper cache — avoids redundant 3rd-party API calls.

Stores fetched paper metadata (title, abstract, authors, year, etc.) in a
local SQLite database at ~/.agentpub/cache/papers.db. When the SDK searches
for papers, it checks this cache first. If a paper with the same DOI or
title already exists locally, it skips the API call.

No data is uploaded anywhere — this is purely local.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re
import sqlite3
import json
import time

logger = logging.getLogger("agentpub.paper_cache")

_DB_DIR = pathlib.Path.home() / ".agentpub" / "cache"
_DB_PATH = _DB_DIR / "papers.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Lazy-init SQLite connection."""
    global _conn
    if _conn is not None:
        return _conn

    _DB_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    _conn.execute("PRAGMA journal_mode=WAL")  # better concurrency
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            doc_id    TEXT PRIMARY KEY,
            doi       TEXT,
            title     TEXT,
            data      TEXT,
            cached_at REAL
        )
    """)
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_doi ON papers(doi)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON papers(title)")
    _conn.commit()
    return _conn


def _make_doc_id(doi: str | None, title: str | None) -> str | None:
    """Generate a document ID from DOI or title hash."""
    if doi and doi.strip():
        return "doi_" + re.sub(r"[/\\]", "_", doi.strip().lower())
    if title and title.strip():
        normalized = re.sub(r"\s+", " ", title.lower().strip())
        return "title_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return None


def cache_paper(paper: dict) -> bool:
    """Store a paper in the local cache. Returns True if stored."""
    doi = (paper.get("doi") or "").strip()
    title = (paper.get("title") or "").strip()
    doc_id = _make_doc_id(doi, title)
    if not doc_id or not title:
        return False

    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO papers (doc_id, doi, title, data, cached_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, doi.lower() if doi else None, title.lower()[:200], json.dumps(paper, default=str), time.time()),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("Cache write failed: %s", e)
        return False


def cache_papers(papers: list[dict]) -> int:
    """Batch-store multiple papers. Returns count stored."""
    if not papers:
        return 0
    count = 0
    try:
        conn = _get_conn()
        for paper in papers:
            doi = (paper.get("doi") or "").strip()
            title = (paper.get("title") or "").strip()
            doc_id = _make_doc_id(doi, title)
            if not doc_id or not title:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO papers (doc_id, doi, title, data, cached_at) VALUES (?, ?, ?, ?, ?)",
                (doc_id, doi.lower() if doi else None, title.lower()[:200], json.dumps(paper, default=str), time.time()),
            )
            count += 1
        conn.commit()
    except Exception as e:
        logger.debug("Cache batch write failed: %s", e)
    return count


def get_by_doi(doi: str) -> dict | None:
    """Retrieve a cached paper by DOI."""
    if not doi or not doi.strip():
        return None
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT data FROM papers WHERE doi = ?", (doi.strip().lower(),)
        ).fetchone()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.debug("Cache read failed: %s", e)
    return None


def get_by_title(title: str) -> dict | None:
    """Retrieve a cached paper by exact title match."""
    if not title or not title.strip():
        return None
    normalized = re.sub(r"\s+", " ", title.lower().strip())
    doc_id = "title_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT data FROM papers WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.debug("Cache read failed: %s", e)
    return None


def get_cached(doi: str | None = None, title: str | None = None) -> dict | None:
    """Retrieve a cached paper by DOI (preferred) or title."""
    if doi:
        result = get_by_doi(doi)
        if result:
            return result
    if title:
        return get_by_title(title)
    return None


def search_cached(query: str, limit: int = 20) -> list[dict]:
    """Search local cache by title keywords."""
    words = [w.lower() for w in query.split() if len(w) > 3]
    if not words:
        return []

    try:
        conn = _get_conn()
        # Use LIKE for each keyword against the title column
        conditions = " AND ".join(f"title LIKE ?" for _ in words)
        params = [f"%{w}%" for w in words]
        rows = conn.execute(
            f"SELECT data FROM papers WHERE {conditions} ORDER BY cached_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception as e:
        logger.debug("Cache search failed: %s", e)
        return []


def update_enriched_content(paper: dict, enriched_content: str) -> bool:
    """Update a cached paper with enriched full-text content."""
    doi = (paper.get("doi") or "").strip()
    title = (paper.get("title") or "").strip()
    doc_id = _make_doc_id(doi, title)
    if not doc_id:
        return False

    try:
        conn = _get_conn()
        row = conn.execute("SELECT data FROM papers WHERE doc_id = ?", (doc_id,)).fetchone()
        if row:
            data = json.loads(row[0])
            data["enriched_content"] = enriched_content
            conn.execute(
                "UPDATE papers SET data = ? WHERE doc_id = ?",
                (json.dumps(data, default=str), doc_id),
            )
            conn.commit()
            return True
    except Exception:
        pass
    return False


def cache_stats() -> dict:
    """Return basic cache statistics."""
    try:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        with_doi = conn.execute("SELECT COUNT(*) FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchone()[0]
        return {"total": total, "with_doi": with_doi, "db_path": str(_DB_PATH)}
    except Exception:
        return {"total": 0, "with_doi": 0, "db_path": str(_DB_PATH)}
