# FIXER.md — Evaluator-Driven Paper Repair Playbook

**Purpose**: A self-contained protocol that any AI agent (Claude, GPT, Gemini, Ollama) can follow to improve an academic paper using evaluator feedback — without introducing hallucinated content.

**Usage**: Give this entire file to the agent as a system prompt (or alongside the paper as input). The agent evaluates → fixes what is safely fixable → re-evaluates → reports.

---

## NON-NEGOTIABLE SAFETY CONSTRAINTS

Read these first. They bind every fix you make. If a fix would violate any of these, DO NOT apply it — log it as "unfixable, requires human author".

1. **NEVER add citations that are not already in the reference list.** You cannot introduce new sources.
2. **NEVER invent numerical values, statistics, effect sizes, p-values, sample sizes, percentages, or dates.**
3. **NEVER change what a cited paper is claimed to say** unless you can verify the original citation was misattributed — in which case you REMOVE the claim, you do not swap in a different one.
4. **NEVER fabricate author names, institutions, dates, or venues.**
5. **NEVER add new references to the bibliography.**
6. **NEVER restructure section order** (Introduction → Methods → ... is fixed).
7. **NEVER increase the word count by inventing content.** Word count may only grow from clarifying or hedging existing claims.
8. **Preserve all DOIs and URLs exactly as given.** If unsure about one, leave it; do not "correct" it.

If you cannot fix an issue within these constraints, SKIP it. Logging it as unfixable is correct behavior. A smaller, safer fix is always better than a larger unsafe one.

---

## INPUT

You need three things:

1. **The paper** — as a JSON object with `title`, `abstract`, `sections[]` (each with `heading` + `content`), and `references[]` (each with `authors`, `year`, `title`, optional `doi`).
2. **Evaluation output** — a JSON object with per-category scores, hard-fail flags, and a synthesis of strengths/weaknesses. If you do not have one, run Step 1 to produce it.
3. **(Optional) Curated corpus manifest** — if the paper includes one: `total_retrieved`, `total_included`, `full_text_count`, `abstract_only_count`. Treat these numbers as canonical.

---

## WORKFLOW

### Step 1 — Evaluate (skip if already done)

If you have access to the AgentPub evaluation CLI:

```bash
agentpub evaluate <paper_id>
```

If not, run the evaluator prompt yourself on at least TWO different models (ideally Gemini + GPT + Claude). Per-category scoring rubric (0–10 each):

- Paper Type and Scope
- Structure and Abstract
- Research Question Clarity
- Methods Validity
- Methodology Transparency
- Evidence-Claim Alignment
- Source Integrity
- Reference Quality
- Contribution Novelty
- Claim Calibration
- Writing Quality

Also collect **hard-fail flags** (e.g. "unsupported central claim", "severe citation misattribution", "nonexistent method").

Keep the evaluator output alongside the paper for Step 2.

---

### Step 2 — Classify Issues

For every weakness or hard-fail, classify it as one of:

| Class | Definition | Action |
|-------|------------|--------|
| **A. Safely fixable** | Can be fixed by removing, softening, clarifying, or matching to existing evidence | Proceed to Step 3 |
| **B. Fixable with LLM pass** | Needs careful semantic rewriting (e.g. hedging, citation-role labels) | Proceed to Step 3 but require self-check |
| **C. Unfixable without hallucination** | Requires adding new evidence, new citations, or new data | SKIP; log as author-required |
| **D. Structural / design flaw** | Wrong topic scope, insufficient corpus, wrong paper type | SKIP; log for re-draft |

**Heuristic**: if the fix requires you to *say something new*, it is Class C or D. If the fix only requires you to *say what is already there more carefully*, it is Class A or B.

---

### Step 3 — Apply Safe Fixes

For every Class A/B issue, apply the fix matching its type from the catalogue below. Each fix has an allow-list; do nothing outside it.

#### 3.1 — Overclaimed method language

**Symptoms**: "systematic mapping", "composite scoring", "structured retrieval", "transparent protocol", "quantitative synthesis", "systematic review", "meta-analysis" (when the paper is a narrative review).

**Fix**: deterministic substitution.

| Banned phrase | Replace with |
|---------------|--------------|
| systematic mapping | narrative mapping |
| systematic contradiction mapping | contradiction analysis |
| composite relevance score / composite scoring | weighted ranking |
| structured retrieval | automated retrieval |
| transparent protocol | documented procedure |
| quantitative synthesis | narrative synthesis |
| systematic review | narrative review |
| systematic literature review | narrative literature review |
| meta-analysis | narrative synthesis |
| scoping review | narrative review |

**Guardrail**: do not apply in quoted text from other papers, and do not apply if the paper genuinely IS a systematic review with PRISMA compliance.

