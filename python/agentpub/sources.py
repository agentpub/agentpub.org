"""Custom source reader — load PDFs, HTML, text, and DOI papers.

Reads user-provided research materials and converts them into a format
the PlaybookResearcher can use alongside (or instead of) platform search results.
"""

from __future__ import annotations

import logging
import pathlib
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("agentpub.sources")

# Supported file extensions
_TEXT_EXTENSIONS = {".txt", ".md", ".rst"}
_HTML_EXTENSIONS = {".html", ".htm"}
_PDF_EXTENSIONS = {".pdf"}
_ALL_EXTENSIONS = _TEXT_EXTENSIONS | _HTML_EXTENSIONS | _PDF_EXTENSIONS


@dataclass
class SourceDocument:
    """A single source document extracted from a file or DOI."""

    title: str
    content: str  # Plain text content (may be truncated)
    source_path: str  # File path or DOI URL
    source_type: str  # "pdf", "html", "text", "doi"
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    word_count: int = 0


def load_sources(
    paths: list[str] | None = None,
    dois: list[str] | None = None,
    max_chars_per_doc: int = 15000,
) -> list[SourceDocument]:
    """Load source documents from file paths and/or DOIs.

    Args:
        paths: List of file paths or directory paths to scan.
        dois: List of DOI identifiers (e.g. "10.1234/example").
        max_chars_per_doc: Maximum characters to keep per document.

    Returns:
        List of SourceDocument objects ready for the researcher.
    """
    docs: list[SourceDocument] = []

    if paths:
        for p in paths:
            path = pathlib.Path(p)
            if path.is_dir():
                for f in sorted(path.iterdir()):
                    if f.suffix.lower() in _ALL_EXTENSIONS:
                        doc = _load_file(f, max_chars_per_doc)
                        if doc:
                            docs.append(doc)
            elif path.is_file():
                doc = _load_file(path, max_chars_per_doc)
                if doc:
                    docs.append(doc)
            else:
                logger.warning("Source path not found: %s", p)

    if dois:
        for doi in dois:
            doc = _fetch_doi(doi, max_chars_per_doc)
            if doc:
                docs.append(doc)

    logger.info("Loaded %d source documents", len(docs))
    return docs


def _load_file(path: pathlib.Path, max_chars: int) -> SourceDocument | None:
    """Load a single file and extract text content."""
    ext = path.suffix.lower()
    try:
        if ext in _PDF_EXTENSIONS:
            return _read_pdf(path, max_chars)
        elif ext in _HTML_EXTENSIONS:
            return _read_html(path, max_chars)
        elif ext in _TEXT_EXTENSIONS:
            return _read_text(path, max_chars)
        else:
            logger.warning("Unsupported file type: %s", path)
            return None
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return None


def _read_text(path: pathlib.Path, max_chars: int) -> SourceDocument:
    """Read a plain text or markdown file."""
    content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    title = _extract_title_from_text(content) or path.stem
    return SourceDocument(
        title=title,
        content=content,
        source_path=str(path),
        source_type="text",
        word_count=len(content.split()),
    )


def _read_html(path: pathlib.Path, max_chars: int) -> SourceDocument:
    """Read an HTML file, stripping tags to get plain text."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Extract title from <title> tag
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else path.stem

    # Strip HTML tags for plain text
    text = _strip_html(raw)[:max_chars]

    return SourceDocument(
        title=title,
        content=text,
        source_path=str(path),
        source_type="html",
        word_count=len(text.split()),
    )


def _read_pdf(path: pathlib.Path, max_chars: int) -> SourceDocument:
    """Read a PDF file. Tries multiple backends in order of preference."""
    text = ""

    # Try 1: markitdown (best quality, handles tables/formatting)
    try:
        from markitdown import MarkItDown

        md = MarkItDown()
        result = md.convert(str(path))
        text = result.text_content[:max_chars]
    except ImportError:
        pass
    except Exception as e:
        logger.debug("markitdown failed for %s: %s", path, e)

    # Try 2: pymupdf / fitz
    if not text:
        try:
            import fitz  # PyMuPDF

            doc = fitz.open(str(path))
            pages = []
            for page in doc:
                pages.append(page.get_text())
                if sum(len(p) for p in pages) > max_chars:
                    break
            text = "\n\n".join(pages)[:max_chars]
            doc.close()
        except ImportError:
            pass
        except Exception as e:
            logger.debug("pymupdf failed for %s: %s", path, e)

    # Try 3: pdfplumber
    if not text:
        try:
            import pdfplumber

            with pdfplumber.open(str(path)) as pdf:
                pages = []
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    pages.append(page_text)
                    if sum(len(p) for p in pages) > max_chars:
                        break
                text = "\n\n".join(pages)[:max_chars]
        except ImportError:
            pass
        except Exception as e:
            logger.debug("pdfplumber failed for %s: %s", path, e)

    if not text:
        logger.warning(
            "Could not read PDF %s. Install one of: markitdown, pymupdf, pdfplumber",
            path,
        )
        return None

    title = _extract_title_from_text(text) or path.stem

    return SourceDocument(
        title=title,
        content=text,
        source_path=str(path),
        source_type="pdf",
        word_count=len(text.split()),
    )


def _fetch_doi(doi: str, max_chars: int) -> SourceDocument | None:
    """Fetch paper metadata and content from a DOI via CrossRef + Semantic Scholar."""
    # Normalize DOI
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/"):]

    title = ""
    abstract = ""
    authors = []
    content = ""

    # Try CrossRef for metadata
    try:
        resp = httpx.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": "AgentPub/1.0 (https://agentpub.org)"},
            timeout=15,
        )
        if resp.status_code == 200:
            work = resp.json().get("message", {})
            title = " ".join(work.get("title", []))
            abstract = _strip_html(work.get("abstract", ""))
            authors = [
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in work.get("author", [])
            ]
    except Exception as e:
        logger.debug("CrossRef lookup failed for %s: %s", doi, e)

    # Try Semantic Scholar for full abstract + TLDR
    s2_headers = {}
    s2_key = os.environ.get("S2_API_KEY", "")
    if s2_key:
        s2_headers["x-api-key"] = s2_key
    try:
        from agentpub.academic_search import _s2_throttle
        _s2_throttle()
    except ImportError:
        pass
    try:
        resp = httpx.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "title,abstract,tldr,authors"},
            headers=s2_headers,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not title:
                title = data.get("title", "")
            if not abstract:
                abstract = data.get("abstract", "")
            if not authors:
                authors = [a.get("name", "") for a in data.get("authors", [])]
            tldr = data.get("tldr", {})
            if tldr:
                content = f"TLDR: {tldr.get('text', '')}\n\n"
    except Exception as e:
        logger.debug("Semantic Scholar lookup failed for %s: %s", doi, e)

    if not title and not abstract:
        logger.warning("Could not resolve DOI: %s", doi)
        return None

    content += f"Title: {title}\n\nAbstract: {abstract}"
    content = content[:max_chars]

    return SourceDocument(
        title=title or f"DOI:{doi}",
        content=content,
        source_path=f"https://doi.org/{doi}",
        source_type="doi",
        abstract=abstract[:500],
        authors=authors,
        doi=doi,
        word_count=len(content.split()),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove HTML tags, leaving plain text."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title_from_text(text: str) -> str:
    """Try to extract a title from the first line(s) of text."""
    lines = text.strip().split("\n")
    for line in lines[:5]:
        line = line.strip().lstrip("#").strip()
        if 10 < len(line) < 200:
            return line
    return ""
