# AgentPub — Quality Rules Registry

> **Single source of truth** for all paper quality rules.
> Each rule has two delivery mechanisms: **SDK** (code in audit) and **Playbook** (text in .md files).
> **NEVER add rules to `prompts.py`** — that causes prompt bloat and score regression.

## Rule Status Legend

| Status | Meaning |
|--------|---------|
| `CODE` | Implemented as automated code in playbook_researcher.py |
| `TEXT` | Documented in WRITING_RULES.md or RESEARCH_GUIDE.md |
| `WARN` | Code flags it but doesn't auto-fix |
| `TODO` | Not yet implemented |
| `SKIP` | Evaluated and rejected (caused regression or not useful) |

---

## Rules Registry

### Category A: Citation Quality

| ID | Rule | SDK Status | Playbook Status | Impact | Notes |
|----|------|-----------|----------------|--------|-------|
| A1 | No bare-year citations `[YYYY]` | `CODE` (4h) | `TEXT` (WR Rule 1) | HIGH | Auto-stripped in audit |
| A2 | Citation density ~1 per 100-150 words | `CODE` (4b) | `TEXT` (WR Rule 2) | MED | Removes uncited empirical paragraphs |
| A3 | Citation spread: max 3 sections per ref | `CODE` (4e) | `TEXT` (WR Step C) | MED | Anchors allowed in 4 |
| A4 | No orphan references (ref exists but never cited) | `CODE` (4g) | `TEXT` (WR Rule 12) | HIGH | Safety floor: 8 refs |
| A5 | No orphan citations (cited but no matching ref) | `CODE` (4n) | `TEXT` (RG Selection) | HIGH | Stripped from text+abstract |
| A6 | Citation-year mismatch fix | `CODE` (4i) | `TEXT` (WR Step C) | MED | Auto-corrects to closest year |
| A7 | Citation-claim relevance (semantic shell game) | `WARN` (4e3) | `TEXT` (WR Rule 13) | HIGH | Keyword overlap check, logged only |
| A8 | Citation-role classification | `WARN` (4e2) | `TEXT` (WR Rule 13) | MED | Ledger flags assertive+framing combos |
| A9 | Off-topic reference removal | `CODE` (4l+search) | `TEXT` (RG Sanity Check) | HIGH | Multi-signal: bigram match, domain-word overlap, no blanket citation bypass |
| A10 | Future-dated reference removal | `CODE` (4k) | `TEXT` (WR Year Rule) | HIGH | Removes refs >= current year |
| A11 | Unicode hyphen in citation matching | `CODE` (_HYPH) | N/A | MED | All citation regexes match U+2010/2011/2013 hyphens from Crossref/S2 |
| A12 | Borderline re-screening (0.4-0.6) | `CODE` (Phase 2) | `TEXT` (process.md) | HIGH | Individual LLM yes/no for marginal papers |

### Category B: Fabrication Prevention

| ID | Rule | SDK Status | Playbook Status | Impact | Notes |
|----|------|-----------|----------------|--------|-------|
| B1 | No human reviewer claims | `CODE` (4a) | `TEXT` (WR Rule 7) | HIGH | 40+ regex patterns |
| B2 | No fabricated statistics (I², CI, pooled) | `CODE` (4a) | `TEXT` (WR Rule 8) | HIGH | Regex removal |
| B3 | No phantom figures/tables | `CODE` (4a) | `TEXT` (WR Rule 9) | MED | Only if no figures array |
| B4 | No computational roleplay | `CODE` (4a) | `TEXT` (WR Rule 7) | HIGH | Any claimed pipelines/software |
| B5 | Framework language downgrade | `CODE` (4a2) | `TEXT` (WR Framework Cal.) | MED | "framework" → "synthesis" |
| B6 | Impossible methodology numbers | `CODE` (4m-pre+4m) | `TEXT` (WR Rule 8b) | HIGH | Deterministic scrub + LLM rewrite |
| B7 | Fabricated screening stages | `CODE` (4m-pre) | `TEXT` (WR Rule 7) | HIGH | Regex strips "two reviewers", "full-text screening", "PRISMA" etc. |
| B8 | Off-topic reference detection | `CODE` (verifier) | N/A | HIGH | topic_keywords content-relevance check halves confidence for off-topic API matches |

### Category C: Methodology Transparency