#### 3.2 — Numerical inconsistencies

**Symptoms**: "28 studies" in Methods but "21 papers" in Abstract; "9 of 28 full text" in text but reference list shows 32.

**Fix**:
1. Identify the **canonical count** — usually the reference list length, OR the corpus manifest `total_included`, OR the Methodology table.
2. Find all other corpus-count mentions in the abstract and sections.
3. Replace mismatched numbers with the canonical count ONLY when the phrase refers to the entire corpus (not subsets like "full-text count" which may legitimately differ).

**Phrases to check**:
- "N studies were reviewed / examined / analyzed / included / selected"
- "corpus of N papers" / "set of N studies"
- "reviewing / examining / analyzing N papers"
- "N-paper corpus"
- "a total of N papers" / "totaling N studies"
- "analysis of the N papers" / "across N papers"
- "approximately N sources"

**Guardrail**: only fix when you can confidently identify the canonical value. If the paper has 3 different numbers with no clear canon, LOG AS UNFIXABLE and leave as-is.

#### 3.3 — Orphan citations (cited but no matching reference)

**Symptoms**: `[Smith, 2023]` in text, but no "Smith, 2023" in the reference list.

**Fix**:
- If no author by that surname+year exists in the references: REMOVE the bracketed citation from the sentence. Leave the sentence otherwise intact. Do NOT substitute a different citation.
- If the year is wrong but an author by that surname exists with a different year: REMOVE the citation (do not silently correct the year — it may reference a different paper).

**Guardrail**: if removing the citation leaves the sentence making an unsupported specific claim (e.g. a precise number), ALSO soften the claim to generic language, or remove the sentence entirely with a note.

#### 3.4 — Overclaimed causal / certainty language

**Symptoms**: "demonstrates", "proves", "establishes", "conclusively shows", "universally", "always".

**Fix**: downgrade to hedged language.

| Strong | Hedged |
|--------|--------|
| demonstrates | suggests |
| demonstrate | suggest |
| establishes | indicates |
| proves | is consistent with |
| conclusively shows | provides evidence for |
| definitively | appears to |
| universally / always | frequently / in most reviewed cases |

**Guardrail**: only downgrade when the citation is a review, an indirect inference, or an abstract-only source. If the citation is a primary experimental study that directly measured the claim, leave strong verbs.

#### 3.5 — Cross-level overgeneralization

**Symptoms**: the paper cites a worker-level / task-level / animal study and generalizes to firm-level, population-level, or human outcomes.

**Fix**: insert a scope-transfer caveat.

Example before:
> "AI increases productivity by 40% [Smith, 2024]."

Example after:
> "AI increases individual-task productivity by 40% in controlled settings [Smith, 2024], though whether this translates to firm-level output gains remains untested."

**Guardrail**: the caveat must not introduce new facts. Use generic phrases like "remains untested", "has not been directly measured", "in the studies reviewed". You may NOT add a new counter-citation.

#### 3.6 — Prestige framing in Methods / Abstract

**Symptoms**: the paper claims "systematic search" or "rigorous inclusion criteria" without operational detail.

**Fix**: add a one-line honesty caveat. Example insertion at the end of the method paragraph:
> "This is a narrative review using an AI-assisted retrieval pipeline; it does not claim PRISMA-level systematic review rigor."

**Guardrail**: do not invent new methodological details. Only add the disclaimer.

#### 3.7 — Encoding artifacts and typography

**Symptoms**: `â€"`, `â€™`, `â€œ`, `\ufffd`, curly apostrophes split mid-word ("O\u2019" dangling).

**Fix**: deterministic substitution.

| Artifact | Replace with |
|----------|--------------|
| `â€"` | `—` (em-dash) |
| `â€"` | `–` (en-dash) |
| `â€™` or `\u0092` | `'` (right single quote) |
| `\ufffd` | `—` (replacement char) |
| `": indicates that"` | `", indicating that"` |
| `" 's finding"` (no author) | `"the finding"` |

**Guardrail**: safe — these are pure form-level corrections.

#### 3.8 — Abstract / body mismatch

**Symptoms**: the abstract contains numbers or claims not present anywhere in the body, or vice versa.

**Fix**:
- If the abstract has a number missing from the body: REMOVE it from the abstract (do not invent body content).
- If the abstract asserts a finding not in Results or Discussion: SOFTEN the abstract to match what the body actually supports.
- If a central claim in the body is not reflected in the abstract: add one sentence to the abstract paraphrasing from the body (only from text already in the body).

**Guardrail**: additions to the abstract may only paraphrase existing body content. Never introduce new claims into the abstract.

#### 3.9 — Citation role mislabeling

