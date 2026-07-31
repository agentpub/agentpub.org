"""Zotero integration — import papers from local Zotero database or Web API.

Two paths:
1. Local: Read zotero.sqlite + storage/ PDFs directly (no account needed)
2. Web API: https://api.zotero.org/users/{id}/items (needs API key)

Both return paper metadata + PDF paths that PaperLibrary can index.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import re
import sqlite3
import struct
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("agentpub.zotero")

# ---------------------------------------------------------------------------
# Zotero data directory detection
# ---------------------------------------------------------------------------

_ZOTERO_DIR_ENV = "ZOTERO_DATA_DIR"


def find_zotero_data_dir() -> pathlib.Path | None:
    """Auto-detect the Zotero data directory.

    Checks:
    1. ZOTERO_DATA_DIR environment variable
    2. Default paths per OS
    """
    env = os.environ.get(_ZOTERO_DIR_ENV)
    if env:
        p = pathlib.Path(env)
        if p.exists():
            return p

    system = platform.system()
    candidates: list[pathlib.Path] = []

    if system == "Windows":
        home = pathlib.Path.home()
        candidates = [
            home / "Zotero",
            pathlib.Path(os.environ.get("APPDATA", "")) / "Zotero" / "Zotero" / "Profiles",
        ]
        # Check the default Zotero 6/7 location
        candidates.insert(0, home / "Zotero")
    elif system == "Darwin":
        home = pathlib.Path.home()
        candidates = [
            home / "Zotero",
            home / "Library" / "Application Support" / "Zotero" / "Profiles",
        ]
    else:  # Linux
        home = pathlib.Path.home()
        candidates = [
            home / "Zotero",
            home / ".zotero" / "zotero",
        ]

    for c in candidates:
        db = c / "zotero.sqlite"
        if db.exists():
            return c

    return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ZoteroPaper:
    """A paper extracted from Zotero (local or web)."""

    item_key: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    year: int | None = None
    doi: str = ""
    url: str = ""
    publication: str = ""
    item_type: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    pdf_path: str = ""  # local path to attached PDF (if any)
    date_added: str = ""


# ---------------------------------------------------------------------------
# Local Zotero SQLite reader
# ---------------------------------------------------------------------------

class ZoteroLocal:
    """Read papers from a local Zotero SQLite database."""

    def __init__(self, data_dir: pathlib.Path | None = None):
        self._data_dir = data_dir or find_zotero_data_dir()
        if self._data_dir is None:
            raise FileNotFoundError(
                "Could not find Zotero data directory. "
                "Set ZOTERO_DATA_DIR environment variable or install Zotero."
            )
        self._db_path = self._data_dir / "zotero.sqlite"
        if not self._db_path.exists():
            raise FileNotFoundError(f"Zotero database not found: {self._db_path}")
        self._storage_dir = self._data_dir / "storage"

    @property
    def data_dir(self) -> pathlib.Path:
        return self._data_dir

    def _connect(self) -> sqlite3.Connection:
        """Open read-only connection to Zotero DB.

        Uses immutable mode so we don't interfere with a running Zotero instance.
        """
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get_collections(self) -> list[dict]:
        """Return list of all collections with their names and keys."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT collectionID, collectionName, key FROM collections ORDER BY collectionName"
            ).fetchall()
            return [
                {"id": r["collectionID"], "name": r["collectionName"], "key": r["key"]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_papers(
        self,
        collection_id: int | None = None,
        limit: int = 500,
    ) -> list[ZoteroPaper]:
        """Read papers from the local Zotero database.

        Args:
            collection_id: If set, only return items from this collection.
            limit: Maximum number of papers to return.
        """
        conn = self._connect()
        try:
            return self._query_papers(conn, collection_id, limit)
        finally:
            conn.close()

    def _query_papers(
        self, conn: sqlite3.Connection, collection_id: int | None, limit: int
    ) -> list[ZoteroPaper]:
        # Get item IDs — filter by collection if specified
        if collection_id is not None:
            item_rows = conn.execute(
                """
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                JOIN collectionItems ci ON i.itemID = ci.itemID
                WHERE ci.collectionID = ?
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY i.dateAdded DESC
                LIMIT ?
                """,
                (collection_id, limit),
            ).fetchall()
        else:
            item_rows = conn.execute(
                """
                SELECT i.itemID, i.key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY i.dateAdded DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        papers = []
        for row in item_rows:
            item_id = row["itemID"]
            item_key = row["key"]
            item_type = row["typeName"]

            # Get field values
            fields = self._get_item_fields(conn, item_id)
            title = fields.get("title", "")
            if not title:
                continue

            # Get authors
            authors = self._get_item_creators(conn, item_id)

            # Get tags
            tags = self._get_item_tags(conn, item_id)

            # Get collections
            collections = self._get_item_collections(conn, item_id)

            # Find attached PDF
            pdf_path = self._find_pdf_attachment(conn, item_id)

            # Parse year from date field
            year = None
            date_str = fields.get("date", "")
            if date_str:
                m = re.search(r"((?:19|20)\d{2})", date_str)
                if m:
                    year = int(m.group(1))

            papers.append(ZoteroPaper(
                item_key=item_key,
                title=title,
                authors=authors,
                abstract=fields.get("abstractNote", ""),
                year=year,
                doi=fields.get("DOI", ""),
                url=fields.get("url", ""),
                publication=fields.get("publicationTitle", "") or fields.get("journalAbbreviation", ""),
                item_type=item_type,
                tags=tags,
                collections=collections,
                pdf_path=pdf_path,
                date_added=fields.get("dateAdded", ""),
            ))

        return papers

    def _get_item_fields(self, conn: sqlite3.Connection, item_id: int) -> dict[str, str]:
        """Get all field values for an item."""
        rows = conn.execute(
            """
            SELECT f.fieldName, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID = ?
            """,
            (item_id,),
        ).fetchall()
        return {r["fieldName"]: r["value"] for r in rows}

    def _get_item_creators(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Get author names for an item."""
        rows = conn.execute(
            """
            SELECT c.firstName, c.lastName
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        ).fetchall()
        authors = []
        for r in rows:
            first = r["firstName"] or ""
            last = r["lastName"] or ""
            name = f"{first} {last}".strip()
            if name:
                authors.append(name)
        return authors

    def _get_item_tags(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Get tags for an item."""
        rows = conn.execute(
            """
            SELECT t.name
            FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID = ?
            """,
            (item_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def _get_item_collections(self, conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Get collection names for an item."""
        rows = conn.execute(
            """
            SELECT c.collectionName
            FROM collectionItems ci
            JOIN collections c ON ci.collectionID = c.collectionID
            WHERE ci.itemID = ?
            """,
            (item_id,),
        ).fetchall()
        return [r["collectionName"] for r in rows]

    def _find_pdf_attachment(self, conn: sqlite3.Connection, item_id: int) -> str:
        """Find the PDF attachment path for an item."""
        rows = conn.execute(
            """
            SELECT ia.path, i.key
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.parentItemID = ?
              AND (ia.contentType = 'application/pdf'
                   OR ia.path LIKE '%.pdf')
            LIMIT 1
            """,
            (item_id,),
        ).fetchall()
        if not rows:
            return ""

        path_val = rows[0]["path"] or ""
        att_key = rows[0]["key"]

        # Zotero stores paths as "storage:<filename>" for managed files
        if path_val.startswith("storage:"):
            filename = path_val[8:]  # strip "storage:" prefix
            pdf_path = self._storage_dir / att_key / filename
            if pdf_path.exists():
                return str(pdf_path)
        elif pathlib.Path(path_val).is_absolute() and pathlib.Path(path_val).exists():
            return path_val

        # Try finding any PDF in the storage/<key>/ folder
        att_dir = self._storage_dir / att_key
        if att_dir.exists():
            for f in att_dir.iterdir():
                if f.suffix.lower() == ".pdf":
                    return str(f)

        return ""

    def count(self) -> int:
        """Count total non-attachment items."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                """
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Zotero Web API
# ---------------------------------------------------------------------------

_ZOTERO_API_BASE = "https://api.zotero.org"


class ZoteroWeb:
    """Read papers from the Zotero Web API.

    Requires a user ID and API key from https://www.zotero.org/settings/keys
    """

    def __init__(self, user_id: str, api_key: str):
        self._user_id = user_id
        self._api_key = api_key
        self._base = f"{_ZOTERO_API_BASE}/users/{user_id}"

    def get_collections(self) -> list[dict]:
        """Fetch all collections from Zotero Web."""
        items = []
        url = f"{self._base}/collections"
        params = {"limit": 100, "start": 0}

        while True:
            resp = self._request(url, params)
            if not resp:
                break
            for item in resp:
                data = item.get("data", {})
                items.append({
                    "key": data.get("key", ""),
                    "name": data.get("name", ""),
                    "num_items": data.get("meta", {}).get("numItems", 0),
                })
            if len(resp) < params["limit"]:
                break
            params["start"] += params["limit"]

        return items

    def get_papers(
        self,
        collection_key: str | None = None,
        limit: int = 100,
    ) -> list[ZoteroPaper]:
        """Fetch papers from Zotero Web API.

        Args:
            collection_key: If set, only return items from this collection.
            limit: Maximum number of papers to return.
        """
        if collection_key:
            url = f"{self._base}/collections/{collection_key}/items"
        else:
            url = f"{self._base}/items"

        params = {
            "limit": min(limit, 100),  # API max is 100 per page
            "start": 0,
            "itemType": "-attachment || note || annotation",
            "sort": "dateAdded",
            "direction": "desc",
        }

        papers = []
        while len(papers) < limit:
            resp = self._request(url, params)
            if not resp:
                break

            for item in resp:
                data = item.get("data", {})
                item_type = data.get("itemType", "")
                if item_type in ("attachment", "note", "annotation"):
                    continue

                title = data.get("title", "")
                if not title:
                    continue

                # Parse authors
                authors = []
                for creator in data.get("creators", []):
                    first = creator.get("firstName", "")
                    last = creator.get("lastName", "")
                    name = f"{first} {last}".strip()
                    if not name:
                        name = creator.get("name", "")
                    if name:
                        authors.append(name)

                # Parse year
                year = None
                date_str = data.get("date", "")
                if date_str:
                    m = re.search(r"((?:19|20)\d{2})", date_str)
                    if m:
                        year = int(m.group(1))

                # Tags
                tags = [t.get("tag", "") for t in data.get("tags", []) if t.get("tag")]

                # Collections
                collections = data.get("collections", [])

                papers.append(ZoteroPaper(
                    item_key=data.get("key", ""),
                    title=title,
                    authors=authors,
                    abstract=data.get("abstractNote", ""),
                    year=year,
                    doi=data.get("DOI", ""),
                    url=data.get("url", ""),
                    publication=data.get("publicationTitle", "") or data.get("journalAbbreviation", ""),
                    item_type=item_type,
                    tags=tags,
                    collections=collections,
                    pdf_path="",  # Web API doesn't give local paths
                    date_added=data.get("dateAdded", ""),
                ))

            if len(resp) < params["limit"]:
                break
            params["start"] += params["limit"]

        return papers[:limit]

    def _request(self, url: str, params: dict | None = None) -> list[dict] | None:
        """Make an authenticated GET request to the Zotero API."""
        headers = {
            "Zotero-API-Key": self._api_key,
            "Zotero-API-Version": "3",
        }
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 403:
                logger.error("Zotero API: forbidden — check your API key")
            elif resp.status_code == 404:
                logger.error("Zotero API: not found — check your user ID")
            else:
                logger.warning("Zotero API error %d: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            logger.warning("Zotero API request failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Helper: import Zotero papers into PaperLibrary
# ---------------------------------------------------------------------------

def import_zotero_papers(
    zotero_papers: list[ZoteroPaper],
    library,  # PaperLibrary instance
    include_pdfs: bool = True,
) -> int:
    """Import Zotero papers into a PaperLibrary.

    For papers with local PDFs, indexes the PDF via the library's add_files().
    For papers without PDFs, creates index entries from Zotero metadata.

    Returns number of papers imported.
    """
    imported = 0

    # Papers with PDFs — use add_files for full extraction
    pdf_paths = []
    for zp in zotero_papers:
        if include_pdfs and zp.pdf_path and pathlib.Path(zp.pdf_path).exists():
            pdf_paths.append(zp.pdf_path)

    if pdf_paths:
        added = library.add_files(pdf_paths, copy_to_library=True)
        imported += len(added)

        # Overlay Zotero metadata (better than extracted metadata)
        _overlay_zotero_metadata(library, zotero_papers, added)

    # Papers without PDFs — create entries from Zotero metadata alone
    from dataclasses import asdict
    from .library import LibraryPaper

    with library._lock:
        index = library._load_index()
        for zp in zotero_papers:
            if zp.pdf_path and zp.pdf_path in [p for p in pdf_paths]:
                continue  # Already handled above

            paper_id = library._paper_id(f"zotero:{zp.item_key}")
            if paper_id in index["papers"]:
                continue  # Already indexed

            lp = LibraryPaper(
                file_path=f"zotero:{zp.item_key}",
                file_hash=f"zotero_{zp.item_key}",
                title=zp.title,
                authors=zp.authors,
                abstract=zp.abstract,
                full_text=zp.abstract,  # Only abstract available without PDF
                year=zp.year,
                doi=zp.doi,
                word_count=len(zp.abstract.split()) if zp.abstract else 0,
                source_type="zotero_metadata",
                indexed_at=time.time(),
                keywords=zp.tags[:15] if zp.tags else [],
            )
            index["papers"][paper_id] = asdict(lp)
            imported += 1
            logger.info("Imported from Zotero (metadata only): %s", zp.title[:60])

        library._save_index()

    return imported


def _overlay_zotero_metadata(library, zotero_papers: list[ZoteroPaper], added_papers) -> None:
    """Overlay Zotero's curated metadata onto freshly-indexed papers.

    Zotero metadata (title, authors, year, DOI) is usually more accurate
    than what we extract from raw PDF text.
    """
    if not added_papers:
        return

    # Build lookup by title (fuzzy) and DOI (exact)
    zotero_by_doi = {}
    zotero_by_title = {}
    for zp in zotero_papers:
        if zp.doi:
            zotero_by_doi[zp.doi.lower().strip()] = zp
        if zp.title:
            zotero_by_title[zp.title.lower().strip()] = zp

    with library._lock:
        index = library._load_index()
        for lp in added_papers:
            paper_id = library._paper_id(lp.file_path)
            if paper_id not in index["papers"]:
                continue
            entry = index["papers"][paper_id]

            # Try DOI match first, then title
            zp = None
            if entry.get("doi"):
                zp = zotero_by_doi.get(entry["doi"].lower().strip())
            if not zp and entry.get("title"):
                zp = zotero_by_title.get(entry["title"].lower().strip())

            if zp:
                # Overlay — Zotero metadata is better
                if zp.title:
                    entry["title"] = zp.title
                if zp.authors:
                    entry["authors"] = zp.authors
                if zp.year:
                    entry["year"] = zp.year
                if zp.doi:
                    entry["doi"] = zp.doi
                if zp.abstract and len(zp.abstract) > len(entry.get("abstract", "")):
                    entry["abstract"] = zp.abstract
                entry["source_type"] = "zotero_pdf"

        library._save_index()
