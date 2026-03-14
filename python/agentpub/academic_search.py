"""Academic paper search via free public APIs + optional paid connectors.

Core sources (all free, no API key required):
  1. Crossref REST API — DOI metadata, polite pool (~5 req/s with mailto)
  2. arXiv API — preprints in STEM (3-second delay between requests)
  3. Semantic Scholar — broad coverage, aggressive rate limits (429 common)
  4. OpenAlex — open bibliographic data, citation counts, inverted-index abstracts
  5. PubMed / NCBI — biomedical literature (E-utilities, optional API key for 10/s)
  6. Europe PMC — European biomedical literature, citation counts

Optional sources (require API keys via env vars):
  7. Serper.dev Google Scholar (SERPER_API_KEY)
  8. CORE (CORE_API_KEY) — open access aggregator
  9. Lens.org (LENS_API_KEY) — scholarly + patent search
  10. Scopus (SCOPUS_API_KEY) — Elsevier's abstract/citation database
  11. Dimensions (DIMENSIONS_API_KEY) — DOI-level citation metrics lookup

Results are returned in a format the PlaybookResearcher can use directly.
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger("agentpub.academic_search")

# Crossref (most open, polite pool with mailto)
_CROSSREF_BASE = "https://api.crossref.org"
_CROSSREF_MAILTO = "api@agentpub.org"  # default, overridden by user email

# arXiv (completely open, 3-second delay rule)
_ARXIV_BASE = "https://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Semantic Scholar (free but aggressive rate limits — 100 req/5min unauth, 1 req/s with key)
_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_FIELDS = "paperId,title,abstract,year,authors,citationCount,url,externalIds,venue"
_S2_MIN_INTERVAL = 2.0  # seconds between S2 calls (their limit is 1/s, we use 2 for safety)
_s2_last_call: float = 0.0

# ---------------------------------------------------------------------------
# Generic per-provider throttle (prevents hammering any single API)
# ---------------------------------------------------------------------------
_provider_last_call: dict[str, float] = {}

# Minimum seconds between consecutive calls to the same provider
_PROVIDER_MIN_INTERVALS: dict[str, float] = {
    "crossref": 1.0,     # polite pool allows ~50/s, but 1/s is courteous
    "arxiv": 3.0,        # arXiv explicitly requires 3-second delay
    "s2": 2.0,           # 1/s with key, we use 2 for safety
    "openalex": 0.5,     # generous limits, light throttle for politeness
    "pubmed": 0.34,      # 3/s without key, 10/s with key
    "europe_pmc": 0.5,   # no documented limit, light throttle
    "core": 2.0,         # 0.5 req/s
    "serper": 0.2,       # paid API, generous limits
    "lens": 1.0,         # documented 1 req/s
    "scopus": 1.0,       # Elsevier rate limits
}


def _throttle(provider: str) -> None:
    """Wait if needed to respect per-provider rate limits."""
    interval = _PROVIDER_MIN_INTERVALS.get(provider, 1.0)
    last = _provider_last_call.get(provider, 0.0)
    elapsed = time.time() - last
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _provider_last_call[provider] = time.time()


def _s2_headers() -> dict:
    """Return Semantic Scholar API headers, including API key if available."""
    key = os.environ.get("S2_API_KEY", "")
    if key:
        return {"x-api-key": key}
    return {}


def _s2_throttle() -> None:
    """Wait if needed to respect Semantic Scholar rate limits (2s between calls)."""
    _throttle("s2")


_STOPWORDS = {"the", "of", "and", "in", "a", "an", "for", "to", "on", "with", "is", "are", "by", "from", "at", "or", "as"}

# Words that are too generic to count as domain-specific matches
_GENERIC_WORDS = {
    "time", "define", "fundamental", "change", "process", "system", "systems",
    "model", "models", "method", "methods", "data", "analysis", "results",
    "study", "studies", "evidence", "theory", "theories", "approach", "effect",
    "effects", "role", "structure", "function", "level", "nature", "problem",
    "question", "research", "review", "paper", "work", "new", "based", "using",
    "different", "between", "across", "within", "general", "specific", "human",
    "interaction", "interactions", "relationship", "development", "design",
    "performance", "impact", "outcome", "outcomes", "response", "type", "types",
    "test", "testing", "assessment", "evaluation", "measure", "measures",
    "treatment", "group", "control", "comparison", "framework", "concept",
    "mechanism", "mechanisms", "factor", "factors", "prediction", "predictions",
}


def _clean_words(text: str) -> list[str]:
    """Split text into lowercase words with punctuation stripped."""
    import re as _re
    return _re.findall(r'[a-z0-9]+', text.lower())


def _extract_bigrams(text: str) -> set[str]:
    """Extract 2-word phrases from text, excluding stopwords-only pairs."""
    words = [w for w in _clean_words(text) if w not in _STOPWORDS and len(w) > 1]
    return {f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)}


def _filter_by_topic_relevance(
    results: list[dict], query: str, min_overlap: float = 0.15
) -> list[dict]:
    """Filter out off-topic results using multi-signal relevance check.

    Uses three signals:
    1. Word overlap — requires ≥2 non-generic matching words
    2. Bigram overlap — 2-word phrases are much stronger domain signals
    3. High-citation papers still need ≥1 domain word match (no blanket bypass)

    All word matching strips punctuation to avoid mismatches like gravity? vs gravity.
    """
    query_words = {w for w in _clean_words(query) if w not in _STOPWORDS and len(w) > 1}
    if not query_words:
        return results

    # Domain-specific words = query words minus generic terms
    domain_words = query_words - _GENERIC_WORDS
    query_bigrams = _extract_bigrams(query)

    filtered = []
    for r in results:
        title = r.get("title", "").lower()
        abstract = r.get("abstract", "").lower()
        text = f"{title} {abstract}"
        text_words = set(_clean_words(text))
        text_bigrams = _extract_bigrams(text)

        # Signal 1: Bigram match (strongest — e.g., "quantum gravity" in title)
        bigram_matches = query_bigrams & text_bigrams
        if bigram_matches:
            filtered.append(r)
            continue

        # Signal 2: Domain-word overlap (non-generic content words)
        domain_overlap = domain_words & text_words
        all_overlap = query_words & text_words

        # High-citation papers: require ≥1 domain word (not a blanket pass)
        cite_count = r.get("citation_count", 0) or 0
        if cite_count > 500 and len(domain_overlap) >= 1:
            filtered.append(r)
            continue

        # Normal papers: require ≥2 matching words with ≥1 being domain-specific,
        # OR fractional overlap above threshold
        if len(domain_overlap) >= 1 and len(all_overlap) >= 2:
            filtered.append(r)
            continue
        if len(all_overlap) / len(query_words) >= min_overlap and len(domain_overlap) >= 1:
            filtered.append(r)
            continue

    if filtered:
        return filtered
    # If filter would remove everything, return originals
    return results


def search_papers(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    mailto: str | None = None,
) -> list[dict]:
    """Search for academic papers across multiple free APIs.

    Tries Crossref first (most reliable), then arXiv (STEM preprints),
    then Semantic Scholar. Deduplicates by title.

    Args:
        mailto: Email for Crossref polite pool (uses owner's email for better rate limits).

    Returns list of dicts with keys:
        title, abstract, authors, year, citation_count, url, doi, source
    """
    results = []
    seen_titles: set[str] = set()

    def _dedup_add(hits: list[dict]) -> None:
        for h in hits:
            key = h["title"].lower()[:50]
            if key not in seen_titles and h["title"]:
                seen_titles.add(key)
                results.append(h)

    # 1. Crossref (most reliable, no key needed)
    try:
        crossref_results = _search_crossref(query, limit=limit, year_from=year_from, mailto=mailto)
        crossref_results = _filter_by_topic_relevance(crossref_results, query)
        _dedup_add(crossref_results)
        logger.info("Crossref: %d results (after relevance filter)", len(crossref_results))
    except Exception as e:
        logger.warning("Crossref search failed: %s", e)

    # 2. arXiv (STEM preprints, completely open)
    if len(results) < limit:
        try:
            arxiv_results = _search_arxiv(query, limit=limit - len(results))
            _dedup_add(arxiv_results)
            logger.info("arXiv: %d results", len(arxiv_results))
        except Exception as e:
            logger.warning("arXiv search failed: %s", e)

    # 3. Semantic Scholar (broad coverage, rate-limited)
    if len(results) < limit:
        try:
            s2_results = _search_semantic_scholar(
                query, limit=limit - len(results), year_from=year_from
            )
            _dedup_add(s2_results)
            logger.info("Semantic Scholar: %d results", len(s2_results))
        except Exception as e:
            logger.warning("Semantic Scholar search failed: %s", e)

    return results[:limit]


def search_papers_balanced(
    queries: list[str],
    tradition_labels: list[str] | None = None,
    limit_per_query: int = 10,
    year_from: int | None = None,
    mailto: str | None = None,
) -> dict:
    """Search for papers across multiple queries with coverage tracking.

    Designed for review papers that span multiple research traditions.
    Returns results grouped by query/tradition with coverage statistics.

    Args:
        queries: List of search query strings.
        tradition_labels: Optional labels for each query (e.g., "genomics", "paleontology").
            Must match length of queries if provided.
        limit_per_query: Max results per individual query.
        year_from: Minimum publication year.
        mailto: Email for Crossref polite pool.

    Returns dict with keys:
        results: list of all papers (deduplicated)
        by_tradition: dict mapping tradition label to list of papers
        coverage_report: dict with per-tradition counts and warnings
        total_hits_per_query: dict mapping query to raw hit count
    """
    if tradition_labels and len(tradition_labels) != len(queries):
        raise ValueError("tradition_labels must match length of queries")
    if not tradition_labels:
        tradition_labels = [f"query_{i}" for i in range(len(queries))]

    all_results: list[dict] = []
    seen_titles: set[str] = set()
    by_tradition: dict[str, list[dict]] = {}
    hits_per_query: dict[str, int] = {}

    for query, label in zip(queries, tradition_labels):
        hits = search_papers(query, limit=limit_per_query, year_from=year_from, mailto=mailto)
        hits_per_query[query] = len(hits)

        if label not in by_tradition:
            by_tradition[label] = []

        for h in hits:
            key = h["title"].lower()[:50]
            if key not in seen_titles and h["title"]:
                seen_titles.add(key)
                all_results.append(h)
                by_tradition[label].append(h)

    # Coverage report
    coverage = {}
    total = len(all_results)
    for label, papers in by_tradition.items():
        count = len(papers)
        pct = (count / total * 100) if total > 0 else 0
        coverage[label] = {
            "count": count,
            "percentage": round(pct, 1),
            "warning": "UNDERREPRESENTED" if pct < 10 and total > 20 else None,
        }

    return {
        "results": all_results,
        "by_tradition": by_tradition,
        "coverage_report": coverage,
        "total_hits_per_query": hits_per_query,
    }


def get_paper_details(paper_id: str) -> dict | None:
    """Fetch full paper details from Semantic Scholar by paper ID or DOI."""
    try:
        _s2_throttle()
        with httpx.Client(timeout=15, headers=_s2_headers()) as client:
            resp = client.get(
                f"{_S2_BASE}/paper/{paper_id}",
                params={"fields": f"{_S2_FIELDS},tldr"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return _normalize_s2_result(data)
    except Exception as e:
        logger.warning("Paper details fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Crossref REST API
# ---------------------------------------------------------------------------

def _search_crossref(
    query: str, limit: int = 10, year_from: int | None = None, mailto: str | None = None,
) -> list[dict]:
    """Search Crossref REST API (free, no key, polite pool with mailto).

    Docs: https://api.crossref.org/swagger-ui/index.html
    """
    params: dict = {
        "query": query,
        "rows": min(limit, 50),
        "sort": "relevance",
        "order": "desc",
        "mailto": mailto or _CROSSREF_MAILTO,
        "select": "DOI,title,author,published-print,published-online,abstract,is-referenced-by-count,URL,container-title",
    }
    # Only return journal articles, proceedings articles, and book chapters (not editorial letters, student theses, etc.)
    type_filter = "type:journal-article,type:proceedings-article,type:book-chapter"
    if year_from:
        params["filter"] = f"from-pub-date:{year_from},{type_filter}"
    else:
        params["filter"] = type_filter

    _throttle("crossref")
    with httpx.Client(timeout=20) as client:
        resp = client.get(f"{_CROSSREF_BASE}/works", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("message", {}).get("items", []):
        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""
        if not title:
            continue

        # Authors
        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                name = f"{given} {family}".strip() if given else family
                authors.append(name)

        # Year from published-print or published-online
        year = None
        for date_field in ("published-print", "published-online"):
            date_parts = item.get(date_field, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                year = date_parts[0][0]
                break

        # Abstract (Crossref sometimes includes it as XML/HTML)
        abstract = item.get("abstract", "")
        if abstract:
            # Strip JATS XML tags if present
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        doi = item.get("DOI", "")
        url = f"https://doi.org/{doi}" if doi else item.get("URL", "")

        # Journal / venue from container-title
        container = item.get("container-title", [])
        venue = container[0] if container else ""

        result = {
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": item.get("is-referenced-by-count", 0),
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "crossref",
        }
        if venue:
            result["venue"] = venue
        results.append(result)

    return results


def _crossref_by_doi(doi: str) -> dict | None:
    """Look up a single DOI via Crossref. Returns a paper dict or None."""
    _throttle("crossref")
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{_CROSSREF_BASE}/works/{doi}",
                params={"mailto": _CROSSREF_MAILTO},
            )
            if resp.status_code != 200:
                return None
            item = resp.json().get("message", {})
    except httpx.HTTPError:
        return None

    title_list = item.get("title", [])
    title = title_list[0] if title_list else ""
    if not title:
        return None

    authors = []
    for a in item.get("author", []):
        given = a.get("given", "")
        family = a.get("family", "")
        if family:
            authors.append(f"{given} {family}".strip() if given else family)

    year = None
    for date_field in ("published-print", "published-online"):
        date_parts = item.get(date_field, {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]
            break

    abstract = item.get("abstract", "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

    container = item.get("container-title", [])
    venue = container[0] if container else ""

    result = {
        "title": title,
        "abstract": abstract[:500],
        "authors": authors[:10],
        "year": year,
        "citation_count": item.get("is-referenced-by-count", 0),
        "url": f"https://doi.org/{doi}",
        "doi": doi,
        "source": "crossref",
    }
    if venue:
        result["venue"] = venue
    return result


# ---------------------------------------------------------------------------
# arXiv API
# ---------------------------------------------------------------------------

def _search_arxiv(query: str, limit: int = 10) -> list[dict]:
    """Search arXiv API (completely open, no key).

    Must respect 3-second delay between requests.
    Returns preprints in physics, math, CS, etc.
    """
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": min(limit, 50),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }

    _throttle("arxiv")
    with httpx.Client(timeout=20) as client:
        resp = client.get(_ARXIV_BASE, params=params)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    results = []

    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
        if not title:
            continue

        # Abstract
        summary_el = entry.find("atom:summary", _ARXIV_NS)
        abstract = summary_el.text.strip().replace("\n", " ") if summary_el is not None and summary_el.text else ""

        # Authors
        authors = []
        for author_el in entry.findall("atom:author", _ARXIV_NS):
            name_el = author_el.find("atom:name", _ARXIV_NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        # Year from published date
        published_el = entry.find("atom:published", _ARXIV_NS)
        year = None
        if published_el is not None and published_el.text:
            year_match = re.match(r"(\d{4})", published_el.text)
            if year_match:
                year = int(year_match.group(1))

        # URL and arXiv ID
        url = ""
        arxiv_id = ""
        for link_el in entry.findall("atom:link", _ARXIV_NS):
            if link_el.get("type") == "text/html" or link_el.get("rel") == "alternate":
                url = link_el.get("href", "")
                break
        id_el = entry.find("atom:id", _ARXIV_NS)
        if id_el is not None and id_el.text:
            url = url or id_el.text.strip()
            # Extract arXiv ID: http://arxiv.org/abs/1234.5678v1 -> 1234.5678
            arxiv_match = re.search(r"(\d{4}\.\d{4,5})", id_el.text)
            if arxiv_match:
                arxiv_id = arxiv_match.group(1)

        # DOI (arXiv entries sometimes have one via arxiv:doi)
        doi_el = entry.find("arxiv:doi", _ARXIV_NS)
        doi = doi_el.text.strip() if doi_el is not None and doi_el.text else ""

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": 0,  # arXiv doesn't provide citation counts
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "arxiv_id": arxiv_id,
            "source": "arxiv",
        })

    return results


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

def _search_semantic_scholar(
    query: str, limit: int = 10, year_from: int | None = None
) -> list[dict]:
    """Search Semantic Scholar API (free, no key required).

    Retries on 429 rate-limit with exponential backoff.
    """
    params: dict = {
        "query": query,
        "limit": min(limit, 100),
        "fields": _S2_FIELDS,
    }
    if year_from:
        params["year"] = f"{year_from}-"

    max_attempts = 3
    for attempt in range(max_attempts):
        _s2_throttle()
        with httpx.Client(timeout=20, headers=_s2_headers()) as client:
            resp = client.get(f"{_S2_BASE}/paper/search", params=params)
            if resp.status_code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.info("Semantic Scholar rate-limited, retrying in %ds...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
    else:
        logger.warning("Semantic Scholar rate-limited after %d attempts", max_attempts)
        return []

    results = []
    for item in data.get("data", []):
        normalized = _normalize_s2_result(item)
        if normalized["title"]:
            results.append(normalized)

    return results


def _normalize_s2_result(item: dict) -> dict:
    """Normalize a Semantic Scholar result to our common format."""
    authors = [a.get("name", "") for a in item.get("authors", [])]
    ext_ids = item.get("externalIds", {}) or {}
    doi = ext_ids.get("DOI", "")

    result = {
        "title": item.get("title", ""),
        "abstract": item.get("abstract", "") or "",
        "authors": authors,
        "year": item.get("year"),
        "citation_count": item.get("citationCount", 0),
        "url": item.get("url", ""),
        "doi": doi,
        "paper_id_s2": item.get("paperId", ""),
        "source": "semantic_scholar",
    }
    # Venue / journal name
    venue = item.get("venue", "")
    if venue:
        result["venue"] = venue
    # Preserve TLDR if available (AI-generated 1-sentence summary)
    tldr = item.get("tldr")
    if tldr and isinstance(tldr, dict):
        result["tldr"] = tldr.get("text", "")
    return result


# ---------------------------------------------------------------------------
# Citation graph — follow references and citations from seed papers
# ---------------------------------------------------------------------------


def fetch_paper_references(paper_id_s2: str, limit: int = 50) -> list[dict]:
    """Fetch papers cited BY a given paper (its reference list).

    Uses Semantic Scholar /paper/{id}/references endpoint.
    Returns normalized paper dicts sorted by citation count (descending).
    """
    fields = "paperId,title,abstract,year,authors,citationCount,url,externalIds,venue"
    params = {"fields": fields, "limit": min(limit, 500)}
    data = None

    for attempt in range(3):
        try:
            _s2_throttle()
            with httpx.Client(timeout=20, headers=_s2_headers()) as client:
                resp = client.get(
                    f"{_S2_BASE}/paper/{paper_id_s2}/references", params=params
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                data = resp.json()
                break
        except Exception as e:
            logger.warning("S2 references fetch failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return []
    else:
        return []

    if not data:
        return []

    results = []
    for item in data.get("data") or []:
        cited = item.get("citedPaper") or {}
        if not cited or not cited.get("title"):
            continue
        results.append(_normalize_s2_result(cited))

    # Sort by citation count with title-relevance tiebreaker
    _sort_with_relevance_tiebreak(results, paper_id_s2)
    return results


def _sort_with_relevance_tiebreak(results: list[dict], source_paper_id: str) -> None:
    """Sort by citation count, but when counts are within 2x, prefer title overlap."""
    if not results:
        return
    # Get the source paper's title for relevance comparison
    source_title_words: set[str] = set()
    for r in results:
        if r.get("paper_id_s2") == source_paper_id:
            source_title_words = set(r.get("title", "").lower().split())
            break
    if not source_title_words:
        # Fallback: use the highest-cited paper's title
        top = max(results, key=lambda x: x.get("citation_count", 0))
        source_title_words = set(top.get("title", "").lower().split())

    def _sort_key(paper: dict) -> tuple[float, float]:
        cites = paper.get("citation_count", 0)
        title_words = set(paper.get("title", "").lower().split())
        overlap = len(title_words & source_title_words) / max(len(source_title_words), 1)
        return (cites, overlap)

    results.sort(key=_sort_key, reverse=True)


def fetch_paper_citations(paper_id_s2: str, limit: int = 50) -> list[dict]:
    """Fetch papers that CITE a given paper (forward citations).

    Uses Semantic Scholar /paper/{id}/citations endpoint.
    Returns normalized paper dicts sorted by citation count (descending).
    """
    fields = "paperId,title,abstract,year,authors,citationCount,url,externalIds,venue"
    params = {"fields": fields, "limit": min(limit, 500)}
    data = None

    for attempt in range(3):
        try:
            _s2_throttle()
            with httpx.Client(timeout=20, headers=_s2_headers()) as client:
                resp = client.get(
                    f"{_S2_BASE}/paper/{paper_id_s2}/citations", params=params
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                data = resp.json()
                break
        except Exception as e:
            logger.warning("S2 citations fetch failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return []
    else:
        return []

    if not data:
        return []

    results = []
    for item in data.get("data") or []:
        citing = item.get("citingPaper") or {}
        if not citing or not citing.get("title"):
            continue
        results.append(_normalize_s2_result(citing))

    _sort_with_relevance_tiebreak(results, paper_id_s2)
    return results


def search_seed_papers(
    query: str, limit: int = 5, mailto: str | None = None
) -> list[dict]:
    """Find seed papers: the most-cited papers on a topic.

    Searches Semantic Scholar first (has citationCount), falls back to
    Crossref. Returns results sorted by citation count.
    """
    results = []

    # Semantic Scholar — best for citation-ranked results
    try:
        s2_results = _search_semantic_scholar(query, limit=limit * 2)
        results.extend(s2_results)
    except Exception as e:
        logger.warning("S2 seed search failed: %s", e)

    # If S2 returned too few, supplement with Crossref
    if len(results) < limit:
        try:
            cr_results = _search_crossref(query, limit=limit, mailto=mailto)
            cr_results = _filter_by_topic_relevance(cr_results, query)
            seen = {r["title"].lower()[:50] for r in results}
            for cr in cr_results:
                if cr["title"].lower()[:50] not in seen:
                    results.append(cr)
                    seen.add(cr["title"].lower()[:50])
        except Exception as e:
            logger.warning("Crossref seed search failed: %s", e)

    # Blended ranking: 60% citation rank, 40% relevance (title overlap with query)
    query_terms = set(query.lower().split())
    results.sort(key=lambda x: _seed_score(x, query_terms), reverse=True)
    return results[:limit]


def _seed_score(paper: dict, query_terms: set[str]) -> float:
    """Score a paper for seed selection: blend citation count, title relevance, and recency."""
    cite_score = min(paper.get("citation_count", 0) / 5000, 1.0)
    title_words = set(paper.get("title", "").lower().split())
    overlap = len(title_words & query_terms) / max(len(query_terms), 1)
    # Recency: papers from last 3 years get up to 0.3 bonus
    year = paper.get("year") or 2020
    current_year = 2026
    age = max(0, current_year - year)
    recency = max(0.0, 1.0 - age / 10)  # 2026→1.0, 2023→0.7, 2020→0.4, 2016→0.0
    return 0.4 * cite_score + 0.3 * overlap + 0.3 * recency


# ---------------------------------------------------------------------------
# Content enrichment — get richer paper text for reading memos
# ---------------------------------------------------------------------------

def enrich_paper_content(paper: dict, max_chars: int = 10000) -> str:
    """Fetch the richest available content for a paper.

    Tries to get FULL PAPER TEXT when possible, not just abstracts:
      1. arXiv HTML full text (free, most CS/ML/AI papers)
      2. Semantic Scholar (full abstract + TLDR)
      3. Crossref (abstract)

    Returns a text string suitable for the LLM reading memo prompt.
    For arXiv papers, returns abstract + key sections (intro, methodology,
    discussion, conclusion) — the most informative parts for writing about it.
    """
    title = paper.get("title", "Unknown")
    authors = paper.get("authors", [])
    year = paper.get("year", "N/A")
    doi = paper.get("doi", "")
    url = paper.get("url", "")
    existing_abstract = paper.get("abstract", "")

    header = f"Title: {title}\n"
    if authors:
        header += f"Authors: {', '.join(authors[:5])}\n"
    header += f"Year: {year}\n"
    if doi:
        header += f"DOI: {doi}\n"

    # 1. Try full text from free sources
    full_text = None
    full_text_source = ""

    # 1a. arXiv HTML (CS/ML/AI/physics/math papers)
    arxiv_id = _extract_arxiv_id(url)
    if arxiv_id:
        full_text = _fetch_arxiv_full_text(arxiv_id)
        if full_text and len(full_text) > 500:
            full_text_source = "arXiv"

    # 1b. PubMed Central (biomedical/life sciences — millions of free papers)
    if not full_text and doi:
        full_text = _fetch_pmc_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "PMC"

    # 1c. Unpaywall → open access landing page (any discipline)
    if not full_text and doi:
        oa_url = _get_open_access_url(doi)
        if oa_url:
            full_text = _fetch_html_text(oa_url)
            if full_text and len(full_text) > 500:
                full_text_source = "OA"

    # 1d. Direct URL fetch as last resort (journal HTML pages)
    if not full_text and url and "arxiv.org" not in url:
        full_text = _fetch_html_text(url)
        if full_text and len(full_text) > 500:
            full_text_source = "URL"

    if full_text and len(full_text) > 500:
        sections = _extract_key_sections(full_text)
        body = "\n\n".join(sections)
        result = header + "\n" + body
        if len(result) > max_chars:
            result = result[:max_chars] + "\n[... truncated]"
        logger.info("Enriched '%s' with %s full text: %d chars", title[:40], full_text_source, len(result))
        return result

    # 2. Semantic Scholar (full abstract + TLDR)
    best_abstract = existing_abstract
    tldr = ""
    s2_id = paper.get("paper_id_s2", "")
    lookup_key = f"DOI:{doi}" if doi else (s2_id if s2_id else None)

    if lookup_key:
        try:
            details = _fetch_s2_enriched(lookup_key)
            if details:
                s2_abstract = details.get("abstract", "")
                if s2_abstract and len(s2_abstract) > len(best_abstract):
                    best_abstract = s2_abstract
                tldr = details.get("tldr", "")
        except Exception:
            pass

    # 2b. If S2 DOI lookup failed, try title search
    if len(best_abstract) < 100 and title and title != "Unknown":
        try:
            title_result = lookup_by_title_s2(title)
            if title_result:
                t_abstract = title_result.get("abstract", "")
                if t_abstract and len(t_abstract) > len(best_abstract):
                    best_abstract = t_abstract
                t_tldr = title_result.get("tldr", "")
                if t_tldr and not tldr:
                    tldr = t_tldr
        except Exception:
            pass

    # 3. Crossref fallback for abstract
    if len(best_abstract) < 200 and doi:
        try:
            cr = _crossref_by_doi(doi)
            if cr:
                cr_abstract = cr.get("abstract", "")
                if cr_abstract and len(cr_abstract) > len(best_abstract):
                    best_abstract = cr_abstract
        except Exception:
            pass

    # 4. arXiv API fallback for abstract (if not full text but is arXiv)
    if len(best_abstract) < 100 and arxiv_id:
        try:
            arxiv_abstract = _fetch_arxiv_abstract(arxiv_id)
            if arxiv_abstract and len(arxiv_abstract) > len(best_abstract):
                best_abstract = arxiv_abstract
        except Exception:
            pass

    # Build enriched text from abstract + TLDR
    parts = [header]
    if best_abstract:
        parts.append(f"Abstract: {best_abstract}")
    if tldr:
        parts.append(f"Key finding (TLDR): {tldr}")

    return "\n\n".join(parts)


def _fetch_arxiv_full_text(arxiv_id: str) -> str | None:
    """Fetch full paper text from arXiv HTML.

    Tries arxiv.org/html/ first (newer papers), then ar5iv.org (older papers).
    Returns cleaned plain text, or None if unavailable.
    """
    urls = [
        f"https://arxiv.org/html/{arxiv_id}",
        f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
    ]

    for url in urls:
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code != 200 or len(resp.text) < 5000:
                    continue

                # Strip scripts, styles, then HTML tags
                text = resp.text
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                # Preserve section headings as markers
                text = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n\n## \1\n", text, flags=re.DOTALL)
                # Strip remaining tags
                text = re.sub(r"<[^>]+>", " ", text)
                # Clean whitespace
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = text.strip()

                if len(text) > 1000:
                    logger.debug("Fetched arXiv full text from %s: %d chars", url, len(text))
                    return text
        except Exception as e:
            logger.debug("arXiv HTML fetch failed for %s: %s", url, e)
            continue

    return None


def _fetch_pmc_full_text(doi: str) -> str | None:
    """Fetch full text from PubMed Central via NCBI API.

    PMC has free full text for millions of biomedical/life science papers.
    Uses the NCBI ID converter to find PMC ID from DOI, then fetches text.
    """
    try:
        with httpx.Client(timeout=15) as client:
            # Step 1: Convert DOI to PMCID
            conv_resp = client.get(
                "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                params={"ids": doi, "format": "json", "tool": "agentpub", "email": "api@agentpub.org"},
            )
            if conv_resp.status_code != 200:
                return None
            records = conv_resp.json().get("records", [])
            if not records:
                return None
            pmcid = records[0].get("pmcid", "")
            if not pmcid:
                return None

            # Step 2: Fetch the full text HTML from PMC
            html_resp = client.get(
                f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
                headers={"Accept": "text/html"},
                follow_redirects=True,
            )
            if html_resp.status_code != 200 or len(html_resp.text) < 5000:
                return None

            # Clean HTML to text
            text = _html_to_text(html_resp.text)
            if len(text) > 1000:
                logger.debug("Fetched PMC full text for %s: %d chars", doi, len(text))
                return text
    except Exception as e:
        logger.debug("PMC fetch failed for %s: %s", doi, e)
    return None


def _get_open_access_url(doi: str) -> str | None:
    """Check Unpaywall for a free full-text URL.

    Unpaywall covers 30M+ papers and tells us if there's a free version.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": "api@agentpub.org"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("is_oa"):
                return None
            best = data.get("best_oa_location", {})
            if not best:
                return None
            # Prefer landing page (HTML) over PDF
            return best.get("url_for_landing_page") or best.get("url_for_pdf")
    except Exception as e:
        logger.debug("Unpaywall check failed for %s: %s", doi, e)
    return None


def _fetch_html_text(url: str) -> str | None:
    """Fetch a URL and extract text from HTML.

    Used as a fallback for journal pages that may have full text in HTML.
    Respects robots.txt spirit — only fetches the page itself, no crawling.
    """
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={
                    "User-Agent": "AgentPub/1.0 (academic research; api@agentpub.org)",
                    "Accept": "text/html",
                },
            )
            if resp.status_code != 200:
                return None
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                return None  # Skip PDFs, images, etc.
            if len(resp.text) < 2000:
                return None

            text = _html_to_text(resp.text)
            if len(text) > 500:
                return text
    except Exception as e:
        logger.debug("HTML fetch failed for %s: %s", url[:60], e)
    return None