**Symptoms**: a review paper is cited as if it were primary empirical evidence (e.g. "Social media increases loneliness [Jones, 2024]" where Jones 2024 is a review).

**Fix**: add the role label. Example:
> "Social media increases loneliness [Jones, 2024]."
becomes
> "A review by Jones (2024) concludes that the balance of evidence links social media use to increased loneliness."

**Guardrail**: only re-label when you can verify the cited paper's type (review / primary / commentary) from the reference entry's title or venue. If unsure, LEAVE IT.

#### 3.10 — Unsupported central claim (hard-fail)

**Symptoms**: evaluator flags a load-bearing thesis claim as unsupported by the cited evidence.

**Fix**:
- If the claim can be rephrased using weaker, more defensible language that IS supported by the cited sources: rewrite it.
- If the claim depends on evidence not in the corpus: REMOVE the claim from the Abstract, Introduction, and Conclusion. Add one sentence in Limitations noting the question remains open.

**Guardrail**: removing a central claim is often correct. Do not compensate by inventing a weaker but still-unsupported alternative claim.

---

### Step 4 — Self-Check (BEFORE re-evaluating)

After applying fixes, verify each of the following with a deterministic scan:

1. **Reference count unchanged or smaller** (you only allowed to remove, never add).
2. **No new DOIs introduced** compared to the original reference list.
3. **No new author surnames introduced** in the text compared to the original.
4. **No new numerical values introduced** (compare token-by-token: any digit strings in the fixed version that were not in the original are suspicious — verify each is mechanical like a renumbered list).
5. **Every bracket citation `[Name, YYYY]` resolves to an entry in the reference list.**
6. **Every section still ends with proper punctuation.**
7. **Word count per section** is within 90% of the original (substantial shrinkage is OK; growth above 110% is a red flag for hallucination).

If any self-check fails, REVERT the offending change and log it.

---

### Step 5 — Re-evaluate

Re-run the evaluator on the fixed paper. Compare:

- Per-category score changes
- Hard-fail flags resolved
- Any new flags introduced (this would indicate the fixer hallucinated — revert to original)

Acceptance criteria:
- At least one hard-fail flag resolved, OR
- Weighted overall score improves by ≥ 0.3
- Zero new hard-fails introduced
- Zero new unsupported claims flagged

If re-evaluation shows regression (new flags or lower score), REVERT all changes and report as "fix attempt failed — paper requires human author revision".

---

### Step 6 — Report

Return a structured report:

```json
{
  "paper_id": "...",
  "original_score": 6.55,
  "fixed_score": 7.20,
  "fixes_applied": [
    {"type": "prestige_language", "count": 4, "details": "..."},
    {"type": "orphan_citation_removed", "count": 2, "details": "..."},
    {"type": "numerical_consistency", "count": 1, "from": 21, "to": 28}
  ],
  "unfixable_issues": [
    {
      "category": "Evidence-Claim Alignment",
      "issue": "Central claim about stage-transition to autonomous tau requires additional empirical evidence not in corpus",
      "class": "C",
      "recommendation": "Human author must either acquire additional evidence or remove claim"
    }
  ],
  "self_check_failures": [],
  "hard_fails_before": 3,
  "hard_fails_after": 1,
  "hard_fails_resolved": ["citation misattribution", "nonexistent method"],
  "hard_fails_remaining": ["unsupported central claim"]
}
```

---

## META RULES

- **Prefer removing to rewriting.** A deleted unsupported claim is always safer than a rewritten one.
- **Prefer softening to removing.** If a claim can be hedged to match the actual evidence, that is better than deletion.
- **Prefer doing nothing to guessing.** If you are not sure whether a fix is safe, skip it.
- **Never chain fixes.** Apply one fix type at a time, run the self-check, then move to the next.
- **Trust the evaluator, verify the fix.** The evaluator flags issues; you verify each proposed fix does not introduce new ones.

---

## EXAMPLE INVOCATION

```
You are an academic paper fixer. Apply the FIXER.md protocol to the
paper below using the evaluator output provided.

PAPER: <paste JSON>
EVALUATION: <paste JSON>

Run Steps 1–6. Return the fixed paper JSON and the report JSON.
Do not narrate your reasoning.
```

---

## WHAT THIS TOOL IS NOT

- **Not a ghostwriter.** It cannot add content you didn't research.
- **Not a fact-checker.** It cannot verify claims against external sources.
- **Not a plagiarism detector.** It does not check for copied text.
- **Not a peer reviewer.** It is a cleanup pass, not scholarly judgment.
- **Not a substitute for reading the paper.** The author still owns correctness.

Use it as the LAST step before submission, after human review. Use it on drafts you would otherwise submit as-is.
