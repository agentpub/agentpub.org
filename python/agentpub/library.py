"""Local Paper Library — index and search user-provided research papers.

Users drop PDFs, HTML, or text files into ~/.agentpub/library/.
The SDK indexes each document (title, authors, year, abstract, full text)
and serves matching papers to the research pipeline with full text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger("agentpub.library")

_LIBRARY_DIR = pathlib.Path.home() / ".agentpub" / "library"
_INDEX_FILE = _LIBRARY_DIR / "index.json"

_SUPPORTED_EXTENSIONS = {".pdf", ".html", ".htm", ".txt", ".md", ".rst"}

# Max full text stored per paper in the index (100k chars ≈ 25k words)
_MAX_FULL_TEXT = 100_000


@dataclass
class LibraryPaper:
    """A single indexed paper from the local library."""

    file_path: str
    file_hash: str
    title: str
    authors: list[str]
    abstract: str
    full_text: str
    year: int | None = None
    doi: str = ""
    word_count: int = 0
    source_type: str = ""
    indexed_at: float = 0.0
    keywords: list[str] = field(default_factory=list)


class PaperLibrary:
    """Manages a local library of research papers."""

    def __init__(self, library_dir: pathlib.Path | None = None):
        self._dir = library_dir or _LIBRARY_DIR
        self._index_file = self._dir / "index.json"
        self._lock = threading.Lock()
        self._index: dict | None = None

    @property
    def library_dir(self) -> pathlib.Path:
        return self._dir

    def ensure_dir(self) -> None:
        """Create library directory if it doesn't exist."""
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load_index(self) -> dict:
        """Load index from disk."""
        if self._index is not None:
            return self._index
        if self._index_file.exists():
            try:
                data = json.loads(self._index_file.read_text(encoding="utf-8"))
                self._index = data
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Corrupt library index, starting fresh: %s", e)
        self._index = {"version": 1, "papers": {}}
        return self._index

    def _save_index(self) -> None:
        """Write index to disk atomically."""
        if self._index is None:
            return
        self.ensure_dir()
        tmp = self._index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._index_file)

    @staticmethod
    def _file_hash(path: pathlib.Path) -> str:
        """SHA-256 hash of file contents."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    @staticmethod
    def _paper_id(file_path: str) -> str:
        """Stable ID from file path."""
        return hashlib.sha256(file_path.encode()).hexdigest()[:12]

    def add_files(self, file_paths: list[str], copy_to_library: bool = True) -> list[LibraryPaper]:
        """Index one or more files. Optionally copies them into the library folder.

        Returns list of successfully indexed papers.
        """
        from .sources import _load_file

        results = []
        with self._lock:
            index = self._load_index()

            for fp in file_paths:
                path = pathlib.Path(fp)
                if not path.exists():
                    logger.warning("File not found: %s", fp)
                    continue
                if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                    logger.warning("Unsupported file type: %s", path.suffix)
                    continue

                # Copy to library dir if requested and not already there
                if copy_to_library and not str(path).startswith(str(self._dir)):
                    dest = self._dir / path.name
                    # Handle name collisions
                    if dest.exists() and dest != path:
                        stem = path.stem
                        suffix = path.suffix
                        dest = self._dir / f"{stem}_{int(time.time())}{suffix}"
                    shutil.copy2(path, dest)
                    path = dest

                # Extract content using sources.py
                try:
                    doc = _load_file(path, max_chars=_MAX_FULL_TEXT)
                except Exception as e:
                    logger.warning("Failed to extract %s: %s", path, e)
                    continue

                if not doc:
                    continue

                file_hash = self._file_hash(path)
                paper_id = self._paper_id(str(path))

                # Extract metadata
                title = doc.title or path.stem
                full_text = doc.content or ""
                abstract = self._extract_abstract(full_text)
                authors = doc.authors or self._extract_authors(full_text)
                year = self._extract_year(full_text, str(path))
                doi = doc.doi or self._extract_doi(full_text)
                keywords = self._extract_keywords(title, abstract)

                lp = LibraryPaper(
                    file_path=str(path),
                    file_hash=file_hash,
                    title=title,
                    authors=authors,
                    abstract=abstract,
                    full_text=full_text[:_MAX_FULL_TEXT],
                    year=year,
                    doi=doi,
                    word_count=len(full_text.split()),
                    source_type=doc.source_type,
                    indexed_at=time.time(),
                    keywords=keywords,
                )

                index["papers"][paper_id] = asdict(lp)
                results.append(lp)
                logger.info("Indexed: %s (%d words, %s)", title[:60], lp.word_count, doc.source_type)

            self._save_index()

        return results

    def remove_paper(self, paper_id: str) -> bool:
        """Remove a paper from the index."""
        with self._lock:
            index = self._load_index()
            if paper_id in index["papers"]:
                del index["papers"][paper_id]
                self._save_index()
                return True
        return False

    def reindex(self) -> int:
        """Scan library dir for new/changed files, update index."""
        self.ensure_dir()
        changes = 0

        with self._lock:
            index = self._load_index()
            existing_paths = {p["file_path"] for p in index["papers"].values()}

            # Find new files
            new_files = []
            for f in sorted(self._dir.iterdir()):
                if f.suffix.lower() in _SUPPORTED_EXTENSIONS and str(f) not in existing_paths:
                    new_files.append(str(f))

            # Check for changed files (hash mismatch)
            for paper_id, paper_data in list(index["papers"].items()):
                path = pathlib.Path(paper_data["file_path"])
                if not path.exists():
                    del index["papers"][paper_id]
                    changes += 1
                    logger.info("Removed missing file from index: %s", paper_data.get("title", "?"))
                elif self._file_hash(path) != paper_data.get("file_hash", ""):
                    new_files.append(str(path))
                    del index["papers"][paper_id]
                    changes += 1

            self._save_index()

        # Index new/changed files (outside lock — add_files acquires it)
        if new_files:
            added = self.add_files(new_files, copy_to_library=False)
            changes += len(added)

        return changes

    def get_all(self) -> list[LibraryPaper]:
        """Return all indexed papers."""
        with self._lock:
            index = self._load_index()
            return [LibraryPaper(**p) for p in index["papers"].values()]

    def count(self) -> int:
        """Return number of indexed papers."""
        with self._lock:
            index = self._load_index()
            return len(index["papers"])

    def search(self, query: str, limit: int = 20) -> list[LibraryPaper]:
        """Keyword search against title, abstract, keywords, full_text.

        Scores by term frequency with field weights:
        title (4x), abstract (2x), keywords (3x), full_text (1x).
        """
        terms = [t.lower() for t in query.split() if len(t) > 2]
        if not terms:
            return self.get_all()[:limit]

        with self._lock:
            index = self._load_index()

        scored = []
        for paper_id, p in index["papers"].items():
            score = 0.0
            title_lower = p.get("title", "").lower()
            abstract_lower = p.get("abstract", "").lower()
            keywords_lower = " ".join(p.get("keywords", [])).lower()
            # Don't load full text into memory just for scoring — use first 5000 chars
            text_lower = p.get("full_text", "")[:5000].lower()

            for term in terms:
                if term in title_lower:
                    score += 4.0
                if term in abstract_lower:
                    score += 2.0
                if term in keywords_lower:
                    score += 3.0
                if term in text_lower:
                    score += 1.0

            if score > 0:
                scored.append((score, paper_id, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [LibraryPaper(**p) for _, _, p in scored[:limit]]

    def find_by_doi(self, doi: str) -> LibraryPaper | None:
        """Exact DOI match."""
        if not doi:
            return None
        doi_clean = doi.strip().lower()
        with self._lock:
            index = self._load_index()
            for p in index["papers"].values():
                if p.get("doi", "").strip().lower() == doi_clean:
                    return LibraryPaper(**p)
        return None

    def find_by_title(self, title: str, threshold: float = 0.85) -> LibraryPaper | None:
        """Fuzzy title match using SequenceMatcher."""
        if not title:
            return None
        title_norm = title.strip().lower()
        best_match = None
        best_ratio = 0.0

        with self._lock:
            index = self._load_index()
            for p in index["papers"].values():
                p_title = p.get("title", "").strip().lower()
                ratio = SequenceMatcher(None, title_norm, p_title).ratio()
                if ratio > best_ratio and ratio >= threshold:
                    best_ratio = ratio
                    best_match = p

        if best_match:
            return LibraryPaper(**best_match)
        return None

    def to_paper_dict(self, paper: LibraryPaper) -> dict:
        """Convert LibraryPaper to the dict format used by playbook_researcher."""
        return {
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "doi": paper.doi,
            "url": "",
            "enriched_content": paper.full_text,
            "source": "local_library",
            "relevance_score": 0.85,
            "on_domain": True,
            "citation_count": 0,
            "venue": "",
            "word_count": paper.word_count,
        }

    # ------------------------------------------------------------------
    # Metadata extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_abstract(text: str) -> str:
        """Try to find the abstract section in paper text."""
        # Look for "Abstract" header
        m = re.search(
            r"(?:^|\n)\s*(?:abstract|summary)\s*[:\.\n](.+?)(?:\n\s*(?:introduction|keywords|1[\.\s])|$)",
            text[:5000], re.IGNORECASE | re.DOTALL,
        )
        if m:
            abstract = m.group(1).strip()
            # Limit to ~500 words
            words = abstract.split()
            if len(words) > 500:
                abstract = " ".join(words[:500]) + "..."
            return abstract
        # Fallback: first 300 words
        words = text.split()[:300]
        return " ".join(words)

    @staticmethod
    def _extract_authors(text: str) -> list[str]:
        """Try to extract author names from near the top of the document."""
        # Look in the first 2000 chars for common author patterns
        header = text[:2000]
        # Pattern: names separated by commas or "and", often with affiliations after
        # This is a heuristic — won't work for all formats
        lines = header.split("\n")
        for line in lines[1:10]:  # skip title (first line)
            line = line.strip()
            if not line or len(line) > 500:
                continue
            # Lines with multiple capitalized names separated by commas
            if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+(?:\s*,\s*[A-Z][a-z]+ [A-Z][a-z]+)+", line):
                names = [n.strip() for n in re.split(r",\s*(?:and\s+)?", line) if n.strip()]
                if 2 <= len(names) <= 20:
                    return names[:10]
        return []

    @staticmethod
    def _extract_year(text: str, filename: str) -> int | None:
        """Try to extract publication year."""
        # Check filename first (e.g., "Smith2023.pdf")
        m = re.search(r"(20[012]\d)", filename)
        if m:
            return int(m.group(1))
        # Look in first 3000 chars for year patterns
        header = text[:3000]
        years = re.findall(r"\b(20[012]\d)\b", header)
        if years:
            # Return the most common year in the header
            from collections import Counter
            most_common = Counter(years).most_common(1)
            if most_common:
                return int(most_common[0][0])
        # Try 19xx
        years = re.findall(r"\b(19[89]\d)\b", header)
        if years:
            from collections import Counter
            most_common = Counter(years).most_common(1)
            if most_common:
                return int(most_common[0][0])
        return None

    @staticmethod
    def _extract_doi(text: str) -> str:
        """Try to find a DOI in the text."""
        m = re.search(r"(?:doi[:\s]*|https?://doi\.org/)(10\.\d{4,}/[^\s,;\"']+)", text[:5000], re.IGNORECASE)
        if m:
            doi = m.group(1).rstrip(".")
            return doi
        return ""

    @staticmethod
    def _extract_keywords(title: str, abstract: str) -> list[str]:
        """Extract simple keyword list from title and abstract."""
        # Combine title and abstract, extract significant words
        text = f"{title} {abstract}".lower()
        # Remove common stopwords
        stopwords = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
            "has", "have", "had", "this", "that", "these", "those", "it", "its",
            "we", "our", "they", "their", "not", "no", "can", "may", "will",
            "also", "more", "between", "through", "using", "based", "than",
            "which", "how", "what", "when", "where", "who", "both", "into",
            "such", "very", "most", "each", "some", "all", "about", "over",
        }
        words = re.findall(r"\b[a-z]{4,}\b", text)
        # Count and return top terms
        from collections import Counter
        counts = Counter(w for w in words if w not in stopwords)
        return [w for w, _ in counts.most_common(15)]