def _html_to_text(html: str) -> str:
    """Convert HTML to clean plain text, preserving section structure."""
    text = html
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL)
    text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL)
    # Preserve headings
    text = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n\n## \1\n", text, flags=re.DOTALL)
    # Paragraph breaks
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Clean whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_key_sections(full_text: str, max_chars: int = 9000) -> list[str]:
    """Extract the most informative sections from a full paper.

    Prioritizes: Abstract > Introduction > Methodology/Methods >
    Discussion > Conclusion > Related Work.
    Skips raw results tables and appendices.
    """
    # Split on section headings (## markers from HTML extraction)
    sections: list[tuple[str, str]] = []
    current_heading = "Preamble"
    current_content: list[str] = []

    for line in full_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_content:
                sections.append((current_heading, "\n".join(current_content).strip()))
            current_heading = stripped[3:].strip()
            current_content = []
        else:
            current_content.append(line)

    if current_content:
        sections.append((current_heading, "\n".join(current_content).strip()))

    # Priority order for sections (most useful for writing about the paper)
    priority_keywords = [
        "abstract",
        "introduction",
        "method", "approach", "framework", "design",
        "discussion", "analysis",
        "conclusion", "summary",
        "related work", "background", "literature",
        "limitation",
    ]

    # Score each section by priority
    scored: list[tuple[int, str, str]] = []
    for heading, content in sections:
        if len(content) < 50:
            continue
        heading_lower = heading.lower()
        # Skip non-content sections
        if any(skip in heading_lower for skip in (
            "appendix", "acknowledg", "reference", "bibliograph", "supplement",
            "github", "issue", "footer", "header", "navigation", "menu",
            "copyright", "license", "author info",
        )):
            continue
        score = len(priority_keywords)  # default: lowest priority
        for i, kw in enumerate(priority_keywords):
            if kw in heading_lower:
                score = i
                break
        scored.append((score, heading, content))

    scored.sort(key=lambda x: x[0])

    # Take sections up to max_chars
    result: list[str] = []
    total = 0
    for _score, heading, content in scored:
        # Truncate long sections (e.g., Results with huge tables)
        section_text = f"## {heading}\n{content[:2500]}"
        if total + len(section_text) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                result.append(section_text[:remaining] + "\n[... truncated]")
            break
        result.append(section_text)
        total += len(section_text)

    return result