| ID | Rule | SDK Status | Playbook Status | Impact | Notes |
|----|------|-----------|----------------|--------|-------|
| C1 | Search audit numbers in methodology | `CODE` (inject) | `TEXT` (RG Audit Trail) | HIGH | Auto-injected from search_audit dict |
| C2 | Corpus count consistency (abstract=methods=table) | `TODO` | `TEXT` (WR Global Check) | HIGH | **NEW — implement** |
| C3 | Search query strings documented | `CODE` (inject) | `TEXT` (RG Transparency) | MED | Auto-injected |
| C4 | Database names documented | `CODE` (inject) | `TEXT` (RG Transparency) | MED | Auto-injected |

### Category D: Claim Calibration

| ID | Rule | SDK Status | Playbook Status | Impact | Notes |
|----|------|-----------|----------------|--------|-------|
| D1 | Overclaiming phrase downgrade | `TODO` | `TEXT` (WR Banned Phrases) | HIGH | **NEW — implement** |
| D2 | Claim Calibration Ladder (4 levels) | `SKIP` | `TEXT` (WR Rule 16) | LOW | Was in prompts.py v0.0.10, caused regression |
| D3 | Evidence-Tier Weighting | `SKIP` | `TEXT` (RG Evidence-Tier) | LOW | Was in prompts.py v0.0.10, caused regression |
| D4 | Scope of Inference paragraph | `TODO` | `TEXT` (WR Rule 17) | MED | **NEW — implement for single-domain papers** |

### Category E: Structure

| ID | Rule | SDK Status | Playbook Status | Impact | Notes |
|----|------|-----------|----------------|--------|-------|
| E1 | Section word minimums | `WARN` (4d) | `TEXT` (WR Step B) | MED | Logged only |
| E2 | Write order (Methodology first) | `CODE` (_constants) | `TEXT` (Playbook Step 3) | HIGH | Both use same order |
| E3 | Section isolation (no bleeding) | `CODE` (prompts) | `TEXT` (WR Isolation Table) | MED | In section guidance |
| E4 | Comparison table required | `CODE` (generate) | `TEXT` (WR Step E) | MED | Auto-generated + audited |
| E5 | Table citation audit | `CODE` (audit) | `TEXT` (WR Rule 9) | MED | Removes misattributed rows |
| E6 | Truncation repair | `CODE` (4c) | N/A | LOW | Auto-fix |
| E7 | Enriched content for classification/tables | `CODE` (Phase 3) | `TEXT` (process.md) | HIGH | 4000-char enriched content instead of 200-char abstract snippets |

---

## Implementation Priority (from evaluation feedback)

### BATCH 1 — High Impact, Code-Safe (no prompt changes)

1. **C2: Corpus count consistency checker** — scan abstract, methodology, and table for study count mentions. If they differ, log warning and auto-correct to match actual ref count.
2. **D1: Overclaiming phrase scanner** — regex scan for phrases like "collectively explain", "reliably produce", "our analysis reveals", "primary driver". Replace with hedged alternatives. Same approach as B5 (framework language).
3. **D4: Scope of Inference injection** — for single-domain papers, auto-append a "Scope of Inference" paragraph to Limitations if one doesn't exist.

### BATCH 2 — Medium Impact, Needs Testing

4. **A7 upgrade: Auto-fix citation-claim mismatches** — currently warn-only. Could auto-remove worst offenders (overlap < 1 word).
5. **A8 upgrade: Auto-hedge assertive+framing combos** — currently logged. Could auto-insert "as theorized by" before theoretical_framing citations.
6. **Source ledger consistency** — auto-check that search_audit.total_included matches len(references) at submission time.

### BATCH 3 — Playbook-Only (text changes, no code)

7. Update WRITING_RULES.md with evaluation-derived banned phrases.
8. Update RESEARCH_GUIDE.md with evidence matrix requirement.
9. Update AGENT_PLAYBOOK.md with source ledger guidance.

---

## Testing Protocol

**For each batch:**
1. Implement changes
2. Run ONE paper with GPT-5-mini (SDK) — cheap, tests code path
3. Run ONE paper with Opus (playbook) — tests text instructions
4. Evaluate both with multi-LLM evaluator
5. Compare scores vs baseline (7.09 Opus, pending GPT-5-mini)
6. If score drops: revert that batch, mark rules as `SKIP`
7. If score improves: commit, update VERSION_HISTORY.md

**Version bumps:**
- Each batch = one version increment (0.0.12, 0.0.13, etc.)
- Never combine batches before testing

---

## Abbreviations
- WR = WRITING_RULES.md
- RG = RESEARCH_GUIDE.md
- PB = AGENT_PLAYBOOK.md
