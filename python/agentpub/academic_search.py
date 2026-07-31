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

AI-powered semantic search (require API keys, best relevance):
  12. Consensus (CONSENSUS_API_KEY) — semantic search over 200M+ papers, $0.10/call
  13. Elicit (ELICIT_API_KEY) — AI research assistant, 138M+ papers, Pro plan required
  14. Scite.ai (SCITE_API_KEY) — smart citations with 1.2B+ citation statements

Results are returned in a format the PlaybookResearcher can use directly.
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

from agentpub.paper_cache import cache_papers as _cache_papers, search_cached as _search_cached, get_by_doi as _cache_get_doi

logger = logging.getLogger("agentpub.academic_search")

# Crossref (most open, polite pool with mailto)
_CROSSREF_BASE = "https://api.crossref.org"
_CROSSREF_MAILTO = "api@agentpub.org"  # default, overridden by user email

# arXiv (completely open, 3-second delay rule)
_ARXIV_BASE = "https://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Semantic Scholar (free but aggressive rate limits — 100 req/5min unauth, 1 req/s with key)
_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_REC_BASE = "https://api.semanticscholar.org/recommendations/v1"
_S2_FIELDS = "paperId,title,abstract,year,authors,citationCount,url,externalIds,venue"
_S2_MIN_INTERVAL = 2.0  # seconds between S2 calls (their limit is 1/s, we use 2 for safety)
_s2_last_call: float = 0.0

# ---------------------------------------------------------------------------
# Generic per-provider throttle (prevents hammering any single API)
# ---------------------------------------------------------------------------
_provider_last_call: dict[str, float] = {}
_provider_429_count: dict[str, int] = {}   # consecutive 429s per provider
_provider_disabled: set[str] = set()       # providers disabled for this run
_PROVIDER_429_LIMIT = 3                     # skip provider after this many consecutive 429s

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
    "consensus": 1.0,    # paid API ($0.10/call), light throttle
    "elicit": 1.0,       # Pro plan, 100 req/day
    "scite": 1.0,        # paid API, light throttle
    "biorxiv": 1.0,      # uses Crossref with prefix filter
    "plos": 1.0,         # PLOS Search API
    "springer": 1.0,     # Springer Nature Open Access API
    "hal": 1.0,          # HAL open archive
    "zenodo": 1.0,       # Zenodo open repository
    "nasa_ads": 1.0,     # NASA ADS
    "doaj": 1.0,         # Directory of Open Access Journals
    "dblp": 1.0,         # DBLP computer science bibliography
    "internet_archive": 2.0,  # Internet Archive Scholar
    "openaire": 1.0,         # OpenAIRE European research
    "fatcat": 1.0,           # Fatcat (Internet Archive catalog)
    "opencitations": 1.0,    # OpenCitations citation graph
    "datacite": 1.0,         # DataCite DOI registry
    "dimensions": 2.0,       # Dimensions (freemium)
    "inspire_hep": 1.0,      # INSPIRE-HEP physics
    "eric": 1.0,             # ERIC education research
    "figshare": 1.0,         # Figshare research data
    "scielo": 1.0,           # SciELO Latin American OA
    "base": 1.0,             # BASE search engine
    "ieee": 1.0,             # IEEE Xplore
    "philpapers": 1.0,       # PhilPapers philosophy
    "cinii": 1.0,            # CiNii Japanese research
    "sciencedirect": 1.0,    # Elsevier ScienceDirect
    "wos": 1.0,              # Web of Science
    "google_books": 1.0,     # Google Books
    "open_library": 1.0,     # Open Library
}


def search_semantic_scholar(query: str, limit: int = 10) -> list[dict]:
    """Quick Semantic Scholar search for novelty checking."""
    import urllib.request, urllib.parse, json as _json
    encoded = urllib.parse.quote(query)
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={encoded}&limit={limit}&fields=title,year,citationCount,abstract,authors"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgentPub/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
            return data.get("data", [])
    except Exception:
        return []


def _throttle(provider: str) -> None:
    """Wait if needed to respect per-provider rate limits."""
    interval = _PROVIDER_MIN_INTERVALS.get(provider, 1.0)
    last = _provider_last_call.get(provider, 0.0)
    elapsed = time.time() - last
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _provider_last_call[provider] = time.time()


def _is_provider_disabled(provider: str) -> bool:
    """Check if a provider has been circuit-broken due to repeated failures."""
    return provider in _provider_disabled


def _record_429(provider: str) -> bool:
    """Record a 429 for a provider. Returns True if provider is now disabled."""
    _provider_429_count[provider] = _provider_429_count.get(provider, 0) + 1
    if _provider_429_count[provider] >= _PROVIDER_429_LIMIT:
        _provider_disabled.add(provider)
        logger.warning("%s: %d consecutive 429s — disabling for this run",
                       provider, _provider_429_count[provider])
        return True
    return False


# Per-provider failure tracking for circuit breaker
_provider_fail_count: dict[str, int] = {}

# Per-provider zero-result tracking: skip sources that never return results
# Key = "source:query_prefix", value = consecutive zero-result count
_provider_zero_count: dict[str, int] = {}
_PROVIDER_ZERO_LIMIT = 3  # After 3 consecutive zero-result queries, skip source for this session   # consecutive failures (any type) per provider
_PROVIDER_FAIL_LIMIT = 3                      # disable after this many consecutive non-429 failures


def _record_failure(provider: str) -> bool:
    """Record a generic failure. Returns True if provider is now disabled."""
    _provider_fail_count[provider] = _provider_fail_count.get(provider, 0) + 1
    if _provider_fail_count[provider] >= _PROVIDER_FAIL_LIMIT:
        _provider_disabled.add(provider)
        logger.warning("%s: %d consecutive failures — disabling for this run",
                       provider, _provider_fail_count[provider])
        return True
    return False


def _record_success(provider: str) -> None:
    """Reset consecutive failure counters on success."""
    _provider_429_count[provider] = 0
    _provider_fail_count[provider] = 0


def _record_zero_results(provider: str) -> bool:
    """Track sources returning zero results. Returns True if source should be skipped."""
    _provider_zero_count[provider] = _provider_zero_count.get(provider, 0) + 1
    if _provider_zero_count[provider] >= _PROVIDER_ZERO_LIMIT:
        logger.info("%s: %d consecutive zero-result queries — skipping for remaining queries",
                    provider, _provider_zero_count[provider])
        return True
    return False


def _record_nonzero_results(provider: str) -> None:
    """Reset zero-result counter when source returns results."""
    _provider_zero_count[provider] = 0


def _is_source_exhausted(provider: str) -> bool:
    """Check if a source has been returning zero results consistently."""
    return _provider_zero_count.get(provider, 0) >= _PROVIDER_ZERO_LIMIT


def _s2_headers() -> dict:
    """Return Semantic Scholar API headers, including API key if available."""
    key = os.environ.get("S2_API_KEY", "")
    if key:
        return {"x-api-key": key}
    return {}


def _s2_throttle() -> None:
    """Wait if needed to respect Semantic Scholar rate limits.

    With API key: 1 req/s allowed, throttle at 1.0s.
    Without key: much stricter limits, throttle at 3.0s.
    """
    has_key = bool(os.environ.get("S2_API_KEY", ""))
    if has_key:
        _PROVIDER_MIN_INTERVALS["s2"] = 1.0
    else:
        _PROVIDER_MIN_INTERVALS["s2"] = 3.0
    _throttle("s2")



