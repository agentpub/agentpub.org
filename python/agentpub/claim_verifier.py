"""Claim decomposition and evidence grounding verification.

Inspired by Microsoft's Claimify paper. After a full draft is written,
decomposes it into atomic claims and verifies each is grounded in the
evidence (references + evidence map).

Usage:
    verifier = ClaimVerifier(llm_backend)
    claims = verifier.decompose_section(section_text)
    results = verifier.verify_claims(claims, evidence_map, verified_refs)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("agentpub.claim_verifier")


@dataclass
class Claim:
    """An atomic factual claim extracted from a paper section."""
    text: str
    cited_source: str  # cite_key or "none"
    claim_type: str    # empirical, methodological, definitional, opinion
    verifiable: bool
    section: str = ""


@dataclass
class ClaimResult:
    """Result of verifying a single claim."""
    claim: Claim
    status: str  # "supported", "unsupported", "uncertain"
    reason: str = ""


@dataclass
class ClaimVerificationReport:
    """Summary of claim verification for a paper."""
    total_claims: int = 0
    claims_supported: int = 0
    claims_unsupported: int = 0
    claims_uncertain: int = 0
    results: list[ClaimResult] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)

    @property
    def unsupported_ratio(self) -> float:
        """Fraction of empirical claims that are unsupported."""
        empirical = sum(
            1 for r in self.results
            if r.claim.claim_type == "empirical"
        )
        if empirical == 0:
            return 0.0
        unsupported_empirical = sum(
            1 for r in self.results
            if r.claim.claim_type == "empirical" and r.status == "unsupported"
        )
        return unsupported_empirical / empirical


# Max unsupported empirical claims before flagging
MAX_UNSUPPORTED_RATIO = 0.10


class ClaimVerifier:
    """Decompose paper sections into claims and verify grounding."""

    def __init__(self, llm_backend):
        """
        Args:
            llm_backend: An LLMBackend instance for claim decomposition.
        """
        self.llm = llm_backend

    def decompose_all_sections(self, sections: dict[str, str]) -> list[Claim]:
        """F4: Extract claims from ALL sections in a single LLM call.

        Sends all sections (truncated to 2K each) in one prompt and returns
        claims tagged by section name. Saves ~6 LLM calls vs per-section.
        """
        # Build combined input
        section_blocks = []
        for name, text in sections.items():
            if not text or len(text.split()) < 20:
                continue
            section_blocks.append(f"=== SECTION: {name} ===\n{text[:2000]}")

        if not section_blocks:
            return []

        combined = "\n\n".join(section_blocks)

        system = (
            "You are a claim extraction tool for academic papers. "
            "Extract every factual claim from each section. Be thorough but precise."
        )

        prompt = f"""Extract every factual claim from each section below.

