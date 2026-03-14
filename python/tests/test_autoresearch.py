"""Tests for the autoresearch quality evaluation and optimization system."""
import pytest

from agentpub.autoresearch import (
    PaperEvaluator,
    PromptOptimizer,
    fix_journal_metadata,
    strip_markdown,
    normalize_citations,
    apply_code_fixes,
)


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def good_paper():
    """A minimal paper that should score well."""
    return {
        "abstract": "This survey examines X [Smith & Jones, 2023]. We found Y [Lee et al., 2022].",
        "sections": [
            {"heading": "Introduction", "content": (
                "The field of X has grown substantially [Smith & Jones, 2023]. "
                "Recent studies demonstrate significant impact on outcomes. "
                "Dysbiosis has been linked to IBD in 73% of cases [Lee et al., 2022]. "
                "The relationship between A and B is complex and multi-faceted. "
                "Prior work has established foundational concepts [Brown, 2021]. "
            ) * 5},
            {"heading": "Related Work", "content": (
                "Smith and Jones (2023) provided the seminal framework [Smith & Jones, 2023]. "
                "Building on this, Lee et al. explored broader applications [Lee et al., 2022]. "
                "Brown contributed methodological innovations [Brown, 2021]. "
                "The intersection of these approaches reveals new directions. "
            ) * 6},
            {"heading": "Methodology", "content": (
                "This survey employed automated retrieval from CrossRef and Semantic Scholar. "
                "Search terms included relevant keywords. Papers were filtered by relevance. "
                "The analysis covered publications from 2018 to 2024. "
            ) * 5},
            {"heading": "Results", "content": (
                "Of the 20 studies reviewed, 15 (75%) reported positive outcomes. "
                "Effect sizes ranged from d=0.3 to d=0.8 across 12 studies. "
                "N=450 participants were included in the meta-analysis. "
                "The mean improvement was 23.5% (p<0.01) [Smith & Jones, 2023]. "
                "Collectively, the evidence shows consistent patterns [Lee et al., 2022]. "
            ) * 4},
            {"heading": "Discussion", "content": (
                "These findings have important implications for clinical practice. "
                "The therapeutic potential extends beyond initial applications [Brown, 2021]. "
                "Future research should address the identified gaps. "
                "Integration of multi-omics approaches is recommended. "
            ) * 5},
            {"heading": "Limitations", "content": (
                "This study has several limitations. The reliance on observational data limits causal inference. "
                "Selection bias may affect generalizability. Sample sizes varied across studies. "
            ) * 3},
            {"heading": "Conclusion", "content": (
                "In conclusion, this survey demonstrates the importance of X in Y. "
                "The findings support continued research in this domain [Smith & Jones, 2023]. "
            ) * 4},
        ],
        "references": [
            {"title": "Framework for X", "authors": ["John Smith", "Alice Jones"], "year": 2023, "doi": "10.1234/a", "journal": "Nature"},
            {"title": "Broader applications of X", "authors": ["Wei Lee", "Bob Chen", "Carl Diaz"], "year": 2022, "doi": "10.1234/b", "journal": "Science"},
            {"title": "Methods in X", "authors": ["Tom Brown"], "year": 2021, "doi": "10.1234/c", "journal": "PNAS"},
        ],
    }


@pytest.fixture
def bad_paper():
    """A paper with many quality issues."""
    return {
        "abstract": "This paper discusses things.",
        "sections": [
            {"heading": "Introduction", "content": (
                "The gut microbiome plays a crucial role in health. "
                "Dysbiosis has been implicated in IBD, obesity, and diabetes. "
                "**Important**: this finding is significant. "
            )},
            {"heading": "Results", "content": (
                "### 1. Gut Microbiome\n"
                "Studies suggest that the microbiome is important. "
                "Research indicates various effects on health. "
                "The gut microbiome plays a crucial role in health. "
                "Dysbiosis has been implicated in IBD, obesity, and diabetes. "
            )},
            {"heading": "Discussion", "content": "Short."},
        ],
        "references": [
            {"title": "Paper A", "authors": ["John Smith"], "year": 2023, "journal": "crossref"},
            {"title": "Paper B", "authors": ["Jane Doe"], "year": 2022, "journal": "semantic_scholar"},
            {"title": "Paper C", "authors": ["Bob Lee"], "year": 2021},
        ],
    }


# -- PaperEvaluator tests ---------------------------------------------------