# NOTE: _STOPWORDS, _GENERIC_WORDS, _clean_words, _extract_bigrams, and
# _filter_by_topic_relevance have been removed. All relevance filtering is
# now handled by the LLM scoring pass in the pipeline, which understands
# semantic relevance (e.g., "metastasis" ≈ "metastatic", "tumor invasion"
# is relevant to "cancer").

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
            if not h.get("year"):
                continue  # Skip papers with no publication year
            key = h["title"].lower()[:50]
            if key not in seen_titles and h["title"]:
                seen_titles.add(key)
                results.append(h)

    # Check local cache first — avoids 3rd-party API calls for repeated queries
    try:
        cached = _search_cached(query, limit=limit)
        if cached:
            # Filter by year_from if specified
            if year_from:
                cached = [c for c in cached if (c.get("year") or 0) >= year_from]
            _dedup_add(cached)
            if len(results) >= limit:
                logger.info("Cache hit: %d results for '%s'", len(results), query[:40])
                return results[:limit]
            elif results:
                logger.info("Partial cache: %d results, fetching more", len(results))
    except Exception:
        pass  # Cache failure is non-fatal

    # 1. Crossref (most reliable, no key needed)
    try:
        crossref_results = _search_crossref(query, limit=limit, year_from=year_from, mailto=mailto)
        _dedup_add(crossref_results)
        logger.info("Crossref: %d results", len(crossref_results))
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

    # 4. OpenAlex (broadest coverage, free, no key required)
    if len(results) < limit:
        try:
            oa_results = _search_openalex(query, limit=limit - len(results))
            _dedup_add(oa_results)
            if oa_results:
                logger.info("OpenAlex: %d results", len(oa_results))
        except Exception as e:
            logger.warning("OpenAlex search failed: %s", e)

    # 5. PubMed (biomedical — free, key optional for higher limits)
    if len(results) < limit:
        try:
            pm_results = _search_pubmed(query, limit=limit - len(results))
            _dedup_add(pm_results)
            if pm_results:
                logger.info("PubMed: %d results", len(pm_results))
        except Exception as e:
            logger.warning("PubMed search failed: %s", e)

    # 6. Europe PMC (free, no key needed)
    if len(results) < limit:
        try:
            epmc_results = _search_europe_pmc(query, limit=limit - len(results))
            _dedup_add(epmc_results)
            if epmc_results:
                logger.info("Europe PMC: %d results", len(epmc_results))
        except Exception as e:
            logger.warning("Europe PMC search failed: %s", e)

    # 7. CORE (requires key)
    if len(results) < limit and os.environ.get("CORE_API_KEY"):
        try:
            core_results = _search_core(query, limit=limit - len(results))
            _dedup_add(core_results)
            if core_results:
                logger.info("CORE: %d results", len(core_results))
        except Exception as e:
            logger.warning("CORE search failed: %s", e)

    # 8. Lens.org (requires key)
    if len(results) < limit and os.environ.get("LENS_API_KEY"):
        try:
            lens_results = _search_lens(query, limit=limit - len(results))
            _dedup_add(lens_results)
            if lens_results:
                logger.info("Lens: %d results", len(lens_results))
        except Exception as e:
            logger.warning("Lens search failed: %s", e)

    # 9. Scopus (requires key)
    if len(results) < limit and os.environ.get("SCOPUS_API_KEY"):
        try:
            scopus_results = _search_scopus(query, limit=limit - len(results))
            _dedup_add(scopus_results)
            if scopus_results:
                logger.info("Scopus: %d results", len(scopus_results))
        except Exception as e:
            logger.warning("Scopus search failed: %s", e)

    # Store results in local cache for future runs
    _cache_papers(results)

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
            if not h.get("year"):
                continue  # Skip papers with no publication year
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
    # Crossref `query` does OR matching on all words and doesn't search abstracts.
    # For long queries (>8 words), extract the 5-6 most meaningful terms to avoid
    # matching on generic words like "critical", "analysis", "current" etc.
    query_words = query.split()
    if len(query_words) > 8:
        # Keep only domain-specific words (skip stopwords and short words)
        _stop = {"the","a","an","of","in","on","for","and","or","to","is","are","was",
                 "were","be","been","being","by","at","from","with","as","into","through",
                 "its","their","this","that","these","those","how","what","why","which",
                 "critical","current","novel","new","recent","review","analysis","study",
                 "approach","based","using","between","across","toward","towards","identifying"}
        key_terms = [w for w in query_words if w.lower().strip(".:,;!?()") not in _stop and len(w) > 2]
        search_query = " ".join(key_terms[:6])
    else:
        search_query = query

    params: dict = {
        "query": search_query,
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
            "abstract": abstract[:2000],
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


def verify_paper_bibliographic(
    title: str,
    first_author: str | None = None,
    year: int | None = None,
    doi: str | None = None,
    mailto: str | None = None,
) -> dict | None:
    """Verify an LLM-suggested paper using Crossref query.bibliographic.

    query.bibliographic is designed for citation-style lookups — it matches
    against title + author + year simultaneously, which is much more precise
    than the general `query` parameter for finding a *specific known paper*.

    Returns a verified paper dict if found, or None.
    """
    # If we have a DOI, just look it up directly — fastest and most reliable
    if doi and doi.startswith("10."):
        result = _crossref_by_doi(doi)
        if result:
            return result

    # Build bibliographic query string: "title, first_author, year"
    bib_parts = [title]
    if first_author:
        bib_parts.append(first_author)
    if year:
        bib_parts.append(str(year))
    bib_query = ", ".join(bib_parts)

    params: dict = {
        "query.bibliographic": bib_query,
        "rows": 5,
        "sort": "relevance",
        "order": "desc",
        "mailto": mailto or _CROSSREF_MAILTO,
        "select": "DOI,title,author,published-print,published-online,abstract,is-referenced-by-count,URL,container-title,type",
        "filter": "type:journal-article,type:proceedings-article,type:book-chapter,type:posted-content",
    }

    _throttle("crossref")
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_CROSSREF_BASE}/works", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None

    # Stopwords to exclude from title matching
    _match_stop = {"the","a","an","of","in","on","for","and","or","to","is","are","was",
                   "were","by","at","from","with","as","its","how","what","why","new"}

    # Check top results for a title match
    for item in data.get("message", {}).get("items", []):
        cr_title_list = item.get("title", [])
        cr_title = cr_title_list[0] if cr_title_list else ""
        if not cr_title:
            continue

        # Fuzzy title match — normalize, remove stopwords, then compare
        norm_suggested = [w for w in re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
                         if w not in _match_stop and len(w) > 2]
        norm_found = [w for w in re.sub(r"[^a-z0-9 ]", "", cr_title.lower()).split()
                     if w not in _match_stop and len(w) > 2]
        if not norm_suggested or not norm_found:
            continue

        # Bidirectional overlap — both directions must be high
        common = set(norm_suggested) & set(norm_found)
        overlap_fwd = len(common) / max(len(set(norm_suggested)), 1)  # how much of suggested is in found
        overlap_rev = len(common) / max(len(set(norm_found)), 1)      # how much of found is in suggested
        # Require ≥70% overlap in BOTH directions (prevents partial matches)
        if overlap_fwd < 0.7 or overlap_rev < 0.5:
            continue

        # Found a match — build result
        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                authors.append(f"{given} {family}".strip() if given else family)

        found_year = None
        for date_field in ("published-print", "published-online"):
            date_parts = item.get(date_field, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                found_year = date_parts[0][0]
                break

        abstract = item.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        found_doi = item.get("DOI", "")
        url = f"https://doi.org/{found_doi}" if found_doi else item.get("URL", "")
        container = item.get("container-title", [])
        venue = container[0] if container else ""

        result = {
            "title": cr_title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": found_year,
            "citation_count": item.get("is-referenced-by-count", 0),
            "url": url,
            "doi": found_doi,
            "source": "crossref",
            "llm_verified": True,
        }
        if venue:
            result["venue"] = venue
        return result

    # Crossref didn't find it — try OpenAlex as fallback
    try:
        oa_hits = _search_openalex(title, limit=3, mailto=mailto)
        for h in oa_hits:
            oa_title = h.get("title", "")
            norm_sug = [w for w in re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
                       if w not in _match_stop and len(w) > 2]
            norm_oa = [w for w in re.sub(r"[^a-z0-9 ]", "", oa_title.lower()).split()
                      if w not in _match_stop and len(w) > 2]
            if norm_sug and norm_oa:
                common = set(norm_sug) & set(norm_oa)
                fwd = len(common) / max(len(set(norm_sug)), 1)
                rev = len(common) / max(len(set(norm_oa)), 1)
                if fwd >= 0.7 and rev >= 0.5:
                    h["llm_verified"] = True
                    h["source"] = "openalex"
                    return h
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# arXiv API
# ---------------------------------------------------------------------------

def _search_arxiv(query: str, limit: int = 10) -> list[dict]:
    """Search arXiv API (completely open, no key).

    Must respect 3-second delay between requests.
    Returns preprints in physics, math, CS, etc.
    """
    # arXiv API fails on long queries and special characters — simplify to key terms
    arxiv_query = _simplify_query(query, max_words=6)
    # arXiv query syntax chokes on parentheses, brackets, and other special chars
    arxiv_query = re.sub(r"[()[\]{}<>\"'`~!@#$%^&*=+|\\]", " ", arxiv_query)
    arxiv_query = re.sub(r"\s+", " ", arxiv_query).strip()

    params = {
        "search_query": f"all:{arxiv_query}",
        "start": 0,
        "max_results": min(limit, 50),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }

    if _is_provider_disabled("arxiv"):
        return []

    _throttle("arxiv")
    with httpx.Client(timeout=20) as client:
        resp = client.get(_ARXIV_BASE, params=params)
        if resp.status_code == 429:
            if _record_429("arxiv"):
                return []  # circuit breaker tripped
            wait = 5 * _provider_429_count["arxiv"]
            logger.info("arXiv rate-limited (429 #%d/%d), waiting %ds...",
                        _provider_429_count["arxiv"], _PROVIDER_429_LIMIT, wait)
            time.sleep(wait)
            _provider_last_call["arxiv"] = time.time()
            resp = client.get(_ARXIV_BASE, params=params)
            if resp.status_code == 429:
                return []
        else:
            _record_success("arxiv")
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    results = []

    # Build query term set for basic relevance check
    _query_terms = {w.lower() for w in arxiv_query.split() if len(w) > 3}

    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
        if not title:
            continue

        # Category filter — skip papers from obviously off-topic arXiv categories
        # (math, physics, astrophysics, etc.) that match on abbreviation collisions
        primary_cat_el = entry.find("arxiv:primary_category", _ARXIV_NS)
        if primary_cat_el is not None:
            cat = (primary_cat_el.get("term") or "").lower()
            _OFF_TOPIC_PREFIXES = [
                "math.", "hep-", "gr-qc", "astro-ph", "nucl-", "cond-mat",
                "nlin.", "quant-ph", "math-ph",
            ]
            # Don't filter categories that match the query domain
            _q_lower = arxiv_query.lower()
            _MATH_TERMS = {"math", "algebra", "topology", "conjecture", "theorem", "manifold",
                          "cohomology", "homology", "geometric", "polynomial", "eigenvalue",
                          "hodge", "riemann", "hilbert", "galois", "abelian", "moduli"}
            _PHYSICS_TERMS = {"quantum", "quark", "boson", "fermion", "higgs", "particle",
                             "hadron", "neutrino", "gravitational", "relativity", "cosmolog",
                             "astrophys", "nuclear", "plasma", "condensed matter"}
            if any(t in _q_lower for t in _MATH_TERMS):
                _OFF_TOPIC_PREFIXES = [p for p in _OFF_TOPIC_PREFIXES if not p.startswith("math")]
            if any(t in _q_lower for t in _PHYSICS_TERMS):
                _OFF_TOPIC_PREFIXES = [p for p in _OFF_TOPIC_PREFIXES
                                       if p not in ("hep-", "gr-qc", "astro-ph", "nucl-",
                                                    "cond-mat", "quant-ph", "math-ph")]
            if any(cat.startswith(p) for p in _OFF_TOPIC_PREFIXES):
                logger.debug("arXiv skip off-topic [%s]: %s", cat, title[:60])
                continue

        # Basic title relevance — require at least 1 query term in title or abstract
        _title_words = {w.lower().strip(".,;:()") for w in title.split()}
        summary_el_check = entry.find("atom:summary", _ARXIV_NS)
        _abs_text = (summary_el_check.text or "") if summary_el_check is not None else ""
        _abs_words = {w.lower().strip(".,;:()") for w in _abs_text.split()[:100]}
        if _query_terms and not (_query_terms & (_title_words | _abs_words)):
            logger.debug("arXiv skip no query overlap: %s", title[:60])
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
            "venue": "arXiv",
        })

    return results


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

def _simplify_query(query: str, max_words: int = 8) -> str:
    """Simplify a long query into key terms for APIs that choke on long input.

    Removes common filler words and returns the most meaningful terms.
    """
    _QUERY_STOP = {
        "a", "an", "the", "of", "in", "on", "for", "and", "or", "to", "with",
        "by", "from", "is", "are", "was", "were", "be", "been", "being", "how",
        "why", "what", "which", "that", "this", "these", "those", "its", "their",
        "our", "between", "across", "through", "into", "about", "toward", "towards",
        "identifying", "mapping", "understanding", "exploring", "examining",
        "investigating", "reconciling", "bridging", "critical", "review",
        "comprehensive", "systematic", "narrative", "analysis", "study",
        "whether", "does", "can", "evidence", "new", "novel", "current",
    }
    words = query.split()
    if len(words) <= max_words:
        return query
    key_words = [w for w in words if w.lower().strip(",:;()") not in _QUERY_STOP and len(w) > 2]
    return " ".join(key_words[:max_words]) if key_words else " ".join(words[:max_words])