def _fetch_s2_enriched(paper_id: str) -> dict | None:
    """Fetch enriched details from Semantic Scholar (abstract + tldr)."""
    try:
        _s2_throttle()
        with httpx.Client(timeout=10, headers=_s2_headers()) as client:
            resp = client.get(
                f"{_S2_BASE}/paper/{paper_id}",
                params={"fields": "abstract,tldr"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            result = {"abstract": data.get("abstract", "") or ""}
            tldr = data.get("tldr")
            if tldr and isinstance(tldr, dict):
                result["tldr"] = tldr.get("text", "")
            return result
    except Exception:
        return None


def _extract_arxiv_id(url: str) -> str | None:
    """Extract arXiv ID from a URL like https://arxiv.org/abs/2301.12345."""
    if not url:
        return None
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/(\d+\.\d+(?:v\d+)?)", url)
    return match.group(1) if match else None


def _fetch_arxiv_abstract(arxiv_id: str) -> str:
    """Fetch the full abstract for an arXiv paper."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _ARXIV_BASE,
                params={"id_list": arxiv_id, "max_results": "1"},
            )
            if resp.status_code != 200:
                return ""
            root = ET.fromstring(resp.text)
            entry = root.find("atom:entry", _ARXIV_NS)
            if entry is None:
                return ""
            summary_el = entry.find("atom:summary", _ARXIV_NS)
            if summary_el is not None and summary_el.text:
                return summary_el.text.strip().replace("\n", " ")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_year(text: str) -> int | None:
    match = re.search(r"\b(19[9]\d|20[0-2]\d)\b", text)
    return int(match.group(1)) if match else None


def _extract_doi(url: str) -> str | None:
    if not url:
        return None
    doi_match = re.search(r"(10\.\d{4,}/[^\s]+)", url)
    return doi_match.group(1) if doi_match else None


def lookup_by_title_s2(title: str) -> dict | None:
    """Look up a paper by exact title via Semantic Scholar. Returns normalized record or None."""
    try:
        _s2_throttle()
        with httpx.Client(timeout=10, headers=_s2_headers()) as client:
            resp = client.get(
                f"{_S2_BASE}/paper/search",
                params={
                    "query": title[:200],
                    "limit": 3,
                    "fields": _S2_FIELDS,
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except httpx.HTTPError:
        return None

    papers = data.get("data", [])
    if not papers:
        return None

    # Return the best title match
    from difflib import SequenceMatcher
    norm_title = title.lower().strip()
    best = None
    best_sim = 0.0
    for p in papers:
        sim = SequenceMatcher(None, norm_title, (p.get("title", "") or "").lower().strip()).ratio()
        if sim > best_sim:
            best_sim = sim
            best = p

    if best is None or best_sim < 0.5:
        return None

    return _normalize_s2_result(best)


def lookup_by_title_openalex(title: str) -> dict | None:
    """Look up a paper by title via OpenAlex. Returns normalized record or None."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://api.openalex.org/works",
                params={
                    "search": title[:200],
                    "per_page": 3,
                    "select": "title,authorships,publication_year,doi",
                },
                headers={"User-Agent": "AgentPub/1.0 (mailto:api@agentpub.org)"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except httpx.HTTPError:
        return None

    works = data.get("results", [])
    if not works:
        return None

    from difflib import SequenceMatcher
    norm_title = title.lower().strip()
    best = None
    best_sim = 0.0
    for w in works:
        sim = SequenceMatcher(None, norm_title, (w.get("title", "") or "").lower().strip()).ratio()
        if sim > best_sim:
            best_sim = sim
            best = w

    if best is None or best_sim < 0.5:
        return None

    authors = []
    for authorship in best.get("authorships", []):
        name = authorship.get("author", {}).get("display_name", "")
        if name:
            authors.append(name)

    doi = best.get("doi", "")
    if doi and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]

    return {
        "title": best.get("title", ""),
        "abstract": "",
        "authors": authors,
        "year": best.get("publication_year"),
        "citation_count": 0,
        "url": f"https://doi.org/{doi}" if doi else "",
        "doi": doi,
        "paper_id_s2": "",
        "source": "openalex",
    }


# ---------------------------------------------------------------------------
# Serper.dev Google Scholar (optional, requires API key)
# ---------------------------------------------------------------------------

def search_serper_scholar(
    query: str,
    api_key: str,
    limit: int = 10,
) -> list[dict]:
    """Search Google Scholar via Serper.dev API.

    Requires a Serper.dev API key. Free tier: 2,500 queries.
    Returns results in the same format as other search functions.
    """
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            "https://google.serper.dev/scholar",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": min(limit, 20)},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("organic", []):
        title = item.get("title", "")
        if not title:
            continue

        # Parse authors from publication info
        pub_info = item.get("publication_info", {})
        authors_str = pub_info.get("authors", [])
        if isinstance(authors_str, str):
            authors = [a.strip() for a in authors_str.split(",")]
        elif isinstance(authors_str, list):
            authors = [a.get("name", a) if isinstance(a, dict) else str(a) for a in authors_str]
        else:
            authors = []

        # Extract year from publication info summary
        year = None
        summary = pub_info.get("summary", "")
        if summary:
            year_match = re.search(r"\b(19\d{2}|20[0-2]\d)\b", summary)
            if year_match:
                year = int(year_match.group(1))

        # Snippet as abstract
        abstract = item.get("snippet", "")

        # Citation count
        cited_str = item.get("inline_links", {}).get("cited_by", {}).get("total", "")
        citation_count = int(cited_str) if str(cited_str).isdigit() else 0

        url = item.get("link", "")
        doi = _extract_doi(url) or ""

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "serper_scholar",
        })

    return results[:limit]


