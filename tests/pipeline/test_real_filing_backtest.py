"""End-to-end backtest of the extraction + auto-assumption pipeline against
real, messy filing text — not the synthetic reportlab-built PDF the rest of
tests/pipeline/test_pipeline.py uses.

Every other regression test in this package proves a specific, isolated fix
works against a *curated snippet* reproducing one real bug. This file is
different in kind: it runs the *whole* pipeline (PDFExtractor -> AutoAssumer
-> AnalysisRunner) against real filing excerpts and checks the results are
plausible, the way the accuracy-improvement research session that produced
several of those fixes actually validated them — by running against real
documents for well-known companies where the right order of magnitude is
independently knowable, not just clean synthetic examples.

Fixtures (tests/fixtures/):
  - bls_international_q1_fy2026-27_raw_text.txt — the FULL raw text of a
    real BLS International Services quarterly results announcement (a
    board-resolution + P&L filing, no balance-sheet section at all — the
    filing type that originally produced a $54B nonsense DCF headline).
  - tesla_10k_fy2025_raw_text_excerpts.txt — five real excerpts (cover page,
    MD&A summary, income statement, revenue-by-category, debt schedule)
    concatenated from Tesla's actual FY2025 10-K; not the full ~400KB
    document, but every excerpt is copied verbatim, not paraphrased.

Run: pytest tests/pipeline/test_real_filing_backtest.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline import AnalysisRunner, AutoAssumer, PDFExtractor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def bls_text() -> str:
    return (FIXTURES / "bls_international_q1_fy2026-27_raw_text.txt").read_text()


@pytest.fixture(scope="module")
def tesla_text() -> str:
    return (FIXTURES / "tesla_10k_fy2025_raw_text_excerpts.txt").read_text()


# --------------------------------------------------------------------------- #
# BLS International — quarterly results announcement, INR, no balance sheet
# --------------------------------------------------------------------------- #
def test_bls_company_identity_and_currency(bls_text):
    data = PDFExtractor().scrape_figures(bls_text)
    assert data.company_name and "BLS International" in data.company_name
    assert data.ticker == "BLS"
    assert data.currency == "INR"


def test_bls_revenue_and_net_income_are_the_real_consolidated_figures(bls_text):
    """The headline regression this whole accuracy pass started from: these
    two used to silently resolve to a wrong-but-plausible number lifted
    from an auditor footnote about unreviewed subsidiaries."""
    data = PDFExtractor().scrape_figures(bls_text)
    # Income from operations 89,052.66 lakhs
    assert data.revenue == pytest.approx(89_052.66 * 1e5, rel=0.01)
    # Net Profit for the period/year 20,162.22 lakhs
    assert data.net_income == pytest.approx(20_162.22 * 1e5, rel=0.01)


def test_bls_yoy_growth_margin_tax_derived_not_generic(bls_text):
    data = PDFExtractor().scrape_figures(bls_text)
    assert data.revenue_growth == pytest.approx(0.2533, rel=0.02)
    assert data.operating_margin == pytest.approx(0.2646, rel=0.02)
    assert data.tax_rate == pytest.approx(0.1444, rel=0.02)


def test_bls_has_no_balance_sheet_data_and_rdcf_is_correctly_unavailable(bls_text):
    """This filing type structurally never states a share price, share
    count or total debt — confirming the auto-assumer doesn't fabricate
    them, and that Reverse DCF (which requires real market data) is
    correctly blocked rather than producing a headline from placeholders."""
    data = PDFExtractor().scrape_figures(bls_text)
    assert data.current_price is None
    assert data.shares_outstanding is None
    assumptions = AutoAssumer().build(data)
    assert "Reverse DCF / Market-Implied Expectations" in assumptions.unavailable


def test_bls_dcf_runs_and_produces_a_finite_enterprise_value(bls_text):
    """The actual end-to-end regression check for the original "$54,280,899,174.28"
    bug: DCF must still run (enterprise value doesn't need share data), and
    its FCF trajectory is now seeded from the real 25.3% growth / 26.5%
    margin instead of the generic 5%/15% that produced the nonsense figure."""
    data = PDFExtractor().scrape_figures(bls_text)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, ["Discounted Cash Flow"], mode="auto")
    assert "Discounted Cash Flow" not in report.errors
    ev = report.results["Discounted Cash Flow"]["enterprise_value"]
    assert ev > 0
    # Real revenue is ~8.9B (INR); a same-order-of-magnitude EV is a sane
    # bound to catch a regression back toward a wildly-off placeholder-
    # driven figure, without pinning to one exact number this test would
    # need updating every time an upstream assumption default changes.
    assert 1e9 < ev < 1e12


def test_bls_hdebt_runs_and_is_flagged_unassessed(bls_text):
    """No lease/contingent-liability disclosures exist in this filing type
    at all — HDEBT should still run (a real, if uninformative, $0
    adjustment) but be flagged as unassessed, not shown as a confirmed
    clean result."""
    data = PDFExtractor().scrape_figures(bls_text)
    assumptions = AutoAssumer().build(data)
    hdebt = "Ind AS 116 Hidden-Debt Normalizer"
    assert hdebt in assumptions.partial
    report = AnalysisRunner(data).run(assumptions, [hdebt], mode="auto")
    assert hdebt not in report.errors
    df = report.summary_frame()
    assert df.loc[df["Model"] == hdebt, "Status"].iloc[0] == "UNASSESSED"


# --------------------------------------------------------------------------- #
# Tesla FY2025 10-K — the "extraction should be much easier" comparison case
# --------------------------------------------------------------------------- #
def test_tesla_core_figures_are_plausible(tesla_text):
    """Independently knowable ground truth for a well-known company:
    Tesla's FY2025 revenue is in the ~$90-100B range and its effective tax
    rate is in the ~20-30% range — sanity bounds, not exact pins, since the
    exact figure a regex lands on depends on which of several real
    same-magnitude numbers in the document it happens to match first."""
    data = PDFExtractor().scrape_figures(tesla_text)
    assert data.company_name and "Tesla" in data.company_name
    assert data.currency == "USD"
    assert data.revenue is not None and 8e10 < data.revenue < 1.1e11
    assert data.net_income is not None and data.net_income > 0
    assert data.tax_rate is not None and 0.15 < data.tax_rate < 0.35


def test_tesla_yoy_derivation_is_a_noop_for_a_10k(tesla_text):
    """A 10-K isn't the SEBI quarterly-results table format the YoY
    derivation targets — must not fire (and must not crash) on a
    structurally different real document."""
    data = PDFExtractor().scrape_figures(tesla_text)
    assert PDFExtractor._derive_yoy_metrics(tesla_text) == {}


def test_tesla_dcf_runs_end_to_end_without_crashing(tesla_text):
    data = PDFExtractor().scrape_figures(tesla_text)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, ["Discounted Cash Flow"], mode="auto")
    assert "Discounted Cash Flow" not in report.errors
    assert report.results["Discounted Cash Flow"]["enterprise_value"] > 0


def test_tesla_total_debt_sums_current_and_long_term_columns(tesla_text):
    """Regression for the bug this backtest originally surfaced: Tesla's
    own "Total debt" line is a multi-column debt-schedule table ("Total
    debt 1,569 6,584 $8,177 $6,429" — current-portion / long-term-portion
    / unpaid-principal / unused-committed-amount); a single-column reader
    grabbed just the first (current-portion) figure, understating real
    total debt ~5x and implying a nonsensical ~21.5% cost of debt.
    PDFExtractor._scrape_total_debt now sums the confirmed Current +
    Long-Term columns instead."""
    data = PDFExtractor().scrape_figures(tesla_text)
    assert data.total_debt == pytest.approx(8_153_000_000, rel=0.01)


def test_tesla_real_cost_of_debt_now_flows_into_wacc(tesla_text):
    """With total_debt fixed, interest expense / real total debt (~4.15%)
    is a genuinely plausible cost of debt and should now be used in WACC
    instead of falling back to the generic rf+150bp default."""
    data = PDFExtractor().scrape_figures(tesla_text)
    assumptions = AutoAssumer().build(data)
    rationale = assumptions.rationale[("DCF", "discount_rate")]
    assert "interest expense/total debt" in rationale
    assert "rf+150bp default" not in rationale