class TestPaperEvaluator:
    def test_evaluate_returns_report(self, good_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(good_paper)
        assert report.composite_score >= 0
        assert report.composite_score <= 100
        assert len(report.metrics) == 10

    def test_good_paper_scores_higher(self, good_paper, bad_paper):
        evaluator = PaperEvaluator()
        good_report = evaluator.evaluate(good_paper)
        bad_report = evaluator.evaluate(bad_paper)
        assert good_report.composite_score > bad_report.composite_score

    def test_journal_metadata_detects_blocklist(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        journal_metric = next(m for m in report.metrics if m.name == "journal_metadata")
        assert journal_metric.score < 100
        assert journal_metric.details["bad_journals"] >= 2

    def test_markdown_leakage_detects_formatting(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        md_metric = next(m for m in report.metrics if m.name == "markdown_leakage")
        assert md_metric.score < 100
        assert md_metric.details["leak_count"] > 0

    def test_orphan_refs_all_cited(self, good_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(good_paper)
        orphan_metric = next(m for m in report.metrics if m.name == "orphan_references")
        assert orphan_metric.score >= 80

    def test_quantitative_density_good_results(self, good_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(good_paper)
        quant_metric = next(m for m in report.metrics if m.name == "quantitative_density")
        assert quant_metric.score >= 60

    def test_quantitative_density_bad_results(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        quant_metric = next(m for m in report.metrics if m.name == "quantitative_density")
        assert quant_metric.score < 60

    def test_paraphrase_repetition_catches_dupes(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        rep_metric = next(m for m in report.metrics if m.name == "paraphrase_repetition")
        # Bad paper repeats "gut microbiome plays a crucial role" and "dysbiosis" across sections
        assert rep_metric.details["duplicate_pairs"] >= 1

    def test_summary_format(self, good_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(good_paper)
        summary = report.summary()
        assert "Iteration" in summary
        assert "Composite" in summary


# -- PromptOptimizer tests --------------------------------------------------

class TestPromptOptimizer:
    def test_plan_for_failing_paper(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        optimizer = PromptOptimizer()
        plan = optimizer.plan(report)
        assert plan.weakness_summary  # should have guidance
        assert len(plan.priority_metrics) > 0

    def test_plan_for_passing_paper(self, good_paper):
        evaluator = PaperEvaluator(pass_threshold=30.0, metric_threshold=20.0)
        report = evaluator.evaluate(good_paper)
        optimizer = PromptOptimizer()
        plan = optimizer.plan(report)
        # If all metrics pass, no weakness summary needed
        if not report.worst_metrics:
            assert plan.weakness_summary == ""

    def test_code_fixes_identified(self, bad_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(bad_paper)
        optimizer = PromptOptimizer()
        plan = optimizer.plan(report)
        assert "fix_journal_metadata" in plan.code_fixes
        # markdown_leakage may or may not fail depending on threshold
        # but journal fix should always be identified for bad_paper


# -- Code fix tests ----------------------------------------------------------

class TestCodeFixes:
    def test_fix_journal_metadata(self):
        paper = {
            "references": [
                {"title": "A", "journal": "crossref"},
                {"title": "B", "journal": "Nature"},
                {"title": "C", "journal": "semantic_scholar"},
            ]
        }
        fixed = fix_journal_metadata(paper)
        journals = [r.get("journal") for r in fixed["references"]]
        assert journals == [None, "Nature", None]

    def test_strip_markdown(self):
        paper = {
            "sections": [
                {"heading": "Intro", "content": "This is **bold** and *italic* text.\n### Sub-header\nMore text."},
            ]
        }
        fixed = strip_markdown(paper)
        content = fixed["sections"][0]["content"]
        assert "**" not in content
        assert "###" not in content
        assert "bold" in content

    def test_normalize_citations_two_authors(self):
        paper = {
            "abstract": "",
            "sections": [
                {"heading": "Intro", "content": "This was found [Smith et al., 2023] and confirmed [Smith & Jones, 2023]."},
            ],
            "references": [
                {"title": "A", "authors": ["John Smith", "Alice Jones"], "year": 2023},
            ],
        }
        fixed = normalize_citations(paper)
        content = fixed["sections"][0]["content"]
        # Both should now be [Smith & Jones, 2023]
        assert content.count("[Smith & Jones, 2023]") == 2
        assert "et al." not in content

    def test_apply_code_fixes(self):
        paper = {
            "sections": [{"heading": "Intro", "content": "**bold** text"}],
            "references": [{"title": "A", "journal": "crossref"}],
        }
        fixed = apply_code_fixes(paper, ["fix_journal_metadata", "strip_markdown"])
        assert "**" not in fixed["sections"][0]["content"]
        assert "journal" not in fixed["references"][0]


# -- Evaluate the actual microbiome paper ------------------------------------

class TestRealPaper:
    """Run evaluator against the actually generated paper (if it exists)."""

    @pytest.fixture
    def real_paper(self):
        import pathlib, json
        path = pathlib.Path.home() / ".agentpub" / "papers" / "The Role Of The Human Microbiome In Health And Dis.json"
        if not path.exists():
            pytest.skip("Real paper not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_real_paper_evaluation(self, real_paper):
        evaluator = PaperEvaluator()
        report = evaluator.evaluate(real_paper)
        print("\n" + report.summary())
        # Just verify it runs — scores are informational
        assert report.composite_score >= 0
        assert len(report.metrics) == 10

    def test_real_paper_after_fixes(self, real_paper):
        """Apply all code fixes and see improvement."""
        evaluator = PaperEvaluator()
        before = evaluator.evaluate(real_paper)
        fixed = apply_code_fixes(real_paper, [
            "fix_journal_metadata", "strip_markdown", "normalize_citations",
        ])
        after = evaluator.evaluate(fixed)
        print(f"\nBefore fixes: {before.composite_score:.1f}")
        print(f"After fixes:  {after.composite_score:.1f}")
        assert after.composite_score >= before.composite_score
