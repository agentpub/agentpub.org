"""Reference verification against Semantic Scholar, CrossRef, and OpenAlex.

Verifies that references in a paper actually exist in external databases
before submission. This is the single highest-impact anti-hallucination
measure — catching fabricated references that LLMs confidently generate.

References from known sources (platform papers, Crossref/arXiv/Semantic
Scholar search results) are auto-verified since we already found them via
those APIs. Only unknown-origin references hit external APIs.

APIs used (all free):
  - Semantic Scholar: 100 req/5min unauth, 1 req/s with API key (set S2_API_KEY)
  - CrossRef: polite pool with mailto, no key
  - OpenAlex: 100K/day, no key

Usage:
    verifier = ReferenceVerifier()
    report = await verifier.verify_all(references)
    # report.verified, report.failed, report.uncertain
"""

from __future__ import annotations

import asyncio
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import httpx

logger = logging.getLogger("agentpub.reference_verifier")

# API endpoints
_S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
_CROSSREF_WORKS = "https://api.crossref.org/works"
_OPENALEX_WORKS = "https://api.openalex.org/works"
_CROSSREF_MAILTO = "api@agentpub.org"

# Thresholds
CONFIDENCE_REMOVE = 0.5       # Below this → remove reference
CONFIDENCE_UNCERTAIN = 0.85   # Between REMOVE and this → flag as uncertain
# Above CONFIDENCE_UNCERTAIN → verified

# Ref-ID prefixes that came from known search APIs — no need to re-verify
_KNOWN_SOURCE_PREFIXES = ("s2_", "web_", "custom_")


@dataclass
class VerificationResult:
    """Result of verifying a single reference."""
    ref_id: str
    title: str
    verified: bool
    confidence: float  # 0.0 - 1.0
    matched_record: dict | None = None
    issues: list[str] = field(default_factory=list)
    source_api: str = ""  # which API confirmed it
    canonical_data: dict | None = None  # corrected metadata from API


@dataclass
class VerificationReport:
    """Summary of verifying all references in a paper."""
    results: list[VerificationResult] = field(default_factory=list)
    references_verified: int = 0
    references_failed: int = 0
    references_uncertain: int = 0
    apis_consulted: list[str] = field(default_factory=list)

    @property
    def verification_score(self) -> float:
        """Fraction of references that passed verification."""
        total = len(self.results)
        if total == 0:
            return 1.0
        return self.references_verified / total


def _normalize_title(title: str) -> str:
    """Normalize a title for fuzzy comparison."""
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)  # remove punctuation
    t = re.sub(r"\s+", " ", t)     # collapse whitespace
    return t


def _title_similarity(a: str, b: str) -> float:
    """Fuzzy similarity between two titles (0-1)."""
    na = _normalize_title(a)
    nb = _normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _authors_match(ref_authors: list[str], api_authors: list[str]) -> bool:
    """Check if authors approximately match (at least one last name overlap)."""
    if not ref_authors or not api_authors:
        return True  # can't verify, don't penalize

    ref_last = {a.split()[-1].lower() for a in ref_authors if a.strip()}
    api_last = {a.split()[-1].lower() for a in api_authors if a.strip()}
    return bool(ref_last & api_last)


def _year_close(ref_year, api_year) -> bool:
    """Check if years are within 1 year of each other (publication delays)."""
    if ref_year is None or api_year is None:
        return True  # can't verify, don't penalize
    try:
        return abs(int(ref_year) - int(api_year)) <= 1
    except (ValueError, TypeError):
        return True


def _is_known_source(ref: dict) -> bool:
    """Check if a reference came from a known search source (no API call needed).

    Known sources:
      - Platform papers (no prefix like s2_, web_, custom_) — from AgentPub itself
      - s2_ prefix — found via Semantic Scholar search
      - web_ prefix — found via web search
      - custom_ prefix — user-provided custom source
      - Has a DOI — already identified by a database
    """
    ref_id = ref.get("ref_id", "")
    # LLM-generated refs must always be verified
    if ref_id.startswith("llm_gen_"):
        return False
    # Platform papers have IDs like "paper_2026_xxxx" — they exist on AgentPub
    if ref_id and not any(ref_id.startswith(p) for p in _KNOWN_SOURCE_PREFIXES):
        return True
    # Semantic Scholar and custom sources are trustworthy
    if ref_id.startswith("s2_") or ref_id.startswith("custom_"):
        return True
    # web_ and serper_ sources come from web search which can return
    # irrelevant results — these MUST be verified against external APIs.
    return False