def _search_semantic_scholar(
    query: str, limit: int = 10, year_from: int | None = None
) -> list[dict]:
    """Search Semantic Scholar API (free, no key required).

    Retries on 429 rate-limit with exponential backoff.
    """
    # S2 returns 0 results on very long queries — simplify to key terms
    s2_query = _simplify_query(query, max_words=10)
    params: dict = {
        "query": s2_query,
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
# Semantic Scholar Recommendations — SPECTER2 embedding-based similarity
# ---------------------------------------------------------------------------


def s2_recommend_from_paper(paper_id_s2: str, *, limit: int = 100, pool: str = "recent") -> list[dict]:
    """Get semantically similar papers using S2 Recommendations API (SPECTER2 embeddings).

    Args:
        paper_id_s2: Semantic Scholar paper ID.
        limit: Max results (up to 500).
        pool: "recent" (last 5 years) or "all-cs" (all CS papers). Default "recent".

    Returns list of normalized paper dicts, sorted by embedding similarity.
    Free, no API key required (shares S2 rate limits).
    """
    params: dict = {
        "limit": min(limit, 500),
        "fields": _S2_FIELDS,
        "from": pool,
    }
    for attempt in range(3):
        _s2_throttle()
        try:
            with httpx.Client(timeout=20, headers=_s2_headers()) as client:
                resp = client.get(
                    f"{_S2_REC_BASE}/papers/forpaper/{paper_id_s2}",
                    params=params,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                data = resp.json()
                break
        except httpx.HTTPError as e:
            logger.warning("S2 recommendations failed (attempt %d): %s", attempt, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return []
    else:
        return []

    results = []
    for item in data.get("recommendedPapers", []):
        if item.get("title"):
            results.append(_normalize_s2_result(item))
    return results


def s2_recommend_from_list(
    positive_ids: list[str],
    negative_ids: list[str] | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    """Get recommendations from a list of positive (and optional negative) seed papers.

    This is the most powerful S2 recommendation endpoint — give it 3-5 seed papers
    that are on-topic, and it returns up to 500 semantically similar papers using
    SPECTER2 embeddings. Much better than keyword search for topic discovery.

    Args:
        positive_ids: List of S2 paper IDs to find similar papers for.
        negative_ids: Optional list of S2 paper IDs to push away from.
        limit: Max results (up to 500).

    Free, no API key required.
    """
    body: dict = {"positivePaperIds": positive_ids}
    if negative_ids:
        body["negativePaperIds"] = negative_ids

    params: dict = {
        "limit": min(limit, 500),
        "fields": _S2_FIELDS,
    }

    for attempt in range(3):
        _s2_throttle()
        try:
            with httpx.Client(timeout=30, headers=_s2_headers()) as client:
                resp = client.post(
                    f"{_S2_REC_BASE}/papers/",
                    params=params,
                    json=body,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
        except httpx.HTTPError as e:
            logger.warning("S2 batch recommendations failed (attempt %d): %s", attempt, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return []
    else:
        return []

    results = []
    for item in data.get("recommendedPapers", []):
        if item.get("title"):
            results.append(_normalize_s2_result(item))
    return results


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
    """Score a paper for seed selection: blend citation count and recency.

    NOTE: No keyword overlap scoring — relevance is determined by the LLM
    scoring pass, not by word matching (which misses synonyms/related terms).
    """
    cite_score = min(paper.get("citation_count", 0) / 5000, 1.0)
    # Recency: papers from last 3 years get up to 0.5 bonus
    year = paper.get("year") or 2020
    current_year = time.localtime().tm_year
    age = max(0, current_year - year)
    recency = max(0.0, 1.0 - age / 10)
    return 0.5 * cite_score + 0.5 * recency


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

    # 1c. Europe PMC full text XML (European biomedical literature)
    if not full_text and doi:
        full_text = _fetch_europe_pmc_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "EuropePMC"

    # 1d. bioRxiv/medRxiv (preprints with DOI prefix 10.1101/)
    if not full_text and doi and doi.startswith("10.1101/"):
        full_text = _fetch_biorxiv_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "bioRxiv"

    # 1e. PLOS full text (DOIs containing 10.1371/journal.p)
    if not full_text and doi and "10.1371/journal.p" in doi:
        full_text = _fetch_plos_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "PLOS"

    # 1f. Springer Nature JATS (if SPRINGER_API_KEY set)
    if not full_text and doi and os.environ.get("SPRINGER_API_KEY"):
        full_text = _fetch_springer_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "Springer"

    # 1g. CORE full text (if CORE_API_KEY set)
    if not full_text and doi and os.environ.get("CORE_API_KEY"):
        full_text = _fetch_core_full_text(doi)
        if full_text and len(full_text) > 500:
            full_text_source = "CORE"

    # 1h. S2 open access PDF URL (any discipline — S2 tracks OA status)
    s2_id = paper.get("paper_id_s2", "")
    if not full_text and (doi or s2_id):
        try:
            lookup_key = f"DOI:{doi}" if doi else (paper.get("paper_id_s2", "") or None)
            if lookup_key:
                s2_details = _fetch_s2_enriched(lookup_key)
                if s2_details:
                    s2_oa_url = s2_details.get("open_access_pdf_url", "")
                    if s2_oa_url:
                        full_text = _fetch_html_text(s2_oa_url)
                        if full_text and len(full_text) > 500:
                            full_text_source = "S2-OA"
        except Exception:
            pass

    # 1i. Unpaywall → open access landing page (any discipline)
    if not full_text and doi:
        oa_url = _get_open_access_url(doi)
        if oa_url:
            full_text = _fetch_html_text(oa_url)
            if full_text and len(full_text) > 500:
                full_text_source = "OA"

    # 1j. HAL full text (if URL contains hal.science or archives-ouvertes)
    if not full_text and doi:
        hal_url = url or ""
        if "hal.science" in hal_url or "archives-ouvertes" in hal_url or not full_text:
            full_text = _fetch_hal_full_text(doi)
            if full_text and len(full_text) > 500:
                full_text_source = "HAL"

    # 1k. Zenodo full text (if URL contains zenodo.org)
    if not full_text and doi:
        zenodo_url = url or ""
        if "zenodo.org" in zenodo_url:
            full_text = _fetch_zenodo_full_text(doi)
            if full_text and len(full_text) > 500:
                full_text_source = "Zenodo"

    # 1l. Direct URL fetch as last resort (journal HTML pages)
    if not full_text and url and "arxiv.org" not in url:
        full_text = _fetch_html_text(url)
        if full_text and len(full_text) > 500:
            full_text_source = "URL"

    # 1m. Serper.dev Google search for author-posted PDFs
    # Authors often post PDFs on personal sites, ResearchGate, university repos
    if not full_text:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if serper_key and title and title != "Unknown":
            first_author = authors[0] if authors else ""
            full_text = _search_pdf_via_serper(title, serper_key, author=first_author, doi=doi)
            if full_text and len(full_text) > 500:
                full_text_source = "Serper-PDF"

    if full_text and len(full_text) > 500:
        # Pass max_chars through so caller can control content length
        section_limit = max_chars if max_chars else 0
        sections = _extract_key_sections(full_text, max_chars=section_limit)
        body = "\n\n".join(sections)
        result = header + "\n" + body
        if max_chars and len(result) > max_chars:
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


def _fetch_europe_pmc_full_text(doi: str) -> str | None:
    """Fetch full text from Europe PMC via JATS XML.

    Europe PMC provides free full text XML for open-access biomedical papers.
    First converts DOI to PMCID, then fetches JATS XML and extracts body text.
    """
    try:
        _throttle("europe_pmc")
        with httpx.Client(timeout=15) as client:
            # Step 1: Convert DOI to PMCID via Europe PMC search
            resp = client.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": f"DOI:{doi}", "resultType": "idlist", "format": "json"},
            )
            if resp.status_code != 200:
                return None
            _record_success("europe_pmc")

            results = resp.json().get("resultList", {}).get("result", [])
            if not results:
                return None
            pmcid = results[0].get("pmcid", "")
            if not pmcid:
                return None

            # Step 2: Fetch JATS XML full text
            _throttle("europe_pmc")
            xml_resp = client.get(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
            )
            if xml_resp.status_code != 200:
                return None
            _record_success("europe_pmc")

            # Parse JATS XML — extract text from <body> → <sec> → <p>
            root = ET.fromstring(xml_resp.text)
            body = root.find(".//body")
            if body is None:
                return None

            paragraphs = []
            for sec in body.iter("sec"):
                # Extract section title
                title_el = sec.find("title")
                if title_el is not None and title_el.text:
                    paragraphs.append(f"\n\n## {title_el.text.strip()}\n")
                for p in sec.findall("p"):
                    text = "".join(p.itertext()).strip()
                    if text:
                        paragraphs.append(text)

            full_text = "\n".join(paragraphs).strip()
            if len(full_text) > 500:
                logger.info("Fetched Europe PMC full text for %s: %d chars", doi, len(full_text))
                return full_text
    except Exception as e:
        logger.debug("Europe PMC full text fetch failed for %s: %s", doi, e)
    return None


def _fetch_biorxiv_full_text(doi: str) -> str | None:
    """Fetch full text from bioRxiv or medRxiv HTML pages.

    Works for DOIs starting with 10.1101/ (bioRxiv/medRxiv prefix).
    Fetches the HTML full-text page and extracts clean text.
    """
    if not doi or not doi.startswith("10.1101/"):
        return None

    urls = [
        f"https://www.biorxiv.org/content/{doi}v1.full",
        f"https://www.medrxiv.org/content/{doi}v1.full",
    ]

    for url in urls:
        try:
            _throttle("biorxiv")
            text = _fetch_html_text(url)
            if text and len(text) > 1000:
                logger.info("Fetched bioRxiv/medRxiv full text from %s: %d chars", url, len(text))
                _record_success("biorxiv")
                return text
        except Exception as e:
            logger.debug("bioRxiv/medRxiv fetch failed for %s: %s", url, e)
            continue
    return None


def _fetch_core_full_text(doi_or_title: str, core_id: str | None = None) -> str | None:
    """Fetch full text from CORE (open access aggregator).

    Requires CORE_API_KEY. CORE indexes 300M+ open access papers with full text.
    """
    api_key = os.environ.get("CORE_API_KEY", "")
    if not api_key:
        return None

    try:
        _throttle("core")
        headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=15) as client:
            if core_id:
                resp = client.get(
                    f"https://api.core.ac.uk/v3/works/{core_id}",
                    headers=headers,
                )
            else:
                resp = client.get(
                    "https://api.core.ac.uk/v3/search/works",
                    params={"q": f'doi:"{doi_or_title}"', "limit": 1},
                    headers=headers,
                )

            if resp.status_code == 429:
                _record_429("core")
                return None
            if resp.status_code != 200:
                return None
            _record_success("core")

            data = resp.json()
            # Direct lookup returns the work; search returns results array
            if core_id:
                full_text = data.get("fullText", "")
            else:
                results = data.get("results", [])
                if not results:
                    return None
                full_text = results[0].get("fullText", "")

            if full_text and len(full_text) > 500:
                logger.info("Fetched CORE full text for %s: %d chars", doi_or_title[:40], len(full_text))
                return full_text
    except Exception as e:
        logger.debug("CORE full text fetch failed for %s: %s", doi_or_title[:40], e)
    return None


def _fetch_plos_full_text(doi: str) -> str | None:
    """Fetch full text from PLOS (Public Library of Science).

    PLOS provides free full text for all its journals via the search API.
    Works for DOIs containing 10.1371/journal.p (PLOS ONE, PLOS Biology, etc.).
    """
    if not doi:
        return None

    try:
        _throttle("plos")
        params: dict = {
            "q": f'id:"{doi}"',
            "fl": "body",
            "wt": "json",
        }
        plos_key = os.environ.get("PLOS_API_KEY", "")
        if plos_key:
            params["api_key"] = plos_key

        with httpx.Client(timeout=15) as client:
            resp = client.get("https://api.plos.org/search", params=params)
            if resp.status_code == 429:
                _record_429("plos")
                return None
            if resp.status_code != 200:
                return None
            _record_success("plos")

            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            if not docs:
                return None

            body = docs[0].get("body", "")
            if not body or len(body) < 500:
                return None

            # Clean inline citation markers like [1], [2,3]
            body = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", body)
            body = re.sub(r"\s{2,}", " ", body).strip()

            logger.info("Fetched PLOS full text for %s: %d chars", doi, len(body))
            return body
    except Exception as e:
        logger.debug("PLOS full text fetch failed for %s: %s", doi, e)
    return None


def _fetch_springer_full_text(doi: str) -> str | None:
    """Fetch full text from Springer Nature Open Access JATS API.

    Requires SPRINGER_API_KEY. Covers Springer, Nature, BMC open access content.
    """
    api_key = os.environ.get("SPRINGER_API_KEY", "")
    if not api_key or not doi:
        return None

    try:
        _throttle("springer")
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.springernature.com/openaccess/jats",
                params={"q": f"doi:{doi}", "api_key": api_key},
            )
            if resp.status_code == 429:
                _record_429("springer")
                return None
            if resp.status_code != 200:
                return None
            _record_success("springer")

            # Parse JATS XML — extract text from <body> elements
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                return None

            body = root.find(".//{http://www.w3.org/1999/xhtml}body")
            if body is None:
                body = root.find(".//body")
            if body is None:
                return None

            paragraphs = []
            for elem in body.iter():
                if elem.tag in ("p", "title", "sec-title"):
                    text = "".join(elem.itertext()).strip()
                    if text:
                        paragraphs.append(text)

            full_text = "\n\n".join(paragraphs).strip()
            if len(full_text) > 500:
                logger.info("Fetched Springer full text for %s: %d chars", doi, len(full_text))
                return full_text
    except Exception as e:
        logger.debug("Springer full text fetch failed for %s: %s", doi, e)
    return None


def _fetch_hal_full_text(hal_id_or_doi: str) -> str | None:
    """Fetch full text from HAL (French open archive).

    HAL is a major European open access repository. Tries to find the
    paper by DOI and fetch text from the HAL landing page.
    """
    if not hal_id_or_doi:
        return None

    try:
        _throttle("hal")
        with httpx.Client(timeout=15) as client:
            # Try DOI lookup first
            resp = client.get(
                "https://api.archives-ouvertes.fr/search/",
                params={
                    "q": f'doiId_s:"{hal_id_or_doi}"',
                    "fl": "fileMain_s,halId_s,uri_s",
                    "wt": "json",
                },
            )
            if resp.status_code != 200:
                return None
            _record_success("hal")

            docs = resp.json().get("response", {}).get("docs", [])
            if not docs:
                return None

            doc = docs[0]
            hal_id = doc.get("halId_s", "")
            uri = doc.get("uri_s", "")

            # Try fetching from HAL landing page
            landing_url = uri if uri else (f"https://hal.science/{hal_id}" if hal_id else "")
            if not landing_url:
                return None

            text = _fetch_html_text(landing_url)
            if text and len(text) > 500:
                logger.info("Fetched HAL full text for %s: %d chars", hal_id_or_doi[:40], len(text))
                return text
    except Exception as e:
        logger.debug("HAL full text fetch failed for %s: %s", hal_id_or_doi[:40], e)
    return None


def _fetch_zenodo_full_text(doi: str) -> str | None:
    """Fetch full text from Zenodo (CERN open repository).

    Zenodo hosts research outputs across all disciplines. Checks for
    text/HTML files first, then tries the landing page.
    """
    if not doi:
        return None

    try:
        _throttle("zenodo")
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://zenodo.org/api/records",
                params={"q": f'doi:"{doi}"', "size": 1},
            )
            if resp.status_code == 429:
                _record_429("zenodo")
                return None
            if resp.status_code != 200:
                return None
            _record_success("zenodo")

            hits = resp.json().get("hits", {}).get("hits", [])
            if not hits:
                return None

            record = hits[0]
            files = record.get("files", [])

            # Look for text-friendly files first
            for f in files:
                fname = f.get("key", "").lower()
                if fname.endswith((".txt", ".html", ".htm")):
                    file_url = f.get("links", {}).get("self", "")
                    if file_url:
                        text = _fetch_html_text(file_url)
                        if text and len(text) > 500:
                            logger.info("Fetched Zenodo file text for %s: %d chars", doi, len(text))
                            return text

            # Fallback: try the Zenodo landing page
            record_url = record.get("links", {}).get("html", "")
            if record_url:
                text = _fetch_html_text(record_url)
                if text and len(text) > 500:
                    logger.info("Fetched Zenodo page text for %s: %d chars", doi, len(text))
                    return text
    except Exception as e:
        logger.debug("Zenodo full text fetch failed for %s: %s", doi, e)
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


def _extract_key_sections(full_text: str, max_chars: int = 0) -> list[str]:
    """Extract the most informative sections from a full paper.

    Prioritizes: Abstract > Introduction > Methodology/Methods >
    Discussion > Conclusion > Related Work.
    Skips raw results tables and appendices.

    max_chars=0 means no limit (return all content).
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

    # Take sections up to max_chars (0 = unlimited)
    result: list[str] = []
    total = 0
    per_section_cap = 2500 if max_chars else 0  # no per-section cap when unlimited
    for _score, heading, content in scored:
        section_content = content[:per_section_cap] if per_section_cap else content
        section_text = f"## {heading}\n{section_content}"
        if max_chars and total + len(section_text) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                result.append(section_text[:remaining] + "\n[... truncated]")
            break
        result.append(section_text)
        total += len(section_text)

    return result


def _search_pdf_via_serper(title: str, api_key: str, author: str = "", doi: str = "") -> str | None:
    """Search Google via Serper.dev for a PDF version of a paper.

    Authors often post PDFs on personal sites, ResearchGate, university
    repositories, or preprint servers. This searches for the exact title
    plus author name and DOI to find these copies.

    Returns extracted text from the PDF page (HTML landing page), or None.
    """
    # Clean title for search — remove special chars that break exact match
    clean_title = re.sub(r"[^\w\s\-]", "", title).strip()
    if len(clean_title) < 10:
        return None

    # Build query: "exact title" + author surname + DOI + filetype:pdf
    query_parts = [f'"{clean_title[:100]}"']
    if author:
        # Extract surname
        surname = author.split(",")[0].strip() if "," in author else author.split()[-1].strip()
        if len(surname) >= 2:
            query_parts.append(surname)
    if doi:
        query_parts.append(doi)
    query_parts.append("filetype:pdf")
    query = " ".join(query_parts)

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "q": query,
                    "num": 3,
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

        results = data.get("organic", [])
        if not results:
            return None

        # Build title keywords for verification (lowercase, 4+ chars, no stopwords)
        _stop = {"the", "and", "for", "that", "this", "with", "from", "which",
                 "have", "been", "were", "their", "than", "also", "into", "more"}
        title_keywords = {
            w.lower() for w in re.findall(r"[a-zA-Z]{4,}", clean_title)
            if w.lower() not in _stop
        }
        min_title_match = max(3, len(title_keywords) // 2)  # need at least half the title words

        # Try each result — verify it's the right paper before using
        for item in results[:5]:
            link = item.get("link", "")
            result_title = item.get("title", "")
            snippet = item.get("snippet", "")
            if not link:
                continue

            # Step 1: Quick title check from search result metadata
            # The search result title should overlap with our paper title
            result_words = {w.lower() for w in re.findall(r"[a-zA-Z]{4,}", result_title) if w.lower() not in _stop}
            title_overlap = len(title_keywords & result_words)
            if title_overlap < min(2, len(title_keywords)):
                logger.debug("Serper PDF: skipping '%s' — title mismatch (%d overlap)", result_title[:50], title_overlap)
                continue

            # Skip publisher paywalls — these won't give us full text
            paywall_domains = [
                "sciencedirect.com", "springer.com", "wiley.com",
                "tandfonline.com", "sagepub.com", "nature.com",
                "ieee.org", "acm.org", "jstor.org",
            ]
            if any(d in link for d in paywall_domains):
                continue

            # For PDF links, try the HTML version of the same page
            html_url = link
            if link.endswith(".pdf"):
                html_url = link.replace(".pdf", ".html")

            text = _fetch_html_text(html_url)
            if not text or len(text) < 1000:
                # Try original URL if HTML variant didn't work
                if html_url != link:
                    text = _fetch_html_text(link)

            if not text or len(text) < 1000:
                continue

            # Step 2: Verify the fetched content is actually the right paper
            # Check that enough title keywords appear in the first 3000 chars
            text_start = text[:3000].lower()
            found_keywords = sum(1 for kw in title_keywords if kw in text_start)
            if found_keywords < min_title_match:
                logger.debug(
                    "Serper PDF: content mismatch for '%s' — only %d/%d title keywords found",
                    link[:60], found_keywords, len(title_keywords),
                )
                continue

            # Also check author surname if available
            if author:
                surname = author.split(",")[0].strip() if "," in author else author.split()[-1].strip()
                if len(surname) >= 3 and surname.lower() not in text[:5000].lower():
                    logger.debug("Serper PDF: author '%s' not found in content from %s", surname, link[:60])
                    continue

            logger.info("Found verified full text via Serper PDF search: %s (%d chars, %d/%d title match)",
                        link[:60], len(text), found_keywords, len(title_keywords))
            return text

    except Exception as e:
        logger.debug("Serper PDF search failed for '%s': %s", title[:40], e)

    return None


def _fetch_s2_enriched(paper_id: str) -> dict | None:
    """Fetch enriched details from Semantic Scholar (abstract + tldr)."""
    try:
        _s2_throttle()
        with httpx.Client(timeout=10, headers=_s2_headers()) as client:
            resp = client.get(
                f"{_S2_BASE}/paper/{paper_id}",
                params={"fields": "abstract,tldr,openAccessPdf"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            result = {"abstract": data.get("abstract", "") or ""}
            tldr = data.get("tldr")
            if tldr and isinstance(tldr, dict):
                result["tldr"] = tldr.get("text", "")
            oa_pdf = data.get("openAccessPdf")
            if oa_pdf and isinstance(oa_pdf, dict):
                result["open_access_pdf_url"] = oa_pdf.get("url", "")
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
    # OpenAlex `search` supports phrase matching with quotes and boolean AND/OR.
    # For long queries (full titles), extract 5-6 key terms to avoid noise.
    query_words = query.split()
    if len(query_words) > 8:
        _stop = {"the","a","an","of","in","on","for","and","or","to","is","are","by",
                 "at","from","with","as","its","this","that","how","what","why","which",
                 "critical","current","novel","new","recent","review","analysis","study",
                 "approach","based","using","between","identifying","toward","towards"}
        key_terms = [w.strip(".:,;!?()") for w in query_words
                     if w.lower().strip(".:,;!?()") not in _stop and len(w) > 2]
        search_query = " ".join(key_terms[:6])
    else:
        search_query = query

    params: dict = {
        "search": search_query,
        "per_page": min(limit, 50),
        "sort": "relevance_score:desc",
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

        # Extract venue from primary_location or host_venue
        venue = ""
        primary_loc = item.get("primary_location") or {}
        source_info = primary_loc.get("source") or {}
        venue = source_info.get("display_name", "")
        if not venue:
            host = item.get("host_venue") or {}
            venue = host.get("display_name", "")

        result = {
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "openalex",
        }
        if venue:
            result["venue"] = venue
        results.append(result)

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
    # PubMed handles long queries better but simplify very long ones
    pubmed_query = _simplify_query(query, max_words=12) if len(query.split()) > 12 else query
    search_params = {
        **base_params,
        "db": "pubmed",
        "term": pubmed_query,
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

        # Journal/venue
        journal_el = article.find(".//Journal/Title")
        venue = journal_el.text.strip() if journal_el is not None and journal_el.text else ""
        if not venue:
            iso_el = article.find(".//Journal/ISOAbbreviation")
            venue = iso_el.text.strip() if iso_el is not None and iso_el.text else ""

        result = {
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": 0,  # PubMed doesn't provide citation counts
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "pubmed",
        }
        if venue:
            result["venue"] = venue
        results.append(result)

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

        # Venue/journal
        venue = item.get("journalTitle", "") or ""
        if not venue:
            journal_info = item.get("journal")
            if isinstance(journal_info, dict):
                venue = journal_info.get("title", "") or ""

        result = {
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": citation_count,
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "europe_pmc",
        }
        if venue:
            result["venue"] = venue
        results.append(result)

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
# Consensus — AI semantic search over 200M+ papers (CONSENSUS_API_KEY)
# ---------------------------------------------------------------------------

def _search_consensus(
    query: str, limit: int = 20, year_from: int | None = None,
    exclude_preprints: bool = False,
) -> list[dict]:
    """Search Consensus.app semantic academic search API.

    Docs: https://docs.consensus.app/reference/v1_quick_search
    Requires CONSENSUS_API_KEY env var. $0.10 per call.
    """
    api_key = os.environ.get("CONSENSUS_API_KEY", "")
    if not api_key:
        return []

    params: dict = {"query": query}
    if year_from:
        params["year_min"] = year_from
    if exclude_preprints:
        params["exclude_preprints"] = True

    _throttle("consensus")
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            "https://api.consensus.app/v1/quick_search",
            headers={"x-api-key": api_key},
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:limit]:
        title = item.get("title", "")
        if not title:
            continue
        results.append({
            "title": title,
            "abstract": (item.get("abstract") or "")[:500],
            "authors": item.get("authors", [])[:10],
            "year": item.get("publish_year"),
            "citation_count": item.get("citation_count", 0),
            "url": item.get("url", ""),
            "doi": item.get("doi", ""),
            "paper_id_s2": "",
            "source": "consensus",
            "venue": item.get("journal_name", ""),
        })

    return results


# ---------------------------------------------------------------------------
# Elicit — AI semantic search over 138M+ papers (ELICIT_API_KEY)
# ---------------------------------------------------------------------------

def _search_elicit(
    query: str, limit: int = 20, year_from: int | None = None,
) -> list[dict]:
    """Search Elicit.com semantic academic search API.

    Docs: https://docs.elicit.com/
    Requires ELICIT_API_KEY env var (Bearer token, elk_live_...).
    Pro plan: 100 papers/request, 100 requests/day.
    """
    api_key = os.environ.get("ELICIT_API_KEY", "")
    if not api_key:
        return []

    body: dict = {
        "query": query,
        "maxResults": min(limit, 100),
    }
    if year_from:
        body["filters"] = {"year": {"min": year_from}}

    _throttle("elicit")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://elicit.com/api/v1/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("papers", [])[:limit]:
        title = item.get("title", "")
        if not title:
            continue

        authors = item.get("authors", [])
        if authors and isinstance(authors[0], dict):
            authors = [a.get("name", "") for a in authors if a.get("name")]

        results.append({
            "title": title,
            "abstract": (item.get("abstract") or "")[:500],
            "authors": authors[:10],
            "year": item.get("year"),
            "citation_count": item.get("citationCount", 0) or item.get("citation_count", 0),
            "url": item.get("url", ""),
            "doi": item.get("doi", ""),
            "paper_id_s2": "",
            "source": "elicit",
            "venue": item.get("venue", "") or item.get("journal", ""),
        })

    return results


# ---------------------------------------------------------------------------
# Scite.ai — smart citation search with 1.2B+ citation statements (SCITE_API_KEY)
# ---------------------------------------------------------------------------

def _search_scite(
    query: str, limit: int = 20,
) -> list[dict]:
    """Search Scite.ai for papers with citation context.

    Docs: https://api.scite.ai/docs
    Requires SCITE_API_KEY env var.
    """
    api_key = os.environ.get("SCITE_API_KEY", "")
    if not api_key:
        return []

    params: dict = {
        "q": query,
        "limit": min(limit, 30),
    }

    _throttle("scite")
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            "https://api.scite.ai/search",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    hits = data.get("hits", data.get("results", []))
    if isinstance(hits, dict):
        hits = hits.get("hits", [])
    for item in hits[:limit]:
        # Scite may wrap in _source
        src = item.get("_source", item)
        title = src.get("title", "")
        if not title:
            continue

        authors = src.get("authors", [])
        if authors and isinstance(authors[0], dict):
            authors = [a.get("name", "") for a in authors if a.get("name")]

        results.append({
            "title": title,
            "abstract": (src.get("abstract") or "")[:500],
            "authors": authors[:10],
            "year": src.get("year"),
            "citation_count": src.get("citationCount", 0) or src.get("citation_count", 0),
            "url": f"https://doi.org/{src['doi']}" if src.get("doi") else src.get("url", ""),
            "doi": src.get("doi", ""),
            "paper_id_s2": "",
            "source": "scite",
            "venue": src.get("journal", "") or src.get("venue", ""),
        })

    return results


# ---------------------------------------------------------------------------
# bioRxiv search (via Crossref with prefix filter)
# ---------------------------------------------------------------------------

def _search_biorxiv(query: str, limit: int = 10) -> list[dict]:
    """Search bioRxiv/medRxiv preprints via Crossref with DOI prefix filter.

    bioRxiv has no native query search API, so we use Crossref filtered
    to the 10.1101/ prefix (bioRxiv/medRxiv DOIs).
    """
    if _is_provider_disabled("biorxiv"):
        return []

    try:
        _throttle("biorxiv")
        params = {
            "query": query,
            "filter": "prefix:10.1101",
            "rows": min(limit, 25),
            "sort": "relevance",
            "order": "desc",
            "mailto": _CROSSREF_MAILTO,
            "select": "DOI,title,author,published-online,abstract,is-referenced-by-count,URL",
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_CROSSREF_BASE}/works", params=params)
            if resp.status_code == 429:
                _record_429("biorxiv")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("biorxiv")

        results = []
        for item in data.get("message", {}).get("items", []):
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""
            if not title:
                continue

            authors = []
            for a in item.get("author", []):
                given = a.get("given", "")
                family = a.get("family", "")
                if family:
                    name = f"{given} {family}".strip() if given else family
                    authors.append(name)

            year = None
            for date_field in ("published-online", "published-print"):
                date_parts = item.get(date_field, {}).get("date-parts", [[]])
                if date_parts and date_parts[0]:
                    year = date_parts[0][0]
                    break

            abstract = item.get("abstract", "")
            if abstract:
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()

            doi = item.get("DOI", "")
            url = f"https://doi.org/{doi}" if doi else item.get("URL", "")

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": item.get("is-referenced-by-count", 0) or 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "biorxiv",
            })

        return results
    except Exception as e:
        logger.warning("bioRxiv search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# PLOS search (requires PLOS_API_KEY)
# ---------------------------------------------------------------------------

def _search_plos(query: str, limit: int = 10) -> list[dict]:
    """Search PLOS (Public Library of Science) journals.

    Requires PLOS_API_KEY env var. Covers PLOS ONE, PLOS Biology, etc.
    """
    if _is_provider_disabled("plos"):
        return []

    plos_key = os.environ.get("PLOS_API_KEY", "")
    if not plos_key:
        return []

    try:
        _throttle("plos")
        params = {
            "q": query,
            "fl": "id,title_display,abstract,author_display,publication_date,counter_total_all",
            "rows": min(limit, 25),
            "wt": "json",
            "api_key": plos_key,
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get("https://api.plos.org/search", params=params)
            if resp.status_code == 429:
                _record_429("plos")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("plos")

        results = []
        for doc in data.get("response", {}).get("docs", []):
            title = doc.get("title_display", "")
            if not title:
                continue

            authors = doc.get("author_display", []) or []
            if isinstance(authors, str):
                authors = [a.strip() for a in authors.split(",")]

            year = None
            pub_date = doc.get("publication_date", "")
            if pub_date and len(pub_date) >= 4:
                try:
                    year = int(pub_date[:4])
                except (ValueError, TypeError):
                    pass

            abstract = doc.get("abstract", "") or ""
            if isinstance(abstract, list):
                abstract = abstract[0] if abstract else ""

            doi = doc.get("id", "")
            url = f"https://doi.org/{doi}" if doi else ""

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": doc.get("counter_total_all", 0) or 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "plos",
            })

        return results
    except Exception as e:
        logger.warning("PLOS search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Springer Nature search (requires SPRINGER_API_KEY)
# ---------------------------------------------------------------------------

def _search_springer(query: str, limit: int = 10) -> list[dict]:
    """Search Springer Nature Open Access API.

    Requires SPRINGER_API_KEY env var. Covers Springer, Nature, BMC open access.
    """
    if _is_provider_disabled("springer"):
        return []

    springer_key = os.environ.get("SPRINGER_API_KEY", "")
    if not springer_key:
        return []

    try:
        _throttle("springer")
        params = {
            "q": query,
            "p": min(limit, 25),
            "api_key": springer_key,
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.springernature.com/openaccess/json",
                params=params,
            )
            if resp.status_code == 429:
                _record_429("springer")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("springer")

        results = []
        for rec in data.get("records", []):
            title = rec.get("title", "")
            if not title:
                continue

            creators = rec.get("creators", [])
            authors = []
            for c in creators:
                name = c.get("creator", "")
                if name:
                    authors.append(name)

            year = None
            pub_date = rec.get("publicationDate", "") or rec.get("onlineDate", "")
            if pub_date and len(pub_date) >= 4:
                try:
                    year = int(pub_date[:4])
                except (ValueError, TypeError):
                    pass

            abstract = rec.get("abstract", "") or ""
            doi = rec.get("doi", "") or ""
            url_list = rec.get("url", [])
            url = ""
            if isinstance(url_list, list) and url_list:
                url = url_list[0].get("value", "") if isinstance(url_list[0], dict) else str(url_list[0])
            if not url and doi:
                url = f"https://doi.org/{doi}"

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "springer",
            })

        return results
    except Exception as e:
        logger.warning("Springer search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# HAL search (French open archive, no key needed)
# ---------------------------------------------------------------------------

def _search_hal(query: str, limit: int = 10) -> list[dict]:
    """Search HAL (Hyper Articles en Ligne) open archive.

    Free, no API key needed. Major European open access repository.
    """
    if _is_provider_disabled("hal"):
        return []

    try:
        _throttle("hal")
        params = {
            "q": query,
            "fl": "title_s,abstract_s,authFullName_s,producedDateY_i,doiId_s,uri_s,citationFull_s",
            "rows": min(limit, 25),
            "wt": "json",
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.archives-ouvertes.fr/search/",
                params=params,
            )
            if resp.status_code == 429:
                _record_429("hal")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("hal")

        results = []
        for doc in data.get("response", {}).get("docs", []):
            title_field = doc.get("title_s", "")
            # title_s can be a list or string
            title = title_field[0] if isinstance(title_field, list) and title_field else str(title_field or "")
            if not title:
                continue

            authors = doc.get("authFullName_s", []) or []
            if isinstance(authors, str):
                authors = [authors]

            year = doc.get("producedDateY_i")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None

            abstract_field = doc.get("abstract_s", "")
            abstract = abstract_field[0] if isinstance(abstract_field, list) and abstract_field else str(abstract_field or "")

            doi = doc.get("doiId_s", "") or ""
            uri = doc.get("uri_s", "") or ""
            url = f"https://doi.org/{doi}" if doi else uri

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "hal",
            })

        return results
    except Exception as e:
        logger.warning("HAL search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Zenodo search (CERN open repository, no key needed)
# ---------------------------------------------------------------------------

def _search_zenodo(query: str, limit: int = 10) -> list[dict]:
    """Search Zenodo open repository.

    Free, no API key needed. Hosts research outputs across all disciplines.
    """
    if _is_provider_disabled("zenodo"):
        return []

    try:
        _throttle("zenodo")
        params = {
            "q": query,
            "type": "publication",
            "size": min(limit, 25),
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get("https://zenodo.org/api/records", params=params)
            if resp.status_code == 429:
                _record_429("zenodo")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("zenodo")

        results = []
        for hit in data.get("hits", {}).get("hits", []):
            metadata = hit.get("metadata", {})
            title = metadata.get("title", "")
            if not title:
                continue

            creators = metadata.get("creators", [])
            authors = [c.get("name", "") for c in creators if c.get("name")]

            year = None
            pub_date = metadata.get("publication_date", "")
            if pub_date and len(pub_date) >= 4:
                try:
                    year = int(pub_date[:4])
                except (ValueError, TypeError):
                    pass

            abstract = metadata.get("description", "") or ""
            # Strip HTML from description
            if abstract:
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()

            doi = metadata.get("doi", "") or hit.get("doi", "") or ""
            url = f"https://doi.org/{doi}" if doi else hit.get("links", {}).get("html", "")

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "zenodo",
            })

        return results
    except Exception as e:
        logger.warning("Zenodo search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# NASA ADS search (requires ADS_API_KEY)
# ---------------------------------------------------------------------------

def _search_nasa_ads(query: str, limit: int = 10) -> list[dict]:
    """Search NASA Astrophysics Data System (ADS).

    Requires ADS_API_KEY env var. Best for astronomy, astrophysics, and physics.
    """
    if _is_provider_disabled("nasa_ads"):
        return []

    ads_key = os.environ.get("ADS_API_KEY", "")
    if not ads_key:
        return []

    try:
        _throttle("nasa_ads")
        params = {
            "q": query,
            "fl": "title,abstract,author,year,doi,bibcode,citation_count",
            "rows": min(limit, 25),
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.adsabs.harvard.edu/v1/search/query",
                params=params,
                headers={"Authorization": f"Bearer {ads_key}"},
            )
            if resp.status_code == 429:
                _record_429("nasa_ads")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("nasa_ads")

        results = []
        for doc in data.get("response", {}).get("docs", []):
            title_field = doc.get("title", [])
            title = title_field[0] if isinstance(title_field, list) and title_field else str(title_field or "")
            if not title:
                continue

            authors = doc.get("author", []) or []

            year = None
            year_str = doc.get("year", "")
            if year_str:
                try:
                    year = int(year_str)
                except (ValueError, TypeError):
                    pass

            abstract = doc.get("abstract", "") or ""
            doi_list = doc.get("doi", [])
            doi = doi_list[0] if isinstance(doi_list, list) and doi_list else str(doi_list or "")
            bibcode = doc.get("bibcode", "")
            url = f"https://doi.org/{doi}" if doi else (f"https://ui.adsabs.harvard.edu/abs/{bibcode}" if bibcode else "")

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": doc.get("citation_count", 0) or 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "nasa_ads",
            })

        return results
    except Exception as e:
        logger.warning("NASA ADS search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# DOAJ search (Directory of Open Access Journals, no key needed)
# ---------------------------------------------------------------------------

def _search_doaj(query: str, limit: int = 10) -> list[dict]:
    """Search DOAJ (Directory of Open Access Journals).

    Free, no API key needed. Indexes 20,000+ open access journals.
    """
    if _is_provider_disabled("doaj"):
        return []

    try:
        _throttle("doaj")
        with httpx.Client(timeout=25) as client:
            resp = client.get(
                f"https://doaj.org/api/search/articles/{query}",
                params={"page": 1, "pageSize": min(limit, 25)},
            )
            if resp.status_code == 429:
                _record_429("doaj")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("doaj")

        results = []
        for item in data.get("results", []):
            bib = item.get("bibjson", {})
            title = bib.get("title", "")
            if not title:
                continue

            author_list = bib.get("author", [])
            authors = [a.get("name", "") for a in author_list if a.get("name")]

            year = None
            year_str = bib.get("year", "")
            if year_str:
                try:
                    year = int(year_str)
                except (ValueError, TypeError):
                    pass

            abstract = bib.get("abstract", "") or ""

            # DOI from identifiers
            doi = ""
            for ident in bib.get("identifier", []):
                if ident.get("type") == "doi":
                    doi = ident.get("id", "")
                    break

            # URL from links or DOI
            url = ""
            for link in bib.get("link", []):
                if link.get("url"):
                    url = link["url"]
                    break
            if not url and doi:
                url = f"https://doi.org/{doi}"

            journal = bib.get("journal", {})
            venue = journal.get("title", "") if isinstance(journal, dict) else ""

            result = {
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "doaj",
            }
            if venue:
                result["venue"] = venue
            results.append(result)

        return results
    except Exception as e:
        logger.warning("DOAJ search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# DBLP search (computer science bibliography, no key needed)
# ---------------------------------------------------------------------------

def _search_dblp(query: str, limit: int = 10) -> list[dict]:
    """Search DBLP computer science bibliography.

    Free, no API key needed. Comprehensive CS publication index.
    """
    if _is_provider_disabled("dblp"):
        return []

    try:
        _throttle("dblp")
        params = {
            "q": query,
            "format": "json",
            "h": min(limit, 25),
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://dblp.org/search/publ/api",
                params=params,
            )
            if resp.status_code == 429:
                _record_429("dblp")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("dblp")

        results = []
        hits = data.get("result", {}).get("hits", {}).get("hit", [])
        for hit in hits:
            info = hit.get("info", {})
            title = info.get("title", "")
            if not title:
                continue
            # DBLP sometimes appends a period
            title = title.rstrip(".")

            # Authors can be a single dict or a list
            author_field = info.get("authors", {}).get("author", [])
            if isinstance(author_field, dict):
                author_field = [author_field]
            authors = []
            for a in author_field:
                name = a.get("text", "") if isinstance(a, dict) else str(a)
                if name:
                    authors.append(name)

            year = None
            year_str = info.get("year", "")
            if year_str:
                try:
                    year = int(year_str)
                except (ValueError, TypeError):
                    pass

            doi = info.get("doi", "") or ""
            url = info.get("ee", "") or info.get("url", "")
            if not url and doi:
                url = f"https://doi.org/{doi}"

            venue = info.get("venue", "") or ""

            result = {
                "title": title,
                "abstract": "",  # DBLP doesn't provide abstracts
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "dblp",
            }
            if venue:
                result["venue"] = venue
            results.append(result)

        return results
    except Exception as e:
        logger.warning("DBLP search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Internet Archive Scholar search (no key needed)
# ---------------------------------------------------------------------------

def _search_internet_archive(query: str, limit: int = 10) -> list[dict]:
    """Search Internet Archive Scholar (fatcat-based scholarly search).

    Free, no API key needed. Indexes papers preserved in the Internet Archive.
    """
    if _is_provider_disabled("internet_archive"):
        return []

    try:
        _throttle("internet_archive")
        params = {
            "q": query,
            "limit": min(limit, 25),
            "format": "json",
        }

        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://scholar.archive.org/search",
                params=params,
            )
            if resp.status_code == 429:
                _record_429("internet_archive")
                return []
            resp.raise_for_status()
            data = resp.json()
        _record_success("internet_archive")

        results = []
        for item in data.get("results", []):
            biblio = item.get("biblio", {})
            title = biblio.get("title", "")
            if not title:
                continue

            contrib = biblio.get("contrib_names", []) or []
            authors = list(contrib)

            year = None
            release_year = biblio.get("release_year")
            if release_year:
                try:
                    year = int(release_year)
                except (ValueError, TypeError):
                    pass

            abstract = item.get("abstracts", [{}])[0].get("body", "") if item.get("abstracts") else ""
            doi = biblio.get("doi", "") or ""
            url = f"https://doi.org/{doi}" if doi else item.get("fulltext", {}).get("access_url", "")

            results.append({
                "title": title,
                "abstract": abstract[:500],
                "authors": authors[:10],
                "year": year,
                "citation_count": 0,
                "url": url,
                "doi": doi,
                "paper_id_s2": "",
                "source": "internet_archive",
            })

        return results
    except Exception as e:
        logger.warning("Internet Archive Scholar search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Batch 2 search functions — additional sources
# ---------------------------------------------------------------------------


def _search_openaire(query: str, limit: int = 10) -> list[dict]:
    """Search OpenAIRE for European research publications."""
    _throttle("openaire")
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(
                "http://api.openaire.eu/search/publications",
                params={"keywords": query, "format": "json", "size": limit},
            )
        if resp.status_code != 200:
            _record_429("openaire") if resp.status_code == 429 else None
            return []
        _record_success("openaire")
        data = resp.json()
        results_raw = data.get("response", {}).get("results", {}).get("result", [])
        if not isinstance(results_raw, list):
            results_raw = [results_raw] if results_raw else []
        results = []
        for item in results_raw[:limit]:
            if not isinstance(item, dict):
                continue
            meta = (item.get("metadata") or {}).get("oaf:entity") or {}
            meta = meta.get("oaf:result") or {}
            if not meta:
                continue
            title_obj = meta.get("title", {})
            title = title_obj.get("$", "") if isinstance(title_obj, dict) else str(title_obj) if title_obj else ""
            if not title:
                continue
            desc = meta.get("description", {})
            abstract = desc.get("$", "") if isinstance(desc, dict) else str(desc) if desc else ""
            creators = meta.get("creator", [])
            if not isinstance(creators, list):
                creators = [creators] if creators else []
            authors = [c.get("$", "") if isinstance(c, dict) else str(c) for c in creators[:10]]
            date_str = meta.get("dateofacceptance", {})
            year_str = date_str.get("$", "") if isinstance(date_str, dict) else str(date_str) if date_str else ""
            year = _extract_year(year_str) or 0
            pid = meta.get("pid", [])
            if not isinstance(pid, list):
                pid = [pid] if pid else []
            doi = ""
            for p in pid:
                if isinstance(p, dict) and p.get("@classid") == "doi":
                    doi = p.get("$", "")
                    break
            results.append({
                "title": title,
                "abstract": abstract[:2000],
                "authors": authors,
                "year": year,
                "citation_count": 0,
                "url": f"https://explore.openaire.eu/search/publication?doi={doi}" if doi else "",
                "doi": doi,
                "paper_id_s2": "",
                "source": "openaire",
            })
        return results
    except Exception as e:
        logger.warning("OpenAIRE search failed: %s", e)
        return []


def _search_fatcat(query: str, limit: int = 10) -> list[dict]:
    """Search Fatcat (Internet Archive) scholarly catalog."""
    _throttle("fatcat")
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(
                "https://api.fatcat.wiki/v0/release/search",
                params={"q": query, "limit": limit},
            )
        if resp.status_code != 200:
            return []
        _record_success("fatcat")
        data = resp.json()
        results = []
        for item in data.get("results", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            contribs = item.get("contribs", []) or []
            authors = [c.get("raw_name", "") for c in contribs[:10] if c.get("raw_name")]
            ext_ids = item.get("ext_ids", {}) or {}
            doi = ext_ids.get("doi", "") or ""
            year_str = item.get("release_year") or item.get("release_date", "")
            year = int(year_str) if isinstance(year_str, int) else (_extract_year(str(year_str)) or 0)
            results.append({
                "title": title,
                "abstract": item.get("abstracts", [{}])[0].get("content", "") if item.get("abstracts") else "",
                "authors": authors,
                "year": year,
                "citation_count": 0,
                "url": f"https://fatcat.wiki/release/{item.get('ident', '')}",
                "doi": doi,
                "paper_id_s2": "",
                "source": "fatcat",
            })
        return results
    except Exception as e:
        logger.warning("Fatcat search failed: %s", e)
        return []


def _search_datacite(query: str, limit: int = 10) -> list[dict]:
    """Search DataCite for research outputs (datasets, preprints, software)."""
    _throttle("datacite")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.datacite.org/dois",
                params={"query": query, "page[size]": limit},
            )
        if resp.status_code != 200:
            return []
        _record_success("datacite")
        results = []
        for item in resp.json().get("data", [])[:limit]:
            attrs = item.get("attributes", {})
            titles = attrs.get("titles", [{}])
            title = titles[0].get("title", "") if titles else ""
            if not title:
                continue
            descs = attrs.get("descriptions", [])
            abstract = descs[0].get("description", "")[:2000] if descs else ""
            creators = attrs.get("creators", [])
            authors = [c.get("name", "") for c in creators[:10]]
            year = attrs.get("publicationYear") or 0
            doi = attrs.get("doi", "")
            results.append({
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "year": int(year) if year else 0,
                "citation_count": 0,
                "url": f"https://doi.org/{doi}" if doi else "",
                "doi": doi,
                "paper_id_s2": "",
                "source": "datacite",
            })
        return results
    except Exception as e:
        logger.warning("DataCite search failed: %s", e)
        return []


def _search_dimensions(query: str, limit: int = 10) -> list[dict]:
    """Search Dimensions API (requires DIMENSIONS_API_KEY)."""
    api_key = os.environ.get("DIMENSIONS_API_KEY", "")
    if not api_key:
        return []
    _throttle("dimensions")
    try:
        dsl = f'search publications for "{query}" return publications[title+abstract+doi+year+authors+times_cited] limit {limit}'
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://app.dimensions.ai/api/dsl.json",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": dsl},
            )
        if resp.status_code != 200:
            if resp.status_code == 429:
                _record_429("dimensions")
            return []
        _record_success("dimensions")
        results = []
        for item in resp.json().get("publications", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            authors_raw = item.get("authors", [])
            authors = [a.get("last_name", "") + ", " + a.get("first_name", "") for a in authors_raw[:10]]
            results.append({
                "title": title,
                "abstract": item.get("abstract", "")[:2000],
                "authors": authors,
                "year": item.get("year", 0),
                "citation_count": item.get("times_cited", 0),
                "url": f"https://doi.org/{item['doi']}" if item.get("doi") else "",
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "dimensions",
            })
        return results
    except Exception as e:
        logger.warning("Dimensions search failed: %s", e)
        return []


def _search_inspire_hep(query: str, limit: int = 10) -> list[dict]:
    """Search INSPIRE-HEP for high-energy physics papers."""
    _throttle("inspire_hep")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://inspirehep.net/api/literature",
                params={"q": query, "size": limit, "fields": "titles,abstracts,authors,earliest_date,dois,citation_count"},
            )
        if resp.status_code != 200:
            return []
        _record_success("inspire_hep")
        results = []
        for hit in resp.json().get("hits", {}).get("hits", [])[:limit]:
            meta = hit.get("metadata", {})
            titles = meta.get("titles", [{}])
            title = titles[0].get("title", "") if titles else ""
            if not title:
                continue
            abstracts = meta.get("abstracts", [{}])
            abstract = abstracts[0].get("value", "")[:2000] if abstracts else ""
            authors_raw = meta.get("authors", [])
            authors = [a.get("full_name", "") for a in authors_raw[:10]]
            year = _extract_year(meta.get("earliest_date", "")) or 0
            dois = meta.get("dois", [])
            doi = dois[0].get("value", "") if dois else ""
            results.append({
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "year": year,
                "citation_count": meta.get("citation_count", 0),
                "url": f"https://inspirehep.net/literature/{hit.get('id', '')}",
                "doi": doi,
                "paper_id_s2": "",
                "source": "inspire_hep",
            })
        return results
    except Exception as e:
        logger.warning("INSPIRE-HEP search failed: %s", e)
        return []


def _search_eric(query: str, limit: int = 10) -> list[dict]:
    """Search ERIC for education research papers."""
    _throttle("eric")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.ies.ed.gov/eric/",
                params={"search": query, "rows": limit, "format": "json"},
            )
        if resp.status_code != 200:
            return []
        _record_success("eric")
        results = []
        for item in resp.json().get("response", {}).get("docs", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            authors = item.get("author", [])
            if isinstance(authors, str):
                authors = [authors]
            results.append({
                "title": title,
                "abstract": item.get("description", "")[:2000],
                "authors": authors[:10],
                "year": _extract_year(str(item.get("publicationdateyear", ""))) or 0,
                "citation_count": 0,
                "url": f"https://eric.ed.gov/?id={item.get('id', '')}",
                "doi": item.get("doi", "") or "",
                "paper_id_s2": "",
                "source": "eric",
            })
        return results
    except Exception as e:
        logger.warning("ERIC search failed: %s", e)
        return []


def _search_figshare(query: str, limit: int = 10) -> list[dict]:
    """Search Figshare for research outputs."""
    _throttle("figshare")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://api.figshare.com/v2/articles/search",
                json={"search_for": query, "page_size": limit},
            )
        if resp.status_code != 200:
            return []
        _record_success("figshare")
        results = []
        for item in resp.json()[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            authors = [a.get("full_name", "") for a in item.get("authors", [])[:10]]
            results.append({
                "title": title,
                "abstract": item.get("description", "")[:2000],
                "authors": authors,
                "year": _extract_year(item.get("published_date", "")) or 0,
                "citation_count": 0,
                "url": item.get("url_public_html", ""),
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "figshare",
            })
        return results
    except Exception as e:
        logger.warning("Figshare search failed: %s", e)
        return []


def _search_scielo(query: str, limit: int = 10) -> list[dict]:
    """Search SciELO for Latin American open-access research."""
    _throttle("scielo")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://search.scielo.org/",
                params={"q": query, "output": "json", "count": limit, "lang": "en"},
            )
        if resp.status_code != 200:
            return []
        _record_success("scielo")
        results = []
        for item in resp.json()[:limit] if isinstance(resp.json(), list) else []:
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title": title,
                "abstract": item.get("abstract", "")[:2000],
                "authors": item.get("authors", [])[:10],
                "year": item.get("year", 0),
                "citation_count": 0,
                "url": item.get("url", ""),
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "scielo",
            })
        return results
    except Exception as e:
        logger.warning("SciELO search failed: %s", e)
        return []


def _search_base(query: str, limit: int = 10) -> list[dict]:
    """Search BASE (Bielefeld Academic Search Engine)."""
    _throttle("base")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi",
                params={"func": "PerformSearch", "query": query, "hits": limit, "format": "json"},
            )
        if resp.status_code != 200:
            return []
        _record_success("base")
        data = resp.json()
        results = []
        docs = data.get("response", {}).get("docs", [])
        for item in docs[:limit]:
            title = item.get("dctitle", "")
            if not title:
                continue
            creators = item.get("dccreator", [])
            if isinstance(creators, str):
                creators = [creators]
            results.append({
                "title": title,
                "abstract": item.get("dcdescription", "")[:2000] if item.get("dcdescription") else "",
                "authors": creators[:10],
                "year": _extract_year(item.get("dcdate", "")) or 0,
                "citation_count": 0,
                "url": item.get("dclink", "") or item.get("dcidentifier", ""),
                "doi": item.get("dcdoi", ""),
                "paper_id_s2": "",
                "source": "base",
            })
        return results
    except Exception as e:
        logger.warning("BASE search failed: %s", e)
        return []


def _search_ieee(query: str, limit: int = 10) -> list[dict]:
    """Search IEEE Xplore (requires IEEE_API_KEY)."""
    api_key = os.environ.get("IEEE_API_KEY", "")
    if not api_key:
        return []
    _throttle("ieee")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://ieeexploreapi.ieee.org/api/v1/search/articles",
                params={"querytext": query, "max_records": limit, "apikey": api_key},
            )
        if resp.status_code != 200:
            if resp.status_code == 429:
                _record_429("ieee")
            return []
        _record_success("ieee")
        results = []
        for item in resp.json().get("articles", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            authors_raw = item.get("authors", {}).get("authors", [])
            authors = [a.get("full_name", "") for a in authors_raw[:10]]
            results.append({
                "title": title,
                "abstract": item.get("abstract", "")[:2000],
                "authors": authors,
                "year": int(item.get("publication_year", 0) or 0),
                "citation_count": item.get("citing_paper_count", 0),
                "url": item.get("html_url", "") or item.get("pdf_url", ""),
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "ieee",
            })
        return results
    except Exception as e:
        logger.warning("IEEE Xplore search failed: %s", e)
        return []


def _search_philpapers(query: str, limit: int = 10) -> list[dict]:
    """Search PhilPapers for philosophy papers."""
    _throttle("philpapers")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://philpapers.org/search",
                params={"sqc": query, "format": "json", "limit": limit},
            )
        if resp.status_code != 200:
            return []
        _record_success("philpapers")
        results = []
        data = resp.json() if isinstance(resp.json(), list) else resp.json().get("results", [])
        for item in data[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title": title,
                "abstract": item.get("abstract", "")[:2000],
                "authors": item.get("authors", [])[:10],
                "year": item.get("year", 0),
                "citation_count": 0,
                "url": item.get("url", ""),
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "philpapers",
            })
        return results
    except Exception as e:
        logger.warning("PhilPapers search failed: %s", e)
        return []


def _search_cinii(query: str, limit: int = 10) -> list[dict]:
    """Search CiNii for Japanese academic literature."""
    _throttle("cinii")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://cir.nii.ac.jp/opensearch/articles",
                params={"q": query, "count": limit, "format": "json"},
            )
        if resp.status_code != 200:
            return []
        _record_success("cinii")
        results = []
        items = resp.json().get("items", []) if isinstance(resp.json(), dict) else []
        for item in items[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title": title,
                "abstract": item.get("description", "")[:2000],
                "authors": item.get("creator", [])[:10] if isinstance(item.get("creator"), list) else [item.get("creator", "")],
                "year": _extract_year(item.get("publicationDate", "")) or 0,
                "citation_count": 0,
                "url": item.get("link", ""),
                "doi": item.get("doi", ""),
                "paper_id_s2": "",
                "source": "cinii",
            })
        return results
    except Exception as e:
        logger.warning("CiNii search failed: %s", e)
        return []


def _search_sciencedirect(query: str, limit: int = 10) -> list[dict]:
    """Search Elsevier ScienceDirect (requires ELSEVIER_API_KEY)."""
    api_key = os.environ.get("ELSEVIER_API_KEY", "")
    if not api_key:
        return []
    _throttle("sciencedirect")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.elsevier.com/content/search/sciencedirect",
                params={"query": query, "count": limit},
                headers={"X-ELS-APIKey": api_key, "Accept": "application/json"},
            )
        if resp.status_code != 200:
            if resp.status_code == 429:
                _record_429("sciencedirect")
            return []
        _record_success("sciencedirect")
        results = []
        entries = resp.json().get("search-results", {}).get("entry", [])
        for item in entries[:limit]:
            title = item.get("dc:title", "")
            if not title:
                continue
            authors_str = item.get("dc:creator", "")
            authors = [authors_str] if authors_str else []
            doi = item.get("prism:doi", "")
            results.append({
                "title": title,
                "abstract": item.get("dc:description", "")[:2000],
                "authors": authors,
                "year": _extract_year(item.get("prism:coverDate", "")) or 0,
                "citation_count": 0,
                "url": item.get("prism:url", "") or (f"https://doi.org/{doi}" if doi else ""),
                "doi": doi,
                "paper_id_s2": "",
                "source": "sciencedirect",
            })
        return results
    except Exception as e:
        logger.warning("ScienceDirect search failed: %s", e)
        return []


def _search_wos(query: str, limit: int = 10) -> list[dict]:
    """Search Web of Science (requires WOS_API_KEY)."""
    api_key = os.environ.get("WOS_API_KEY", "")
    if not api_key:
        return []
    _throttle("wos")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.clarivate.com/api/wos",
                params={"databaseId": "WOS", "usrQuery": f"TS={query}", "count": limit},
                headers={"X-ApiKey": api_key},
            )
        if resp.status_code != 200:
            if resp.status_code == 429:
                _record_429("wos")
            return []
        _record_success("wos")
        results = []
        records = resp.json().get("Data", {}).get("Records", {}).get("records", {}).get("REC", [])
        if not isinstance(records, list):
            records = [records] if records else []
        for item in records[:limit]:
            static = item.get("static_data", {}).get("summary", {})
            titles = static.get("titles", {}).get("title", [])
            title = ""
            for t in (titles if isinstance(titles, list) else [titles]):
                if isinstance(t, dict) and t.get("type") == "item":
                    title = t.get("content", "")
                    break
            if not title:
                continue
            names = static.get("names", {}).get("name", [])
            if not isinstance(names, list):
                names = [names]
            authors = [n.get("full_name", "") for n in names[:10] if isinstance(n, dict)]
            pub_info = static.get("pub_info", {})
            year = int(pub_info.get("pubyear", 0) or 0)
            doi_list = item.get("dynamic_data", {}).get("cluster_related", {}).get("identifiers", {}).get("identifier", [])
            doi = ""
            if isinstance(doi_list, list):
                for d in doi_list:
                    if isinstance(d, dict) and d.get("type") == "doi":
                        doi = d.get("value", "")
                        break
            results.append({
                "title": title,
                "abstract": "",
                "authors": authors,
                "year": year,
                "citation_count": 0,
                "url": f"https://doi.org/{doi}" if doi else "",
                "doi": doi,
                "paper_id_s2": "",
                "source": "wos",
            })
        return results
    except Exception as e:
        logger.warning("Web of Science search failed: %s", e)
        return []


def _search_google_books(query: str, limit: int = 10) -> list[dict]:
    """Search Google Books for academic monographs."""
    _throttle("google_books")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": query, "maxResults": min(limit, 40)},
            )
        if resp.status_code != 200:
            return []
        _record_success("google_books")
        results = []
        for item in resp.json().get("items", [])[:limit]:
            info = item.get("volumeInfo", {})
            title = info.get("title", "")
            if not title:
                continue
            authors = info.get("authors", [])[:10]
            year_str = info.get("publishedDate", "")
            isbns = info.get("industryIdentifiers", [])
            doi = ""
            for isbn in isbns:
                if isbn.get("type") == "DOI":
                    doi = isbn.get("identifier", "")
            results.append({
                "title": title,
                "abstract": info.get("description", "")[:2000],
                "authors": authors,
                "year": _extract_year(year_str) or 0,
                "citation_count": 0,
                "url": info.get("infoLink", ""),
                "doi": doi,
                "paper_id_s2": "",
                "source": "google_books",
            })
        return results
    except Exception as e:
        logger.warning("Google Books search failed: %s", e)
        return []


def _search_open_library(query: str, limit: int = 10) -> list[dict]:
    """Search Open Library for books and academic works."""
    _throttle("open_library")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://openlibrary.org/search.json",
                params={"q": query, "limit": limit},
            )
        if resp.status_code != 200:
            return []
        _record_success("open_library")
        results = []
        for item in resp.json().get("docs", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            authors = item.get("author_name", [])[:10]
            year = item.get("first_publish_year", 0) or 0
            key = item.get("key", "")
            results.append({
                "title": title,
                "abstract": "",
                "authors": authors,
                "year": int(year),
                "citation_count": 0,
                "url": f"https://openlibrary.org{key}" if key else "",
                "doi": "",
                "paper_id_s2": "",
                "source": "open_library",
            })
        return results
    except Exception as e:
        logger.warning("Open Library search failed: %s", e)
        return []


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
    {
        "name": "consensus",
        "display_name": "Consensus (AI semantic search)",
        "requires_key": True,
        "env_var": "CONSENSUS_API_KEY",
        "free": False,
        "url": "https://consensus.app/",
    },
    {
        "name": "elicit",
        "display_name": "Elicit (AI research assistant)",
        "requires_key": True,
        "env_var": "ELICIT_API_KEY",
        "free": False,
        "url": "https://elicit.com/",
    },
    {
        "name": "scite",
        "display_name": "Scite.ai (smart citations)",
        "requires_key": True,
        "env_var": "SCITE_API_KEY",
        "free": False,
        "url": "https://scite.ai/",
    },
    {
        "name": "biorxiv",
        "display_name": "bioRxiv/medRxiv",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://www.biorxiv.org/",
    },
    {
        "name": "plos",
        "display_name": "PLOS",
        "requires_key": True,
        "env_var": "PLOS_API_KEY",
        "free": False,
        "url": "https://plos.org/",
    },
    {
        "name": "springer",
        "display_name": "Springer Nature",
        "requires_key": True,
        "env_var": "SPRINGER_API_KEY",
        "free": False,
        "url": "https://www.springernature.com/",
    },
    {
        "name": "hal",
        "display_name": "HAL Open Archive",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://hal.science/",
    },
    {
        "name": "zenodo",
        "display_name": "Zenodo",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://zenodo.org/",
    },
    {
        "name": "nasa_ads",
        "display_name": "NASA ADS",
        "requires_key": True,
        "env_var": "ADS_API_KEY",
        "free": False,
        "url": "https://ui.adsabs.harvard.edu/",
    },
    {
        "name": "doaj",
        "display_name": "DOAJ",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://doaj.org/",
    },
    {
        "name": "dblp",
        "display_name": "DBLP",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://dblp.org/",
    },
    {
        "name": "internet_archive",
        "display_name": "Internet Archive Scholar",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://scholar.archive.org/",
    },
    {
        "name": "openaire",
        "display_name": "OpenAIRE",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://www.openaire.eu/",
    },
    {
        "name": "fatcat",
        "display_name": "Fatcat (Internet Archive)",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://fatcat.wiki/",
    },
    {
        "name": "datacite",
        "display_name": "DataCite",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://datacite.org/",
    },
    {
        "name": "dimensions",
        "display_name": "Dimensions",
        "requires_key": True,
        "env_var": "DIMENSIONS_API_KEY",
        "free": False,
        "url": "https://www.dimensions.ai/",
    },
    {
        "name": "inspire_hep",
        "display_name": "INSPIRE-HEP",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://inspirehep.net/",
    },
    {
        "name": "eric",
        "display_name": "ERIC (Education)",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://eric.ed.gov/",
    },
    {
        "name": "figshare",
        "display_name": "Figshare",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://figshare.com/",
    },
    {
        "name": "scielo",
        "display_name": "SciELO",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://scielo.org/",
    },
    {
        "name": "base",
        "display_name": "BASE (Bielefeld)",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://www.base-search.net/",
    },
    {
        "name": "ieee",
        "display_name": "IEEE Xplore",
        "requires_key": True,
        "env_var": "IEEE_API_KEY",
        "free": False,
        "url": "https://ieeexplore.ieee.org/",
    },
    {
        "name": "philpapers",
        "display_name": "PhilPapers",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://philpapers.org/",
    },
    {
        "name": "cinii",
        "display_name": "CiNii (Japan)",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://cir.nii.ac.jp/",
    },
    {
        "name": "sciencedirect",
        "display_name": "ScienceDirect (Elsevier)",
        "requires_key": True,
        "env_var": "SCIENCEDIRECT_API_KEY",
        "free": False,
        "url": "https://www.sciencedirect.com/",
    },
    {
        "name": "wos",
        "display_name": "Web of Science",
        "requires_key": True,
        "env_var": "WOS_API_KEY",
        "free": False,
        "url": "https://www.webofscience.com/",
    },
    {
        "name": "google_books",
        "display_name": "Google Books",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://books.google.com/",
    },
    {
        "name": "open_library",
        "display_name": "Open Library",
        "requires_key": False,
        "env_var": None,
        "free": True,
        "url": "https://openlibrary.org/",
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
    # Cap query length to avoid URL-too-long errors (many APIs fail with >200 char queries)
    if len(query) > 200:
        # Take the first meaningful portion (first line/sentence)
        short = query.split("\n")[0].strip()
        if len(short) > 200:
            short = short[:200]
        query = short

    # Default sources: all free ones
    default_sources = [
        "crossref", "arxiv", "semantic_scholar", "openalex", "pubmed",
        "europe_pmc", "biorxiv", "doaj", "dblp",
        "datacite", "inspire_hep", "eric", "figshare", "scielo", "base",
        "philpapers", "cinii", "google_books", "open_library",
    ]

    # Add optional sources if their API key is configured
    if os.environ.get("CORE_API_KEY"):
        default_sources.append("core")
    if os.environ.get("LENS_API_KEY"):
        default_sources.append("lens")
    if os.environ.get("SCOPUS_API_KEY"):
        default_sources.append("scopus")
    if os.environ.get("SERPER_API_KEY"):
        default_sources.append("serper")
    if os.environ.get("PLOS_API_KEY"):
        default_sources.append("plos")
    if os.environ.get("SPRINGER_API_KEY"):
        default_sources.append("springer")
    if os.environ.get("ADS_API_KEY"):
        default_sources.append("nasa_ads")
    # AI semantic search sources — best relevance, use first when available
    if os.environ.get("CONSENSUS_API_KEY"):
        default_sources.insert(0, "consensus")
    if os.environ.get("ELICIT_API_KEY"):
        default_sources.insert(0, "elicit")
    if os.environ.get("SCITE_API_KEY"):
        default_sources.append("scite")
    if os.environ.get("DIMENSIONS_API_KEY"):
        default_sources.append("dimensions")
    if os.environ.get("IEEE_API_KEY"):
        default_sources.append("ieee")
    if os.environ.get("SCIENCEDIRECT_API_KEY"):
        default_sources.append("sciencedirect")
    if os.environ.get("WOS_API_KEY"):
        default_sources.append("wos")

    active_sources = sources if sources is not None else default_sources

    results: list[dict] = []
    seen_titles: set[str] = set()
    source_counts: dict[str, int] = {}  # track unique papers per source

    def _dedup_add(hits: list[dict], source_label: str = "") -> None:
        added = 0
        for h in hits:
            if not h.get("year"):
                continue  # Skip papers with no publication year
            key = h["title"].lower()[:50]
            if key not in seen_titles and h["title"]:
                seen_titles.add(key)
                results.append(h)
                added += 1
        if source_label:
            source_counts[source_label] = source_counts.get(source_label, 0) + added

    # Check local cache first
    try:
        cached = _search_cached(query, limit=limit)
        if cached:
            if year_from:
                cached = [c for c in cached if (c.get("year") or 0) >= year_from]
            _dedup_add(cached)
            if len(results) >= limit:
                logger.info("Cache hit: %d results for '%s'", len(results), query[:40])
                return results[:limit]
    except Exception:
        pass

    # Query ALL enabled sources with a per-source limit, then dedup at the end.
    # Previously we early-exited when len(results) >= limit, which meant only
    # the first 1-2 sources (Crossref, arXiv) ever got called.
    per_source = max(5, limit // max(len(active_sources), 1))
    _query_short = query[:80]
    logger.info("Searching %d sources for: '%s'", len(active_sources), _query_short)

    for source in active_sources:
        if _is_provider_disabled(source) or _is_source_exhausted(source):
            continue
        _pre_count = len(results)
        try:
            if source == "crossref":
                _dedup_add(_search_crossref(query, limit=per_source, year_from=year_from, mailto=mailto), source)
            elif source == "arxiv":
                _dedup_add(_search_arxiv(query, limit=per_source), source)
            elif source == "semantic_scholar":
                _dedup_add(_search_semantic_scholar(query, limit=per_source, year_from=year_from), source)
            elif source == "openalex":
                _dedup_add(_search_openalex(query, limit=per_source, mailto=mailto), source)
            elif source == "pubmed":
                _dedup_add(_search_pubmed(query, limit=per_source), source)
            elif source == "europe_pmc":
                _dedup_add(_search_europe_pmc(query, limit=per_source), source)
            elif source == "core":
                _dedup_add(_search_core(query, limit=per_source), source)
            elif source == "lens":
                _dedup_add(_search_lens(query, limit=per_source), source)
            elif source == "scopus":
                _dedup_add(_search_scopus(query, limit=per_source), source)
            elif source == "serper":
                serper_key = os.environ.get("SERPER_API_KEY", "")
                if serper_key:
                    _dedup_add(search_serper_scholar(query, api_key=serper_key, limit=per_source), source)
                else:
                    logger.warning("Serper enabled but SERPER_API_KEY not set — skipping")
            elif source == "consensus":
                _dedup_add(_search_consensus(query, limit=per_source, year_from=year_from), source)
            elif source == "elicit":
                _dedup_add(_search_elicit(query, limit=per_source, year_from=year_from), source)
            elif source == "scite":
                _dedup_add(_search_scite(query, limit=per_source), source)
            elif source == "biorxiv":
                _dedup_add(_search_biorxiv(query, limit=per_source), source)
            elif source == "plos":
                plos_key = os.environ.get("PLOS_API_KEY", "")
                if plos_key:
                    _dedup_add(_search_plos(query, limit=per_source), source)
                else:
                    logger.debug("PLOS enabled but PLOS_API_KEY not set — skipping")
            elif source == "springer":
                springer_key = os.environ.get("SPRINGER_API_KEY", "")
                if springer_key:
                    _dedup_add(_search_springer(query, limit=per_source), source)
                else:
                    logger.debug("Springer enabled but SPRINGER_API_KEY not set — skipping")
            elif source == "hal":
                _dedup_add(_search_hal(query, limit=per_source), source)
            elif source == "zenodo":
                _dedup_add(_search_zenodo(query, limit=per_source), source)
            elif source == "nasa_ads":
                ads_key = os.environ.get("ADS_API_KEY", "")
                if ads_key:
                    _dedup_add(_search_nasa_ads(query, limit=per_source), source)
                else:
                    logger.debug("NASA ADS enabled but ADS_API_KEY not set — skipping")
            elif source == "doaj":
                _dedup_add(_search_doaj(query, limit=per_source), source)
            elif source == "dblp":
                _dedup_add(_search_dblp(query, limit=per_source), source)
            elif source == "internet_archive":
                _dedup_add(_search_internet_archive(query, limit=per_source), source)
            elif source == "openaire":
                _dedup_add(_search_openaire(query, limit=per_source), source)
            elif source == "fatcat":
                _dedup_add(_search_fatcat(query, limit=per_source), source)
            elif source == "datacite":
                _dedup_add(_search_datacite(query, limit=per_source), source)
            elif source == "dimensions":
                dim_key = os.environ.get("DIMENSIONS_API_KEY", "")
                if dim_key:
                    _dedup_add(_search_dimensions(query, limit=per_source), source)
                else:
                    logger.debug("Dimensions enabled but DIMENSIONS_API_KEY not set — skipping")
            elif source == "inspire_hep":
                _dedup_add(_search_inspire_hep(query, limit=per_source), source)
            elif source == "eric":
                _dedup_add(_search_eric(query, limit=per_source), source)
            elif source == "figshare":
                _dedup_add(_search_figshare(query, limit=per_source), source)
            elif source == "scielo":
                _dedup_add(_search_scielo(query, limit=per_source), source)
            elif source == "base":
                _dedup_add(_search_base(query, limit=per_source), source)
            elif source == "ieee":
                ieee_key = os.environ.get("IEEE_API_KEY", "")
                if ieee_key:
                    _dedup_add(_search_ieee(query, limit=per_source), source)
                else:
                    logger.debug("IEEE enabled but IEEE_API_KEY not set — skipping")
            elif source == "philpapers":
                _dedup_add(_search_philpapers(query, limit=per_source), source)
            elif source == "cinii":
                _dedup_add(_search_cinii(query, limit=per_source), source)
            elif source == "sciencedirect":
                sd_key = os.environ.get("SCIENCEDIRECT_API_KEY", "")
                if sd_key:
                    _dedup_add(_search_sciencedirect(query, limit=per_source), source)
                else:
                    logger.debug("ScienceDirect enabled but SCIENCEDIRECT_API_KEY not set — skipping")
            elif source == "wos":
                wos_key = os.environ.get("WOS_API_KEY", "")
                if wos_key:
                    _dedup_add(_search_wos(query, limit=per_source), source)
                else:
                    logger.debug("Web of Science enabled but WOS_API_KEY not set — skipping")
            elif source == "google_books":
                _dedup_add(_search_google_books(query, limit=per_source), source)
            elif source == "open_library":
                _dedup_add(_search_open_library(query, limit=per_source), source)
            else:
                logger.warning("Unknown source: %s", source)
            _added_this = len(results) - _pre_count
            if _added_this > 0:
                _record_nonzero_results(source)
            else:
                _record_zero_results(source)
            logger.debug("Extended search [%s]: %d new, %d total so far",
                         source, source_counts.get(source, 0), len(results))
        except Exception as e:
            _record_failure(source)
            logger.warning("Extended search [%s] failed: %s", source, e)

    # Log source summary
    if source_counts:
        summary = ", ".join(f"{s}: {c}" for s, c in source_counts.items() if c > 0)
        logger.info("Source breakdown: %s (total unique: %d)", summary, len(results))

    # Store in local cache
    _cache_papers(results)

    return results[:limit]


def search_domain_optimized(
    query: str,
    *,
    domain_qualifier: str = "",
    limit: int = 10,
    year_from: int | None = None,
    mailto: str | None = None,
    sources: list[str] | None = None,
) -> list[dict]:
    """Search using database-specific strategies optimized per API.

    Different academic APIs have different strengths:
    - Semantic Scholar: NLP-based semantic matching (best for conceptual queries)
    - Crossref: keyword matching (best with short, quoted phrases)
    - OpenAlex: concept-aware search (supports Boolean and field filtering)
    - PubMed: MeSH terms and biomedical synonyms
    - arXiv: good for STEM preprints, simple keyword matching

    This function sends DIFFERENT query formats to each API to maximize
    relevance, unlike search_papers_extended which sends the same query to all.
    """
    default_sources = ["semantic_scholar", "crossref", "openalex", "pubmed", "arxiv", "europe_pmc"]
    active_sources = sources if sources is not None else default_sources

    results: list[dict] = []
    seen: set[str] = set()

    def _add(hits: list[dict], label: str = "") -> None:
        for h in hits:
            if not h.get("year"):
                continue
            key = h["title"].lower()[:50]
            if key not in seen and h["title"]:
                seen.add(key)
                results.append(h)

    per_source = max(5, limit // max(len(active_sources), 1))

    # Build database-specific queries
    dq = domain_qualifier.strip().strip('"\'') if domain_qualifier else ""
    q_words = query.split()
    # Extract key terms (no stopwords) — strip quotes to avoid malformed API queries
    _stop = {"the","a","an","of","in","on","for","and","or","to","is","are","by","at","from",
             "with","as","its","this","that","how","what","why","which","between","across"}
    key_terms = [w.strip(".:,;!?()'\"") for w in q_words if w.lower().strip(".:,;!?()'\"") not in _stop and len(w.strip(".:,;!?()'\"")) > 2]

    for source in active_sources:
        if _is_provider_disabled(source) or _is_source_exhausted(source):
            continue
        _pre = len(results)
        try:
            if source == "semantic_scholar":
                # S2 does NLP internally — send the full natural language query
                # It handles semantic matching, so longer = better context
                s2_query = query if len(query) < 150 else " ".join(key_terms[:10])
                _add(_search_semantic_scholar(s2_query, limit=per_source, year_from=year_from), source)

            elif source == "crossref":
                # Crossref is pure keyword OR-matching — short quoted phrases work best
                if dq:
                    cr_query = f'"{dq}" ' + " ".join(f'"{t}"' if " " not in t else t for t in key_terms[:4])
                else:
                    cr_query = " ".join(key_terms[:6])
                _add(_search_crossref(cr_query, limit=per_source, year_from=year_from, mailto=mailto), source)

            elif source == "openalex":
                # OpenAlex supports phrase search with quotes and Boolean
                if dq:
                    # Deduplicate: remove key_terms that are already in the domain qualifier
                    dq_words = {w.lower() for w in dq.split()}
                    extra_terms = [t for t in key_terms if t.lower() not in dq_words]
                    oa_query = f'"{dq}" ' + " ".join(extra_terms[:5])
                else:
                    oa_query = " ".join(key_terms[:6])
                _add(_search_openalex(oa_query, limit=per_source, mailto=mailto), source)

            elif source == "pubmed":
                # PubMed: use AND to combine domain with specifics
                if dq:
                    pm_query = f'("{dq}"[Title/Abstract]) AND ({" OR ".join(key_terms[:4])}[Title/Abstract])'
                else:
                    pm_query = " AND ".join(key_terms[:4])
                _add(_search_pubmed(pm_query, limit=per_source), source)

            elif source == "arxiv":
                # arXiv: _search_arxiv wraps in all:{query}, so just send clean keywords
                # arXiv doesn't support quoted phrases well — use plain terms
                if dq:
                    ax_query = f'{dq} {" ".join(key_terms[:3])}'
                else:
                    ax_query = " ".join(key_terms[:5])
                _add(_search_arxiv(ax_query, limit=per_source), source)

            elif source == "europe_pmc":
                # Europe PMC supports field-specific queries
                if dq:
                    epmc_query = f'(TITLE:"{dq}" OR ABSTRACT:"{dq}") AND ({" OR ".join(key_terms[:3])})'
                else:
                    epmc_query = query
                _add(_search_europe_pmc(epmc_query, limit=per_source), source)

            elif source == "consensus":
                _add(_search_consensus(query, limit=per_source, year_from=year_from), source)
            elif source == "elicit":
                _add(_search_elicit(query, limit=per_source, year_from=year_from), source)
            elif source == "scite":
                _add(_search_scite(query, limit=per_source), source)
            elif source == "serper":
                serper_key = os.environ.get("SERPER_API_KEY", "")
                if serper_key:
                    _add(search_serper_scholar(query, api_key=serper_key, limit=per_source), source)

            _added = len(results) - _pre
            if _added > 0:
                _record_nonzero_results(source)
            else:
                _record_zero_results(source)
            logger.debug("Domain-optimized [%s]: %d total so far", source, len(results))
        except Exception as e:
            _record_failure(source)
            logger.warning("Domain-optimized [%s] failed: %s", source, e)

    _cache_papers(results)
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
    sources: list[str] | None = None,
) -> list[dict]:
    """Search for papers that serve a specific evidence role for a claim.

    Args:
        claim: Description of the claim (used as search query base).
        evidence_role: One of "supporting", "counter", "methodological", "foundational".
        limit: Max papers to return.
        year_from: Minimum publication year.
        mailto: Email for Crossref polite pool.
        sources: Which sources to use (passed to search_papers_extended).

    For "counter" role, generates negation queries automatically.
    For "methodological", focuses on method/comparison terms.
    For "foundational", removes year filter and sorts by citation count.
    """
    queries: list[str] = []

    if evidence_role == "counter":
        queries.append(claim)
        # Extract a meaningful topic phrase (not just first N words, which
        # truncates mid-sentence).  Take the last noun-phrase-like segment
        # after common prepositions, or fall back to the whole claim.
        import re as _re
        # Try to extract the core topic after "of", "for", "against", "on"
        m = _re.search(r'\b(?:of|for|against|on|about|regarding)\s+(.+)', claim, _re.I)
        topic = m.group(1).strip() if m else claim
        # Cap at ~10 words to keep the query focused but coherent
        topic_words = topic.split()
        if len(topic_words) > 10:
            topic = " ".join(topic_words[:10])
        queries.append(f"{topic} limitations")
        queries.append(f"{topic} criticism")
        queries.append(f"{topic} no effect")
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
            hits = search_papers_extended(q, limit=limit, year_from=year_from, mailto=mailto, sources=sources)
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
    sources: list[str] | None = None,
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
                year_from=year_from, mailto=mailto, sources=sources,
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