# ---------------------------------------------------------------------------
# OpenAlex Search (full search, not just title lookup)
# ---------------------------------------------------------------------------

def _reconstruct_openalex_abstract(inverted_index: dict) -> str:
    """Reconstruct abstract from OpenAlex inverted index format.

    OpenAlex stores abstracts as {word: [position, ...], ...}.
    Reconstruct by placing each word at its positions.
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                word_positions.append((pos, word))
    word_positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in word_positions)


def _search_openalex(
    query: str, limit: int = 10, mailto: str | None = None,
) -> list[dict]:
    """Search OpenAlex API (free, optional API key for higher rate limits).

    Docs: https://docs.openalex.org/api-entities/works
    """
    params: dict = {
        "search": query,
        "per_page": min(limit, 50),
        "sort": "cited_by_count:desc",
        "mailto": mailto or _CROSSREF_MAILTO,
    }
    headers: dict = {
        "User-Agent": "AgentPub/1.0 (mailto:api@agentpub.org)",
    }
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    _throttle("openalex")
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.get("https://api.openalex.org/works", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", []):
        title = item.get("title", "")
        if not title:
            continue

        # Authors from authorships
        authors = []
        for authorship in item.get("authorships", []):
            name = authorship.get("author", {}).get("display_name", "")
            if name:
                authors.append(name)

        # DOI — strip https://doi.org/ prefix
        doi = item.get("doi", "") or ""
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        # Abstract from inverted index
        abstract = _reconstruct_openalex_abstract(
            item.get("abstract_inverted_index", {})
        )

        year = item.get("publication_year")
        citation_count = item.get("cited_by_count", 0)
        url = f"https://doi.org/{doi}" if doi else item.get("id", "")

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "openalex",
        })

    return results


# ---------------------------------------------------------------------------
# PubMed / NCBI E-utilities Search
# ---------------------------------------------------------------------------

def _pubmed_throttle() -> None:
    """Wait if needed to respect PubMed rate limits."""
    _throttle("pubmed")


def _search_pubmed(query: str, limit: int = 10) -> list[dict]:
    """Search PubMed via NCBI E-utilities (free, optional API key).

    Two-step process: esearch for IDs, then efetch for details.
    Docs: https://www.ncbi.nlm.nih.gov/books/NBK25500/
    """
    api_key = os.environ.get("NCBI_API_KEY", "")
    base_params: dict = {}
    if api_key:
        base_params["api_key"] = api_key

    # Step 1: Search for PubMed IDs
    search_params = {
        **base_params,
        "db": "pubmed",
        "term": query,
        "retmax": min(limit, 50),
        "retmode": "json",
        "sort": "relevance",
    }

    _pubmed_throttle()
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=search_params,
        )
        resp.raise_for_status()
        search_data = resp.json()

    id_list = search_data.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    # Step 2: Fetch details for each ID
    fetch_params = {
        **base_params,
        "db": "pubmed",
        "id": ",".join(id_list),
        "rettype": "abstract",
        "retmode": "xml",
    }

    _pubmed_throttle()
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params=fetch_params,
        )
        resp.raise_for_status()
        xml_text = resp.text

    # Parse the XML response
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("PubMed XML parse failed")
        return []

    for article_el in root.findall(".//PubmedArticle"):
        medline = article_el.find(".//MedlineCitation")
        if medline is None:
            continue
        article = medline.find("Article")
        if article is None:
            continue

        # Title
        title_el = article.find("ArticleTitle")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        if not title:
            continue

        # Authors
        authors = []
        author_list = article.find("AuthorList")
        if author_list is not None:
            for author_el in author_list.findall("Author"):
                last = author_el.find("LastName")
                fore = author_el.find("ForeName")
                last_name = last.text.strip() if last is not None and last.text else ""
                fore_name = fore.text.strip() if fore is not None and fore.text else ""
                if last_name:
                    name = f"{fore_name} {last_name}".strip() if fore_name else last_name
                    authors.append(name)

        # Year from PubDate
        year = None
        pub_date = article.find(".//PubDate")
        if pub_date is not None:
            year_el = pub_date.find("Year")
            if year_el is not None and year_el.text:
                try:
                    year = int(year_el.text)
                except ValueError:
                    pass
            if year is None:
                medline_date = pub_date.find("MedlineDate")
                if medline_date is not None and medline_date.text:
                    year_match = re.search(r"(\d{4})", medline_date.text)
                    if year_match:
                        year = int(year_match.group(1))

        # Abstract
        abstract_el = article.find(".//AbstractText")
        abstract = abstract_el.text.strip() if abstract_el is not None and abstract_el.text else ""

        # DOI from ArticleIdList
        doi = ""
        pubmed_data = article_el.find(".//PubmedData")
        if pubmed_data is not None:
            for aid in pubmed_data.findall(".//ArticleId"):
                if aid.get("IdType") == "doi" and aid.text:
                    doi = aid.text.strip()
                    break

        pmid_el = medline.find("PMID")
        pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": 0,  # PubMed doesn't provide citation counts
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "pubmed",
        })

    return results


# ---------------------------------------------------------------------------
# Europe PMC Search
# ---------------------------------------------------------------------------

def _search_europe_pmc(query: str, limit: int = 10) -> list[dict]:
    """Search Europe PMC (free, no API key needed).

    Docs: https://europepmc.org/RestfulWebService
    """
    params: dict = {
        "query": query,
        "resultType": "core",
        "pageSize": min(limit, 25),
        "format": "json",
        "sort": "CITED desc",
    }

    _throttle("europe_pmc")
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("resultList", {}).get("result", []):
        title = item.get("title", "")
        if not title:
            continue

        # Authors — authorString is comma-separated
        author_str = item.get("authorString", "")
        authors = [a.strip() for a in author_str.split(",") if a.strip()] if author_str else []

        # Year
        year = None
        pub_year = item.get("pubYear")
        if pub_year:
            try:
                year = int(pub_year)
            except (ValueError, TypeError):
                pass

        abstract = item.get("abstractText", "") or ""
        doi = item.get("doi", "") or ""
        citation_count = item.get("citedByCount", 0) or 0
        url = f"https://doi.org/{doi}" if doi else ""

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "europe_pmc",
        })

    return results


# ---------------------------------------------------------------------------
# CORE Search (requires API key)
# ---------------------------------------------------------------------------

def _core_throttle() -> None:
    """Wait if needed to respect CORE rate limits."""
    _throttle("core")


def _search_core(query: str, limit: int = 10) -> list[dict]:
    """Search CORE API (requires CORE_API_KEY env var).

    Docs: https://core.ac.uk/documentation/api
    """
    api_key = os.environ.get("CORE_API_KEY", "")
    if not api_key:
        return []

    params: dict = {
        "q": query,
        "limit": min(limit, 50),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    _core_throttle()
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.get(
            "https://api.core.ac.uk/v3/search/works",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", []):
        title = item.get("title", "")
        if not title:
            continue

        # Authors
        authors = []
        for author in item.get("authors", []):
            name = author.get("name", "") if isinstance(author, dict) else str(author)
            if name:
                authors.append(name)

        year = None
        year_pub = item.get("yearPublished")
        if year_pub:
            try:
                year = int(year_pub)
            except (ValueError, TypeError):
                pass

        abstract = item.get("abstract", "") or ""
        doi = item.get("doi", "") or ""
        url = f"https://doi.org/{doi}" if doi else item.get("downloadUrl", "")

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": 0,  # CORE doesn't provide citation counts
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "core",
        })

    return results


# ---------------------------------------------------------------------------
# Dimensions Metrics API (DOI lookup only, requires API key)
# ---------------------------------------------------------------------------

def lookup_by_doi_dimensions(doi: str) -> dict | None:
    """Look up citation metrics for a DOI via Dimensions free Metrics API.

    Requires DIMENSIONS_API_KEY env var. Returns citation metrics or None.
    Docs: https://metrics-api.dimensions.ai/
    """
    api_key = os.environ.get("DIMENSIONS_API_KEY", "")
    if not api_key or not doi:
        return None

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://metrics-api.dimensions.ai/doi/{doi}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

        return {
            "doi": doi,
            "times_cited": data.get("times_cited", 0),
            "recent_citations": data.get("recent_citations", 0),
            "field_citation_ratio": data.get("field_citation_ratio", 0),
            "source": "dimensions",
        }
    except Exception as e:
        logger.warning("Dimensions lookup failed for %s: %s", doi, e)
        return None


# ---------------------------------------------------------------------------
# Lens.org Search (requires API key)
# ---------------------------------------------------------------------------

def _search_lens(query: str, limit: int = 10) -> list[dict]:
    """Search Lens.org scholarly API (requires LENS_API_KEY env var).

    Docs: https://docs.api.lens.org/
    """
    api_key = os.environ.get("LENS_API_KEY", "")
    if not api_key:
        return []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "query": {
            "match": {
                "query_string": query,
            },
        },
        "size": min(limit, 50),
        "sort": [{"year_published": "desc"}],
    }

    _throttle("lens")
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.post(
            "https://api.lens.org/scholarly/search",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("data", []):
        title = item.get("title", "")
        if not title:
            continue

        # Authors
        authors = []
        for author in item.get("authors", []):
            first = author.get("first_name", "")
            last = author.get("last_name", "")
            if last:
                name = f"{first} {last}".strip() if first else last
                authors.append(name)

        year = item.get("year_published")

        # Abstract
        abstract_obj = item.get("abstract", "")
        if isinstance(abstract_obj, dict):
            abstract = abstract_obj.get("text", "")
        else:
            abstract = str(abstract_obj) if abstract_obj else ""

        # DOI from external_ids
        doi = ""
        for ext_id in item.get("external_ids", []):
            if isinstance(ext_id, dict) and ext_id.get("type") == "doi":
                doi = ext_id.get("value", "")
                break

        citation_count = item.get("scholarly_citations_count", 0) or 0
        url = f"https://doi.org/{doi}" if doi else ""

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "lens",
        })

    return results


# ---------------------------------------------------------------------------
# Scopus Search (requires API key)
# ---------------------------------------------------------------------------

def _search_scopus(query: str, limit: int = 10) -> list[dict]:
    """Search Scopus via Elsevier API (requires SCOPUS_API_KEY env var).

    Docs: https://dev.elsevier.com/documentation/SCOPUSSearchAPI.wadl
    """
    api_key = os.environ.get("SCOPUS_API_KEY", "")
    if not api_key:
        return []

    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
    }
    params: dict = {
        "query": f"TITLE-ABS-KEY({query})",
        "count": min(limit, 25),
        "sort": "citedby-count",
    }

    _throttle("scopus")
    with httpx.Client(timeout=20, headers=headers) as client:
        resp = client.get(
            "https://api.elsevier.com/content/search/scopus",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("search-results", {}).get("entry", []):
        title = item.get("dc:title", "")
        if not title:
            continue

        # Authors
        authors = []
        for author in item.get("author", []):
            name = author.get("authname", "")
            if name:
                authors.append(name)

        # Year from prism:coverDate (format: YYYY-MM-DD)
        year = None
        cover_date = item.get("prism:coverDate", "")
        if cover_date:
            year_match = re.match(r"(\d{4})", cover_date)
            if year_match:
                year = int(year_match.group(1))

        abstract = item.get("dc:description", "") or ""
        doi = item.get("prism:doi", "") or ""
        citation_count = 0
        cited_str = item.get("citedby-count", "0")
        try:
            citation_count = int(cited_str)
        except (ValueError, TypeError):
            pass

        url = f"https://doi.org/{doi}" if doi else item.get("prism:url", "")

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "scopus",
        })

    return results


# ---------------------------------------------------------------------------
# Extended multi-source search
# ---------------------------------------------------------------------------

# Source registry for get_configured_sources()
_SOURCE_REGISTRY: list[dict] = [
    {
        "name": "crossref",
        "display_name": "Crossref",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://www.crossref.org/",
    },
    {
        "name": "arxiv",
        "display_name": "arXiv",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://arxiv.org/",
    },
    {
        "name": "semantic_scholar",
        "display_name": "Semantic Scholar",
        "requires_key": False,
        "env_var": "S2_API_KEY",
        "free": True,
        "url": "https://www.semanticscholar.org/",
    },
    {
        "name": "openalex",
        "display_name": "OpenAlex",
        "requires_key": False,
        "env_var": "OPENALEX_API_KEY",
        "free": True,
        "url": "https://openalex.org/",
    },
    {
        "name": "pubmed",
        "display_name": "PubMed / NCBI",
        "requires_key": False,
        "env_var": "NCBI_API_KEY",
        "free": True,
        "url": "https://pubmed.ncbi.nlm.nih.gov/",
    },
    {
        "name": "europe_pmc",
        "display_name": "Europe PMC",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://europepmc.org/",
    },
    {
        "name": "core",
        "display_name": "CORE",
        "requires_key": True,
        "env_var": "CORE_API_KEY",
        "free": False,
        "url": "https://core.ac.uk/",
    },
    {
        "name": "lens",
        "display_name": "Lens.org",
        "requires_key": True,
        "env_var": "LENS_API_KEY",
        "free": False,
        "url": "https://www.lens.org/",
    },
    {
        "name": "scopus",
        "display_name": "Scopus",
        "requires_key": True,
        "env_var": "SCOPUS_API_KEY",
        "free": False,
        "url": "https://www.scopus.com/",
    },
    {
        "name": "serper",
        "display_name": "Serper.dev (Google Scholar)",
        "requires_key": True,
        "env_var": "SERPER_API_KEY",
        "free": False,
        "url": "https://serper.dev/",
    },
]


def get_configured_sources() -> list[dict]:
    """Return list of all academic sources with their configuration status.

    Returns list of dicts with: name, display_name, configured (bool),
    requires_key (bool), env_var (str or None), free (bool), url (str)
    """
    result = []
    for src in _SOURCE_REGISTRY:
        entry = dict(src)  # copy
        if src["env_var"]:
            entry["configured"] = bool(os.environ.get(src["env_var"], ""))
        else:
            entry["configured"] = True  # no key needed = always configured
        result.append(entry)
    return result


def search_papers_extended(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    mailto: str | None = None,
    sources: list[str] | None = None,
) -> list[dict]:
    """Search across all configured academic APIs.

    Args:
        query: Search query string.
        limit: Maximum number of results to return.
        year_from: Minimum publication year filter (where supported).
        mailto: Email for polite pool (Crossref, OpenAlex).
        sources: Which sources to use. If None, uses all configured sources.
            Options: crossref, arxiv, semantic_scholar, openalex, pubmed,
            europe_pmc, core, lens, scopus, serper
    """
    # Default sources: all free ones
    default_sources = ["crossref", "arxiv", "semantic_scholar", "openalex", "pubmed", "europe_pmc"]

    # Add optional sources if their API key is configured
    if os.environ.get("CORE_API_KEY"):
        default_sources.append("core")
    if os.environ.get("LENS_API_KEY"):
        default_sources.append("lens")
    if os.environ.get("SCOPUS_API_KEY"):
        default_sources.append("scopus")
    if os.environ.get("SERPER_API_KEY"):
        default_sources.append("serper")

    active_sources = sources if sources is not None else default_sources

    results: list[dict] = []
    seen_titles: set[str] = set()

    def _dedup_add(hits: list[dict]) -> None:
        for h in hits:
            key = h["title"].lower()[:50]
            if key not in seen_titles and h["title"]:
                seen_titles.add(key)
                results.append(h)

    # Dispatch table
    for source in active_sources:
        if len(results) >= limit:
            break
        remaining = limit - len(results)
        try:
            if source == "crossref":
                _dedup_add(_search_crossref(query, limit=remaining, year_from=year_from, mailto=mailto))
            elif source == "arxiv":
                _dedup_add(_search_arxiv(query, limit=remaining))
            elif source == "semantic_scholar":
                _dedup_add(_search_semantic_scholar(query, limit=remaining, year_from=year_from))
            elif source == "openalex":
                _dedup_add(_search_openalex(query, limit=remaining, mailto=mailto))
            elif source == "pubmed":
                _dedup_add(_search_pubmed(query, limit=remaining))
            elif source == "europe_pmc":
                _dedup_add(_search_europe_pmc(query, limit=remaining))
            elif source == "core":
                _dedup_add(_search_core(query, limit=remaining))
            elif source == "lens":
                _dedup_add(_search_lens(query, limit=remaining))
            elif source == "scopus":
                _dedup_add(_search_scopus(query, limit=remaining))
            elif source == "serper":
                serper_key = os.environ.get("SERPER_API_KEY", "")
                if serper_key:
                    _dedup_add(search_serper_scholar(query, api_key=serper_key, limit=remaining))
            else:
                logger.warning("Unknown source: %s", source)
            logger.info("Extended search [%s]: %d total results so far", source, len(results))
        except Exception as e:
            logger.warning("Extended search [%s] failed: %s", source, e)

    return results[:limit]


# ---------------------------------------------------------------------------
# Human-like research pipeline — survey-first, claim-targeted search
# ---------------------------------------------------------------------------


def search_survey_papers(
    query: str, limit: int = 5, year_from: int = 2022, mailto: str | None = None,
) -> list[dict]:
    """Find recent review/survey papers on a topic.

    Uses OpenAlex type:review filter + explicit "review"/"survey" query terms
    via Semantic Scholar. Returns papers sorted by citation count.
    """
    results: list[dict] = []
    seen: set[str] = set()

    def _dedup(papers: list[dict]) -> None:
        for p in papers:
            key = p.get("title", "").lower()[:60]
            if key and key not in seen:
                seen.add(key)
                results.append(p)

    # 1. OpenAlex with type:review + title_and_abstract.search for precision
    try:
        params: dict = {
            "filter": f"type:review,from_publication_date:{year_from}-01-01,title_and_abstract.search:{query}",
            "per_page": min(limit * 3, 30),
            "sort": "cited_by_count:desc",
            "mailto": mailto or _CROSSREF_MAILTO,
        }
        headers: dict = {"User-Agent": "AgentPub/1.0 (mailto:api@agentpub.org)"}
        api_key = os.environ.get("OPENALEX_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        _throttle("openalex")
        with httpx.Client(timeout=20, headers=headers) as client:
            resp = client.get("https://api.openalex.org/works", params=params)
            if resp.status_code == 200:
                for item in resp.json().get("results", []):
                    title = item.get("title", "")
                    if not title:
                        continue
                    authors = []
                    for a in item.get("authorships", []):
                        name = a.get("author", {}).get("display_name", "")
                        if name:
                            authors.append(name)
                    doi = item.get("doi", "") or ""
                    if doi.startswith("https://doi.org/"):
                        doi = doi[len("https://doi.org/"):]
                    abstract = _reconstruct_openalex_abstract(
                        item.get("abstract_inverted_index", {})
                    )
                    venue = ""
                    src = item.get("primary_location", {}) or {}
                    src_obj = src.get("source", {}) or {}
                    if src_obj.get("display_name"):
                        venue = src_obj["display_name"]
                    paper = {
                        "title": title,
                        "abstract": abstract[:500],
                        "authors": authors[:10],
                        "year": item.get("publication_year"),
                        "citation_count": item.get("cited_by_count", 0),
                        "url": f"https://doi.org/{doi}" if doi else item.get("id", ""),
                        "doi": doi,
                        "paper_id_s2": "",
                        "source": "openalex",
                        "is_survey": True,
                    }
                    if venue:
                        paper["venue"] = venue
                    _dedup([paper])
    except Exception as e:
        logger.warning("OpenAlex survey search failed: %s", e)

    # 2. Semantic Scholar with "review"/"survey" appended
    for suffix in ["review", "survey", "systematic review"]:
        if len(results) >= limit:
            break
        try:
            s2_hits = _search_semantic_scholar(
                f"{query} {suffix}", limit=5, year_from=year_from
            )
            for h in s2_hits:
                h["is_survey"] = True
            _dedup(s2_hits)
        except Exception:
            pass

    # Sort by citation count — most-cited surveys are most useful as maps
    results.sort(key=lambda p: p.get("citation_count", 0), reverse=True)
    return results[:limit]


def extract_references_from_surveys(
    survey_papers: list[dict], limit_per_survey: int = 40,
    topic_terms: set[str] | None = None,
) -> list[dict]:
    """Mine reference lists from survey papers.

    For each survey, fetches its references via S2. Papers cited by multiple
    surveys get a ``cited_by_n_surveys`` count — these are almost certainly
    foundational. Returns the deduplicated union sorted by survey-overlap
    then citation count.

    If *topic_terms* is provided, refs whose titles share zero terms with the
    topic are discarded (prevents off-topic leak from broad survey ref lists).
    """
    ref_counts: dict[str, int] = {}   # title_key -> count of surveys citing it
    ref_map: dict[str, dict] = {}     # title_key -> paper dict

    for survey in survey_papers:
        s2_id = survey.get("paper_id_s2", "")
        if not s2_id:
            # Try to find the S2 ID via title search
            try:
                hits = _search_semantic_scholar(survey.get("title", ""), limit=1)
                if hits:
                    s2_id = hits[0].get("paper_id_s2", "")
            except Exception:
                pass
        if not s2_id:
            continue

        try:
            refs = fetch_paper_references(s2_id, limit=limit_per_survey) or []
            for ref in refs:
                key = ref.get("title", "").lower()[:60]
                if not key:
                    continue
                ref_counts[key] = ref_counts.get(key, 0) + 1
                if key not in ref_map:
                    ref_map[key] = ref
        except Exception as e:
            logger.warning("Failed to extract refs from survey '%s': %s",
                           survey.get("title", "?")[:50], e)

    # Attach survey-overlap count and sort
    results = list(ref_map.values())
    for r in results:
        key = r.get("title", "").lower()[:60]
        r["cited_by_n_surveys"] = ref_counts.get(key, 0)

    # Topic-relevance filter: discard refs with zero title overlap
    if topic_terms:
        _stop = {"the", "and", "for", "with", "from", "that", "this", "are",
                 "was", "were", "has", "have", "been", "its", "not", "but",
                 "can", "all", "may", "will", "one", "two", "new", "use",
                 "via", "how", "does", "into", "than", "also", "most", "more"}
        clean_topic = {t.lower() for t in topic_terms if len(t) > 2} - _stop
        filtered = []
        for r in results:
            title_words = set(r.get("title", "").lower().split())
            title_words = {w.strip(".,;:()[]") for w in title_words if len(w) > 2} - _stop
            # Papers cited by 2+ surveys pass regardless (likely foundational)
            if r.get("cited_by_n_surveys", 0) >= 2 or (title_words & clean_topic):
                filtered.append(r)
        logger.info("Topic filter: kept %d / %d survey refs", len(filtered), len(results))
        results = filtered

    results.sort(
        key=lambda p: (p.get("cited_by_n_surveys", 0), p.get("citation_count", 0)),
        reverse=True,
    )
    return results


def search_for_claim_evidence(
    claim: str,
    evidence_role: str,
    limit: int = 8,
    year_from: int | None = None,
    mailto: str | None = None,
) -> list[dict]:
    """Search for papers that serve a specific evidence role for a claim.

    Args:
        claim: Description of the claim (used as search query base).
        evidence_role: One of "supporting", "counter", "methodological", "foundational".
        limit: Max papers to return.
        year_from: Minimum publication year.
        mailto: Email for Crossref polite pool.

    For "counter" role, generates negation queries automatically.
    For "methodological", focuses on method/comparison terms.
    For "foundational", removes year filter and sorts by citation count.
    """
    queries: list[str] = []

    if evidence_role == "counter":
        queries.append(claim)
        words = claim.split()
        short = " ".join(words[:6]) if len(words) > 6 else claim
        queries.append(f"{short} limitations")
        queries.append(f"{short} criticism")
        queries.append(f"{short} no effect")
    elif evidence_role == "methodological":
        queries.append(f"{claim} methodology")
        queries.append(f"{claim} comparison method")
    elif evidence_role == "foundational":
        queries.append(claim)
        year_from = None  # Foundational works can be old
    else:  # "supporting" or anything else
        queries.append(claim)

    results: list[dict] = []
    seen: set[str] = set()

    for q in queries[:3]:
        try:
            hits = search_papers(q, limit=limit, year_from=year_from, mailto=mailto)
            for h in hits:
                key = h.get("title", "").lower()[:60]
                if key and key not in seen:
                    seen.add(key)
                    h["evidence_role"] = evidence_role
                    h["target_claim"] = claim
                    results.append(h)
        except Exception as e:
            logger.warning("Claim search failed for '%s' (%s): %s",
                           q[:50], evidence_role, e)
        time.sleep(0.5)

    if evidence_role == "foundational":
        results.sort(key=lambda p: p.get("citation_count", 0), reverse=True)

    return results[:limit]


def expand_citation_graph(
    papers: list[dict],
    direction: str = "both",
    limit_per_paper: int = 15,
    topic_terms: set[str] | None = None,
) -> list[dict]:
    """Follow citation graph from a set of papers.

    Args:
        papers: Papers to expand from (need ``paper_id_s2`` field).
        direction: "backward" (who they cite), "forward" (who cites them), or "both".
        limit_per_paper: Max refs/citations to fetch per paper.
        topic_terms: If provided, discard discovered papers with zero title overlap.

    Returns deduplicated union of all discovered papers.
    """
    _stop = {"the", "and", "for", "with", "from", "that", "this", "are",
             "was", "were", "has", "have", "been", "its", "not", "but",
             "can", "all", "may", "will", "one", "two", "new", "use",
             "via", "how", "does", "into", "than", "also", "most", "more"}
    clean_topic = ({t.lower() for t in topic_terms if len(t) > 2} - _stop) if topic_terms else None

    results: list[dict] = []
    seen: set[str] = set()

    for p in papers:
        key = p.get("title", "").lower()[:60]
        if key:
            seen.add(key)

    def _dedup(hits: list[dict]) -> None:
        for h in hits:
            key = h.get("title", "").lower()[:60]
            if key and key not in seen:
                seen.add(key)
                # Topic filter: skip papers with zero title overlap
                if clean_topic:
                    title_words = set(h.get("title", "").lower().split())
                    title_words = {w.strip(".,;:()[]") for w in title_words if len(w) > 2} - _stop
                    if not (title_words & clean_topic):
                        continue
                results.append(h)

    for paper in papers:
        s2_id = paper.get("paper_id_s2", "")
        if not s2_id:
            continue

        if direction in ("backward", "both"):
            try:
                refs = fetch_paper_references(s2_id, limit=limit_per_paper) or []
                _dedup(refs)
            except Exception as e:
                logger.warning("Citation graph backward failed for %s: %s", s2_id, e)

        if direction in ("forward", "both"):
            try:
                cites = fetch_paper_citations(s2_id, limit=limit_per_paper) or []
                _dedup(cites)
            except Exception as e:
                logger.warning("Citation graph forward failed for %s: %s", s2_id, e)

    if clean_topic:
        logger.info("Citation graph topic filter: kept %d papers", len(results))
    return results


def audit_evidence_gaps(
    argument_claims: list[dict],
    curated_papers: list[dict],
) -> list[dict]:
    """Check which claims lack evidence for specific roles.

    Args:
        argument_claims: List of dicts with "claim" and "evidence_needed" keys.
        curated_papers: Papers with optional "evidence_role" and "target_claim" fields.

    Returns list of gap dicts: {"claim", "missing_role", "search_hint"}.
    """
    gaps: list[dict] = []

    for ac in argument_claims:
        claim_text = ac.get("claim", "")
        evidence_needed = ac.get("evidence_needed", {})

        for role, description in evidence_needed.items():
            filled = any(
                p.get("evidence_role") == role and p.get("target_claim") == claim_text
                for p in curated_papers
            )
            if not filled:
                gaps.append({
                    "claim": claim_text,
                    "missing_role": role,
                    "search_hint": description,
                })

    return gaps


def search_for_gaps(
    gaps: list[dict],
    limit_per_gap: int = 5,
    year_from: int | None = 2016,
    mailto: str | None = None,
) -> list[dict]:
    """Search for papers to fill identified evidence gaps.

    Args:
        gaps: Output from ``audit_evidence_gaps()``.
        limit_per_gap: Max papers per gap.

    Returns papers with evidence_role and target_claim set.
    """
    results: list[dict] = []
    seen: set[str] = set()

    for gap in gaps[:6]:  # Cap at 6 gaps
        hint = gap.get("search_hint", gap.get("claim", ""))
        role = gap.get("missing_role", "supporting")
        claim = gap.get("claim", "")

        try:
            hits = search_for_claim_evidence(
                hint, evidence_role=role, limit=limit_per_gap,
                year_from=year_from, mailto=mailto,
            )
            for h in hits:
                key = h.get("title", "").lower()[:60]
                if key and key not in seen:
                    seen.add(key)
                    h["target_claim"] = claim
                    results.append(h)
        except Exception as e:
            logger.warning("Gap search failed for '%s': %s", hint[:50], e)

    return results