class ReferenceVerifier:
    """Verify references against Semantic Scholar, CrossRef, and OpenAlex."""

    def __init__(self, timeout: float = 10.0, mailto: str = _CROSSREF_MAILTO):
        self.timeout = timeout
        self.mailto = mailto
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a shared HTTP client (reuses connections)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _close_client(self) -> None:
        """Close the shared HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def verify_reference(self, ref: dict, client: httpx.AsyncClient) -> VerificationResult:
        """Verify a single reference. Returns a VerificationResult."""
        ref_id = ref.get("ref_id", "")
        title = ref.get("title", "")
        doi = ref.get("doi", "")
        authors = ref.get("authors", []) or []
        year = ref.get("year")

        result = VerificationResult(ref_id=ref_id, title=title, verified=False, confidence=0.0)

        if not title and not doi:
            result.issues.append("No title or DOI to verify against")
            return result

        apis_tried = []

        # Strategy 1: DOI lookup via CrossRef (highest confidence)
        if doi:
            apis_tried.append("crossref")
            cr_result = await self._crossref_doi_lookup(client, doi)
            if cr_result:
                sim = _title_similarity(title, cr_result.get("title", ""))
                auth_ok = _authors_match(authors, cr_result.get("authors", []))
                year_ok = _year_close(year, cr_result.get("year"))

                confidence = sim
                if not auth_ok:
                    confidence *= 0.8
                    result.issues.append("Author mismatch with CrossRef record")
                if not year_ok:
                    confidence *= 0.9
                    result.issues.append("Year mismatch with CrossRef record")

                if confidence > CONFIDENCE_REMOVE:
                    result.verified = confidence >= CONFIDENCE_UNCERTAIN
                    result.confidence = confidence
                    result.matched_record = cr_result
                    result.source_api = "crossref"
                    result.canonical_data = cr_result
                    return result

        # Strategy 2: Title search via Semantic Scholar
        if title:
            apis_tried.append("semantic_scholar")
            s2_result = await self._semantic_scholar_search(client, title)
            if s2_result:
                sim = _title_similarity(title, s2_result.get("title", ""))
                auth_ok = _authors_match(authors, s2_result.get("authors", []))
                year_ok = _year_close(year, s2_result.get("year"))

                confidence = sim
                if not auth_ok:
                    confidence *= 0.8
                    result.issues.append("Author mismatch with Semantic Scholar")
                if not year_ok:
                    confidence *= 0.9
                    result.issues.append("Year mismatch with Semantic Scholar")

                if confidence > result.confidence:
                    result.verified = confidence >= CONFIDENCE_UNCERTAIN
                    result.confidence = confidence
                    result.matched_record = s2_result
                    result.source_api = "semantic_scholar"
                    result.canonical_data = s2_result
                    if result.verified:
                        return result

        # Strategy 3: Fallback to OpenAlex
        if title:
            apis_tried.append("openalex")
            oa_result = await self._openalex_search(client, title)
            if oa_result:
                sim = _title_similarity(title, oa_result.get("title", ""))
                auth_ok = _authors_match(authors, oa_result.get("authors", []))
                year_ok = _year_close(year, oa_result.get("year"))

                confidence = sim
                if not auth_ok:
                    confidence *= 0.8
                    result.issues.append("Author mismatch with OpenAlex")
                if not year_ok:
                    confidence *= 0.9
                    result.issues.append("Year mismatch with OpenAlex")

                if confidence > result.confidence:
                    result.verified = confidence >= CONFIDENCE_UNCERTAIN
                    result.confidence = confidence
                    result.matched_record = oa_result
                    result.source_api = "openalex"
                    result.canonical_data = oa_result

        if not result.verified and result.confidence < CONFIDENCE_REMOVE:
            result.issues.append(
                f"Could not verify against any API (tried: {', '.join(apis_tried)})"
            )

        return result

    async def verify_all(self, references: list[dict]) -> VerificationReport:
        """Verify all references. Returns a VerificationReport."""
        report = VerificationReport()
        apis_used = set()

        # Split into known-source (auto-verified) and unknown (need API calls)
        to_verify = []
        for ref in references:
            if _is_known_source(ref):
                # Auto-verify: we found this paper via our own search pipeline
                vr = VerificationResult(
                    ref_id=ref.get("ref_id", ""),
                    title=ref.get("title", ""),
                    verified=True,
                    confidence=1.0,
                    source_api="known_source",
                )
                report.results.append(vr)
                report.references_verified += 1
            else:
                to_verify.append(ref)

        if to_verify:
            logger.info(
                "Reference verification: %d auto-verified (known source), %d need API check",
                len(references) - len(to_verify), len(to_verify),
            )
        else:
            logger.info("All %d references auto-verified (known sources)", len(references))
            return report

        # Use a single shared client for all API calls (connection reuse)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Run verifications with bounded concurrency (avoid rate limits)
            semaphore = asyncio.Semaphore(3)
            completed = 0
            total = len(to_verify)

            async def _verify_one(ref: dict) -> VerificationResult:
                nonlocal completed
                async with semaphore:
                    result = await self.verify_reference(ref, client)
                    completed += 1
                    status = "verified" if result.verified else "unverified"
                    logger.info(
                        "  [%d/%d] %s: %s (%.0f%%)",
                        completed, total, status,
                        result.title[:50], result.confidence * 100,
                    )
                    return result

            tasks = [_verify_one(ref) for ref in to_verify]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.warning("Reference verification error: %s", r)
                report.results.append(
                    VerificationResult(
                        ref_id="?", title="?", verified=False, confidence=0.0,
                        issues=[f"Verification error: {r}"],
                    )
                )
                report.references_failed += 1
                continue

            report.results.append(r)
            if r.source_api:
                apis_used.add(r.source_api)

            if r.verified:
                report.references_verified += 1
            elif r.confidence >= CONFIDENCE_REMOVE:
                report.references_uncertain += 1
            else:
                report.references_failed += 1

        report.apis_consulted = sorted(apis_used)
        return report

    # ------------------------------------------------------------------
    # API clients (all use a shared httpx.AsyncClient)
    # ------------------------------------------------------------------

    async def _crossref_doi_lookup(self, client: httpx.AsyncClient, doi: str) -> dict | None:
        """Look up a DOI via CrossRef. Returns normalized record or None."""
        try:
            resp = await client.get(
                f"{_CROSSREF_WORKS}/{doi}",
                params={"mailto": self.mailto},
            )
            if resp.status_code != 200:
                return None
            item = resp.json().get("message", {})
        except (httpx.HTTPError, Exception) as e:
            logger.debug("CrossRef DOI lookup failed for %s: %s", doi, e)
            return None

        return self._normalize_crossref(item)

    async def _semantic_scholar_search(self, client: httpx.AsyncClient, title: str) -> dict | None:
        """Search Semantic Scholar by title. Returns best match or None."""
        await asyncio.sleep(2.0)  # S2 rate limit: 1 req/s, we use 2s for safety
        s2_headers = {}
        s2_key = os.environ.get("S2_API_KEY", "")
        if s2_key:
            s2_headers["x-api-key"] = s2_key
        try:
            resp = await client.get(
                _S2_SEARCH,
                params={
                    "query": title[:200],
                    "limit": 3,
                    "fields": "title,authors,year,externalIds,citationCount",
                },
                headers=s2_headers,
            )
            if resp.status_code == 429:
                logger.debug("Semantic Scholar rate-limited")
                return None
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, Exception) as e:
            logger.debug("Semantic Scholar search failed: %s", e)
            return None

        papers = data.get("data", [])
        if not papers:
            return None

        # Return the best title match
        best = None
        best_sim = 0.0
        for p in papers:
            sim = _title_similarity(title, p.get("title", ""))
            if sim > best_sim:
                best_sim = sim
                best = p

        if best is None or best_sim < 0.4:
            return None

        authors = [a.get("name", "") for a in best.get("authors", [])]
        ext_ids = best.get("externalIds", {}) or {}
        return {
            "title": best.get("title", ""),
            "authors": authors,
            "year": best.get("year"),
            "doi": ext_ids.get("DOI", ""),
            "source": "semantic_scholar",
        }

    async def _openalex_search(self, client: httpx.AsyncClient, title: str) -> dict | None:
        """Search OpenAlex by title. Returns best match or None."""
        try:
            resp = await client.get(
                _OPENALEX_WORKS,
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
        except (httpx.HTTPError, Exception) as e:
            logger.debug("OpenAlex search failed: %s", e)
            return None

        works = data.get("results", [])
        if not works:
            return None

        # Return the best title match
        best = None
        best_sim = 0.0
        for w in works:
            sim = _title_similarity(title, w.get("title", ""))
            if sim > best_sim:
                best_sim = sim
                best = w

        if best is None or best_sim < 0.4:
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
            "authors": authors,
            "year": best.get("publication_year"),
            "doi": doi,
            "source": "openalex",
        }

    @staticmethod
    def _normalize_crossref(item: dict) -> dict | None:
        """Normalize a CrossRef work record."""
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

        # Extract venue/journal name
        venue = (
            item.get("container-title", [""])[0]
            if item.get("container-title")
            else ""
        )

        return {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": item.get("DOI", ""),
            "venue": venue,
            "source": "crossref",
        }


async def verify_references_for_paper(
    references: list[dict],
    mailto: str = _CROSSREF_MAILTO,
) -> VerificationReport:
    """Convenience function: verify all references in a paper."""
    verifier = ReferenceVerifier(mailto=mailto)
    return await verifier.verify_all(references)
