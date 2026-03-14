"""Academic paper search via free public APIs + optional Serper.dev.

Search order (all free, no API key required):
  1. Serper.dev Google Scholar (optional, requires API key — higher quality)
  2. Crossref REST API — DOI metadata, polite pool (~5 req/s with mailto)
  3. arXiv API — preprints in STEM (3-second delay between requests)
  4. Semantic Scholar — broad coverage, aggressive rate limits (429 common)

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

# Semantic Scholar (free but aggressive rate limits)
_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_FIELDS = "paperId,title,abstract,year,authors,citationCount,url,externalIds"


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

    return results[:limit]


def get_paper_details(paper_id: str) -> dict | None:
    """Fetch full paper details from Semantic Scholar by paper ID or DOI."""
    try:
        with httpx.Client(timeout=15) as client:
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

        results.append({
            "title": title,
            "abstract": abstract[:500],
            "authors": authors[:10],
            "year": year,
            "citation_count": item.get("is-referenced-by-count", 0),
            "url": url,
            "doi": doi,
            "paper_id_s2": "",
            "source": "crossref",
        })

    return results


def _crossref_by_doi(doi: str) -> dict | None:
    """Look up a single DOI via Crossref. Returns a paper dict or None."""
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

    return {
        "title": title,
        "abstract": abstract[:500],
        "authors": authors[:10],
        "year": year,
        "citation_count": item.get("is-referenced-by-count", 0),
        "url": f"https://doi.org/{doi}",
        "doi": doi,
        "source": "crossref",
    }


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

    # Respect arXiv's 3-second delay rule for subsequent calls
    time.sleep(3)

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
        with httpx.Client(timeout=20) as client:
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
    fields = "paperId,title,abstract,year,authors,citationCount,url,externalIds"
    params = {"fields": fields, "limit": min(limit, 500)}
    data = None

    for attempt in range(3):
        try:
            with httpx.Client(timeout=20) as client:
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
    for item in data.get("data", []):
        cited = item.get("citedPaper", {})
        if not cited or not cited.get("title"):
            continue
        results.append(_normalize_s2_result(cited))

    # Sort by citation count — most-cited first
    results.sort(key=lambda x: x.get("citation_count", 0), reverse=True)
    return results


def fetch_paper_citations(paper_id_s2: str, limit: int = 50) -> list[dict]:
    """Fetch papers that CITE a given paper (forward citations).

    Uses Semantic Scholar /paper/{id}/citations endpoint.
    Returns normalized paper dicts sorted by citation count (descending).
    """
    fields = "paperId,title,abstract,year,authors,citationCount,url,externalIds"
    params = {"fields": fields, "limit": min(limit, 500)}
    data = None

    for attempt in range(3):
        try:
            with httpx.Client(timeout=20) as client:
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
    for item in data.get("data", []):
        citing = item.get("citingPaper", {})
        if not citing or not citing.get("title"):
            continue
        results.append(_normalize_s2_result(citing))

    results.sort(key=lambda x: x.get("citation_count", 0), reverse=True)
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

    # Sort by citation count and return top results
    results.sort(key=lambda x: x.get("citation_count", 0), reverse=True)
    return results[:limit]


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
        with httpx.Client(timeout=10) as client:
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
        with httpx.Client(timeout=10) as client:
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