For each claim, output a JSON object with:
- "section": the section name (exactly as shown after "SECTION:")
- "claim": the atomic statement (one fact per claim)
- "cited_source": which reference cite_key supports it (e.g. "[Smith, 2023]") or "none" if no citation
- "claim_type": one of "empirical" (data/finding), "methodological" (about methods), "definitional" (definition/concept), "opinion" (author's view/speculation)
- "verifiable": true if this can be checked against literature, false if it's subjective

Return JSON: {{"claims": [list of claim objects]}}

{combined}"""

        try:
            result = self.llm.generate_json(system, prompt, temperature=0.1)
        except Exception as e:
            logger.warning("Batch claim decomposition failed: %s — falling back to per-section", e)
            # Fallback to per-section
            all_claims: list[Claim] = []
            for name, text in sections.items():
                all_claims.extend(self.decompose_section(name, text))
            return all_claims

        claims = []
        for item in result.get("claims", []):
            if not isinstance(item, dict):
                continue
            claims.append(Claim(
                text=item.get("claim", ""),
                cited_source=item.get("cited_source", "none"),
                claim_type=item.get("claim_type", "empirical"),
                verifiable=item.get("verifiable", True),
                section=item.get("section", ""),
            ))

        return claims

    def decompose_section(self, section_name: str, section_text: str) -> list[Claim]:
        """Extract atomic factual claims from a section using the LLM."""
        if not section_text or len(section_text.split()) < 20:
            return []

        system = (
            "You are a claim extraction tool for academic papers. "
            "Extract every factual claim from the text. Be thorough but precise."
        )

        prompt = f"""Extract every factual claim from this '{section_name}' section text.

For each claim, output a JSON object with:
- "claim": the atomic statement (one fact per claim)
- "cited_source": which reference cite_key supports it (e.g. "[Smith, 2023]") or "none" if no citation
- "claim_type": one of "empirical" (data/finding), "methodological" (about methods), "definitional" (definition/concept), "opinion" (author's view/speculation)
- "verifiable": true if this can be checked against literature, false if it's subjective

Return JSON: {{"claims": [list of claim objects]}}

Text:
{section_text[:6000]}"""

        try:
            result = self.llm.generate_json(system, prompt, temperature=0.1)
        except Exception as e:
            logger.warning("Claim decomposition failed for %s: %s", section_name, e)
            return []

        claims = []
        for item in result.get("claims", []):
            if not isinstance(item, dict):
                continue
            claims.append(Claim(
                text=item.get("claim", ""),
                cited_source=item.get("cited_source", "none"),
                claim_type=item.get("claim_type", "empirical"),
                verifiable=item.get("verifiable", True),
                section=section_name,
            ))

        return claims

    def verify_claims(
        self,
        claims: list[Claim],
        verified_ref_ids: set[str],
        ref_cite_keys: set[str],
    ) -> ClaimVerificationReport:
        """Check if claims are grounded in evidence (rule-based, no extra LLM call).

        Args:
            claims: List of Claim objects from decompose_section.
            verified_ref_ids: Set of ref_ids that passed reference verification.
            ref_cite_keys: Set of valid cite_keys from the reference list.

        Returns:
            ClaimVerificationReport with per-claim results.
        """
        report = ClaimVerificationReport()
        report.total_claims = len(claims)

        for claim in claims:
            result = self._verify_single_claim(claim, verified_ref_ids, ref_cite_keys)
            report.results.append(result)

            if result.status == "supported":
                report.claims_supported += 1
            elif result.status == "unsupported":
                report.claims_unsupported += 1
                report.unsupported_claims.append(claim.text)
            else:
                report.claims_uncertain += 1

        return report

    def _verify_single_claim(
        self,
        claim: Claim,
        verified_ref_ids: set[str],
        ref_cite_keys: set[str],
    ) -> ClaimResult:
        """Verify a single claim against the evidence base."""

        # Definitional and opinion claims don't need citations
        if claim.claim_type in ("definitional", "opinion"):
            return ClaimResult(
                claim=claim,
                status="supported",
                reason=f"{claim.claim_type} claim — citation not required",
            )

        # Non-verifiable claims get a pass
        if not claim.verifiable:
            return ClaimResult(
                claim=claim,
                status="supported",
                reason="Non-verifiable claim — acceptable",
            )

        # Methodological claims are acceptable without citation if they describe
        # the paper's own methodology
        if claim.claim_type == "methodological" and claim.cited_source == "none":
            return ClaimResult(
                claim=claim,
                status="supported",
                reason="Methodological claim about own work — no citation needed",
            )

        # Empirical claims need a citation
        if claim.cited_source == "none" or not claim.cited_source:
            return ClaimResult(
                claim=claim,
                status="unsupported",
                reason="Empirical claim with no citation",
            )

        # Check if the cited source is in the reference list
        cite_key = claim.cited_source.strip()
        if cite_key in ref_cite_keys:
            return ClaimResult(
                claim=claim,
                status="supported",
                reason=f"Cited source {cite_key} found in reference list",
            )

        # Check with normalized matching (fuzzy cite_key match)
        cite_normalized = re.sub(r"[\[\]]", "", cite_key).lower().strip()
        for valid_key in ref_cite_keys:
            valid_normalized = re.sub(r"[\[\]]", "", valid_key).lower().strip()
            if cite_normalized == valid_normalized:
                return ClaimResult(
                    claim=claim,
                    status="supported",
                    reason=f"Cited source matches {valid_key} (normalized)",
                )

        # Citation present but not in the valid reference list
        return ClaimResult(
            claim=claim,
            status="uncertain",
            reason=f"Citation {cite_key} not found in verified reference list",
        )

    def decompose_and_verify_paper(
        self,
        sections: dict[str, str],
        verified_ref_ids: set[str],
        ref_cite_keys: set[str],
    ) -> ClaimVerificationReport:
        """Decompose all sections and verify claims. One-call convenience method."""
        all_claims: list[Claim] = []
        for section_name, section_text in sections.items():
            claims = self.decompose_section(section_name, section_text)
            all_claims.extend(claims)
            logger.info(
                "Section '%s': %d claims extracted", section_name, len(claims)
            )

        report = self.verify_claims(all_claims, verified_ref_ids, ref_cite_keys)
        logger.info(
            "Claim verification: %d total, %d supported, %d unsupported, %d uncertain "
            "(unsupported ratio: %.1f%%)",
            report.total_claims,
            report.claims_supported,
            report.claims_unsupported,
            report.claims_uncertain,
            report.unsupported_ratio * 100,
        )
        return report
