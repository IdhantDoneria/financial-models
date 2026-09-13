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
  - caplin_point_q1_fy2026-27_raw_text.txt — the FULL raw text of a real
    Caplin Point Laboratories quarterly filing, which publishes STANDALONE
    and CONSOLIDATED statements plus a press release and an investor deck in
    one document, in that order. That ordering is the opposite of BLS's and
    is what exposed the cross-basis extraction bug: see
    test_caplin_* below.

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


@pytest.fixture(scope="module")
def caplin_text() -> str:
    return (FIXTURES / "caplin_point_q1_fy2026-27_raw_text.txt").read_text()


CR = 1e7   # one crore, the unit this filing reports in


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


def test_bls_has_no_market_data_and_rdcf_is_correctly_unavailable(bls_text):
    """This filing type never states a market share price — confirming the
    auto-assumer doesn't fabricate one, and that Reverse DCF (which requires
    real market data) is correctly blocked rather than producing a headline
    from placeholders.

    The share COUNT is a different case, and this test originally asserted it
    was unavailable too. That was wrong about the document: a SEBI-format
    statement states paid-up equity share capital and the face value per
    share, and the count is their quotient — here ₹4,117.41 lakhs at Re.1
    face value. The derivation is cross-checked below against the filing's
    own independently-stated EPS, which no part of it depends on.
    """
    data = PDFExtractor().scrape_figures(bls_text)
    assert data.current_price is None
    assert data.shares_outstanding == pytest.approx(411_741_000, rel=0.001)
    # Independent check: the filing states Basic EPS of 4.62 for the quarter.
    # Net profit / derived share count lands at 4.90 — the ~6% gap is the
    # non-controlling interest share (EPS is computed on profit attributable
    # to owners of the parent, net profit here is not), which is the expected
    # direction and magnitude. A wrong share count would miss by orders of
    # magnitude, not by the NCI slice.
    implied_eps = data.net_income / data.shares_outstanding
    assert 4.4 < implied_eps < 5.2
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


def test_tesla_disclosed_stock_comp_volatility_used_instead_of_default(tesla_text):
    """Tesla's real 10-K states "Expected volatility 60% 59% 63%" in its
    stock-comp footnote — a real, filing-disclosed ASC 718 Black-Scholes
    input that was previously extracted only far enough to explicitly
    EXCLUDE it from a false current_price match, then discarded rather
    than used. The option-pricing models (and VaR/MPT, which share the
    same volatility input) should now use this real 60% instead of the
    generic 25% default."""
    data = PDFExtractor().scrape_figures(tesla_text)
    assert data.disclosed_volatility == pytest.approx(0.60, rel=0.01)
    assumptions = AutoAssumer().build(data)
    assert assumptions.market_context["volatility"] == pytest.approx(0.60, rel=0.01)
    assert assumptions.kwargs_by_model["Black-Scholes-Merton"]["sigma"] == pytest.approx(0.60, rel=0.01)


# --------------------------------------------------------------------------- #
# Caplin Point — the filing that exposed cross-basis extraction
#
# This document publishes, in this order: a covering letter, the STANDALONE
# statement, the CONSOLIDATED statement, a press release, and an investor
# deck. BLS publishes consolidated FIRST. Because every field used to be
# resolved by scanning the whole document for that field's own keyword,
# which basis a figure came from was decided by document order — so BLS
# looked correct and Caplin silently blended the two.
# --------------------------------------------------------------------------- #
def test_caplin_identity_is_the_filer_not_a_boilerplate_phrase(caplin_text):
    """The company name resolved to the literal string "the Board" — the
    sign-off regex matched "For and on behalf of the Board", a director
    sign-off, and nothing rejected a capture that plainly isn't a company.
    The ticker came back empty despite appearing twice ("NSE: CAPLIPOINT",
    "Scrip Code: CAPLIPOINT") because only "NSE Symbol:" was recognised."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.company_name == "CAPLIN POINT LABORATORIES LIMITED"
    assert data.ticker == "CAPLIPOINT"
    assert data.currency == "INR"


def test_caplin_numeric_scrip_code_is_never_used_as_a_ticker(caplin_text):
    """The same "Scrip Code:" label prefixes the numeric BSE code (524742)
    directly above the alphabetic NSE one in the covering letter. A numeric
    exchange identifier is not a symbol anyone can fetch a quote with."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.ticker is not None and not data.ticker.isdigit()


def test_caplin_every_figure_comes_from_one_reporting_basis(caplin_text):
    """The core regression. Revenue used to be read from the consolidated
    press-release table while net income and D&A came from the standalone
    statement — real numbers, but from two different statements, so every
    ratio built across them was meaningless while looking reasonable."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.statement_basis == "consolidated"
    # Consolidated Q1 FY27, as the filing states them.
    assert data.revenue == pytest.approx(644 * CR, rel=0.01)          # Total Revenue 643.91
    assert data.net_income == pytest.approx(179.1 * CR, rel=0.01)     # PAT 179.1
    assert data.depreciation_amortization == pytest.approx(21.62 * CR, rel=0.01)
    assert data.interest_expense == pytest.approx(0.18 * CR, rel=0.01)
    # The standalone figures for the same three, which must NOT appear:
    assert data.net_income != pytest.approx(120.38 * CR, rel=0.01)
    assert data.depreciation_amortization != pytest.approx(6.56 * CR, rel=0.01)


def test_caplin_free_cash_flow_is_the_stated_figure_not_a_percentage(caplin_text):
    """free_cash_flows came back [65, 78, 22] crores and went straight into
    the DCF. Only 65 was even a currency figure (a capex number); 78 and 22
    were the two halves of "in the range of 78% and 22% respectively", a
    geographic revenue split on the line after the keyword."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.free_cash_flows == [pytest.approx(40 * CR, rel=0.01)]


def test_caplin_cash_and_capex_are_read_from_the_prose_form(caplin_text):
    """Both are stated, neither in the US-GAAP wording the patterns knew:
    "Free Cash reserves are at Rs 1,502 Crores" and "after Capex investment
    of Rs 55 Crores". Both previously defaulted."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.cash_and_equivalents == pytest.approx(1502 * CR, rel=0.01)
    assert data.capital_expenditures == pytest.approx(55 * CR, rel=0.01)


def test_caplin_share_count_is_derived_from_paid_up_capital(caplin_text):
    """An Indian quarterly statement never states a share count; it states
    paid-up capital (Rs 15.20 Cr) and face value (Rs 2), whose quotient is
    the count. Cross-checked against the filing's own stated Basic EPS of
    23.27, which the derivation does not depend on: PAT / derived shares =
    23.57, the ~1% gap being the non-controlling-interest slice."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.shares_outstanding == pytest.approx(76_000_000, rel=0.001)
    implied_eps = data.net_income / data.shares_outstanding
    assert 22.5 < implied_eps < 24.5


def test_caplin_final_dividend_is_flagged_annual_not_quarterly(caplin_text):
    """The filing recommends "a Final Dividend of Rs.4/- (200%) per equity
    share of Rs.2/- each for the financial year ended March 31, 2026". Two
    traps: the sentence contains a second "Rs.N/-" (the face value) that must
    not be read as the dividend, and the figure is already an ANNUAL
    declaration, so the quarterly-to-annual scaling must leave it alone
    rather than reporting a Rs 16 dividend that was never declared."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.dividend_per_share == pytest.approx(4.0)
    assert data.dividend_is_annual is True


def test_caplin_ratios_match_the_filings_own_stated_percentages(caplin_text):
    """Independent check that the basis narrowing didn't cost accuracy: the
    press release states these three outright, and the derived values agree."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assert data.revenue_growth == pytest.approx(0.207, abs=0.01)      # "increase of 20.7% YoY"
    assert data.operating_margin == pytest.approx(0.350, abs=0.01)    # "PBT Margin 35.0%"
    assert data.tax_rate == pytest.approx(0.205, abs=0.02)            # (225.22-179.1)/225.22


def test_caplin_dcf_runs_on_a_projection_seeded_by_the_real_fcf(caplin_text):
    """A single disclosed FCF is a base, not a trajectory. Handing the DCF a
    one-element list would quietly reduce it to one explicit year plus a
    terminal value; it must be grown into the five-year path instead."""
    data = PDFExtractor().scrape_figures(caplin_text)
    assumptions = AutoAssumer().build(data)
    fcfs = assumptions.kwargs_by_model["Discounted Cash Flow"]["free_cash_flows"]
    assert len(fcfs) == 5
    assert fcfs[0] == pytest.approx(40 * CR * (1 + data.revenue_growth), rel=0.01)
    report = AnalysisRunner(data).run(assumptions, ["Discounted Cash Flow"], mode="auto")
    assert "Discounted Cash Flow" not in report.errors
    assert report.results["Discounted Cash Flow"]["enterprise_value"] > 0


# --------------------------------------------------------------------------- #
# Adversarial cases — the inputs that broke these fixes while building them,
# and the structural variants real filings use that the Caplin document alone
# does not exercise. Every one of these is a case the fix failed on first.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected_basis", [
    ("", "unsegmented"),
    ("   ", "unsegmented"),
    ("Revenue 5", "unsegmented"),
    ("STATEMENT OF UNAUDITED STANDALONE FINANCIAL RESULTS\nTotal Income 100.00\n", "standalone"),
    ("STATEMENT OF AUDITED CONSOLIDATED FINANCIAL RESULTS\nTotal Income 900.00\n", "consolidated"),
    ("Statement of Un-audited Stand-alone Financial Results\nTotal Income 7\n", "standalone"),
])
def test_statement_section_selection_handles_every_document_shape(text, expected_basis):
    """Including the degenerate ones: a document with no statement header at
    all must pass straight through unsegmented, which is what keeps a US 10-K
    (and any press release read on its own) behaving exactly as before."""
    section, basis = PDFExtractor._select_statement_section(text)
    assert basis == expected_basis
    if expected_basis == "unsegmented":
        assert section == text


@pytest.mark.parametrize("text,value,is_annual", [
    # A FINAL dividend covers the whole year and must not be quadrupled.
    ("Recommended a Final Dividend of Rs.4/- (200%) per equity share of "
     "Rs.2/- each for the financial year ended March 31, 2026.", 4.0, True),
    # An interim dividend is a period payment — scaling it is correct.
    ("Declared an Interim Dividend of Rs.3/- per equity share of Rs.2/- each "
     "for the quarter.", 3.0, False),
    # The face value is quoted BEFORE the dividend here; binding to the first
    # "Rs.N/-" in the sentence would report the face value as the dividend.
    ("equity share of Rs.2/- each ... dividend of Rs.7/- per equity share", 7.0, False),
])
def test_dividend_prose_reads_the_dividend_not_the_face_value(text, value, is_annual):
    assert PDFExtractor._scrape_dividend_per_share(text) == (value, is_annual)


@pytest.mark.parametrize("face,capital,unit,expected", [
    ("2", "15.20", "Crores", 76_000_000),        # Caplin Point
    ("1", "4,117.41", "Lakhs", 411_741_000),     # BLS International
    ("10", "500.00", "Lakhs", 5_000_000),        # a Rs 10 face value
])
def test_share_count_derivation_across_face_values_and_scales(face, capital, unit, expected):
    text = (f"Amount in {unit}\nPaid-up equity share capital "
            f"(Face Value Per Share Rs. {face}/-) {capital}")
    assert PDFExtractor._scrape_share_count(text) == pytest.approx(expected, rel=0.001)


def test_share_count_derivation_rejects_an_implausible_result():
    """A mis-parse that yields a fraction of a share is not a small company."""
    text = "Paid-up equity share capital (Face Value Per Share Rs. 100/-) 2.00"
    assert PDFExtractor._scrape_share_count(text) is None


@pytest.mark.parametrize("text", [
    "Free Cash Flow is Rs 40 Crores (after Capex of Rs 55 Crores)",
    "Free Cash Flow is Rs.40 Crores (after capital expenditure of Rs 55 Crores)",
    "Free Cash Flow is INR 40 Crores",
    "Free Cash Flow is 40 Crores (after Capex investment of 55 Crores)",
    "Free Cash Flow stood at 40 Crores",
])
def test_fcf_prose_never_swallows_the_capex_aside(text):
    """Found by attacking the first version of this fix: it accepted only the
    "₹" glyph, so the equally common "Rs 40 Crores" spelling fell through to
    the table-row scan, which returned [40, 55] — the cash flow AND the capex
    deducted to reach it, as two consecutive periods."""
    assert PDFExtractor._scrape_fcf_series(text) == [pytest.approx(40 * CR)]


def test_fcf_table_row_still_reads_a_genuine_multi_period_row():
    """The prose fix must not cost the table-row case: a real FCF row with
    several period columns still yields all of them."""
    assert PDFExtractor._scrape_fcf_series("Free Cash Flow  40  53  65  78") == [40, 53, 65, 78]


# --------------------------------------------------------------------------- #
# Negative free cash flow — a real disclosed figure, not a missing one
#
# The extractor used to require a leading digit, so "(12) Crores" matched
# nothing and the assumer synthesised a POSITIVE free cash flow in its place:
# a fabricated number contradicting what the filing states, which is the exact
# failure class this pipeline exists to refuse.
# --------------------------------------------------------------------------- #
_BURN_FILING = """STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS
Rs in Crores
Total Income 500.00
Net Profit for the period (80.00)
Free Cash Flow is (12) Crores
Paid-up equity share capital (Face Value Per Share Rs. 2/-) 20.00
"""

DCF_MODEL = "Discounted Cash Flow"
RDCF_MODEL = "Reverse DCF / Market-Implied Expectations"


@pytest.mark.parametrize("phrasing", [
    "Free Cash Flow is (12) Crores",
    "Free Cash Flow is -12 Crores",
    "Free Cash Flow is Rs (12) Crores",
    "Negative Free Cash Flow of Rs 12 Crores",
])
def test_negative_fcf_is_extracted_not_discarded(phrasing):
    assert PDFExtractor._scrape_fcf_series(phrasing) == [pytest.approx(-12 * CR)]


def test_negative_fcf_is_never_replaced_by_a_synthesised_positive():
    """The specific silent failure: dropping the negative let the assumer fall
    through to revenue x margin, reporting a positive free cash flow for a
    company that disclosed a burn."""
    data = PDFExtractor().scrape_figures(_BURN_FILING)
    assert data.free_cash_flows == [pytest.approx(-12 * CR)]
    fcfs = AutoAssumer().build(data).kwargs_by_model[DCF_MODEL]["free_cash_flows"]
    assert all(f < 0 for f in fcfs), "a disclosed burn must not become a positive projection"


def test_dcf_and_rdcf_are_gated_with_a_reason_on_a_negative_fcf():
    """Neither model can honestly close a Gordon perpetuity on a cash burn:
    TV = FCF_N(1+g)/(r-g) goes negative, which is arithmetic rather than a
    valuation. ReverseDCFModel already refuses it outright via
    _require_positive, so without the gate a filing with price/shares/TAM
    would surface a raw ValidationError instead of an explained one."""
    assumptions = AutoAssumer().build(PDFExtractor().scrape_figures(_BURN_FILING))
    assert DCF_MODEL in assumptions.unavailable
    assert RDCF_MODEL in assumptions.unavailable
    assert "negative" in assumptions.unavailable[DCF_MODEL].lower()


def test_gated_dcf_reports_the_explanation_not_a_raw_exception():
    data = PDFExtractor().scrape_figures(_BURN_FILING)
    report = AnalysisRunner(data).run(
        AutoAssumer().build(data), [DCF_MODEL], mode="auto")
    status = report.summary_frame().loc[lambda d: d["Model"] == DCF_MODEL, "Status"].iloc[0]
    assert "perpetuity" in status
    assert "Traceback" not in status and "ValidationError" not in status


def test_a_turnaround_series_ending_positive_is_not_gated():
    """Burning early and turning cash-positive is an ordinary, valuable
    company — only the FINAL year drives the terminal perpetuity, so a
    [-10, -5, 3, 8] path must still produce a real valuation."""
    data = PDFExtractor().scrape_figures(_BURN_FILING)
    data.free_cash_flows = [-10 * CR, -5 * CR, 3 * CR, 8 * CR]
    assumptions = AutoAssumer().build(data)
    assert DCF_MODEL not in assumptions.unavailable
    report = AnalysisRunner(data).run(assumptions, [DCF_MODEL], mode="auto")
    assert report.results[DCF_MODEL]["enterprise_value"] > 0


def test_negative_fcf_never_derives_a_negative_revenue():
    """base_revenue falls back to fcfs[0]/margin when revenue is unknown — a
    negative base there would imply a company with negative sales."""
    data = PDFExtractor().scrape_figures("Free Cash Flow is (12) Crores")
    assert data.revenue is None
    assumptions = AutoAssumer().build(data)
    for model_kwargs in assumptions.kwargs_by_model.values():
        for key in ("base_revenue", "portfolio_value"):
            if key in model_kwargs and model_kwargs[key] is not None:
                assert model_kwargs[key] > 0, f"{key} must stay positive"


@pytest.mark.parametrize("fixture_name", [
    "bls_international_q1_fy2026-27_raw_text.txt",
    "tesla_10k_fy2025_raw_text_excerpts.txt",
    "caplin_point_q1_fy2026-27_raw_text.txt",
])
def test_positive_fcf_filings_are_untouched_by_the_negative_handling(fixture_name):
    data = PDFExtractor().scrape_figures((FIXTURES / fixture_name).read_text())
    assert DCF_MODEL not in AutoAssumer().build(data).unavailable


# --------------------------------------------------------------------------
# Scale headers: "(In millions)" / "(₹ in lakhs)" apply to every figure in
# the table they head, regardless of how large that figure is.
#
# These cover the bug that the magnitude test `abs(value) < 1e5` used to
# cause at seven call sites: it was standing in for "this token did not
# already carry its own inline unit suffix", and a genuinely large figure in
# a scaled table failed the proxy and silently lost its multiplier. The
# failure scaled with company size — Apple's real FY2024 net sales came out
# as 391 thousand dollars — so it was invisible on small filers and worst on
# exactly the ones a user is most likely to try first.
# --------------------------------------------------------------------------

_APPLE_SCALED_TABLE = """APPLE INC.
CONSOLIDATED STATEMENTS OF OPERATIONS
(In millions, except number of shares which are reflected in thousands)
Total revenue {revenue}
Interest expense 3,933
"""


@pytest.mark.parametrize("stated, expected", [
    ("391,035", 391_035e6),   # Apple FY2024 net sales — the regressing case.
    ("97,690", 97_690e6),     # Same table, small enough to pass the old test.
    ("100,000", 100_000e6),   # Exactly on the removed 1e5 boundary.
    ("99,999", 99_999e6),     # One below it.
])
def test_a_scale_header_applies_however_large_the_figure(stated, expected):
    data = PDFExtractor().scrape_figures(
        _APPLE_SCALED_TABLE.format(revenue=stated))
    assert data.revenue == pytest.approx(expected)


def test_an_inline_unit_is_not_multiplied_by_the_header_as_well():
    """"$3.2 billion" inside an "(In millions)" section is 3.2e9, not 3.2e15.

    This is what the magnitude test was actually protecting against, and the
    reason the fix keys off whether a unit suffix was consumed rather than
    off how big the number is.
    """
    data = PDFExtractor().scrape_figures(
        _APPLE_SCALED_TABLE.format(revenue="3.2 billion"))
    assert data.revenue == pytest.approx(3.2e9)


def test_indian_lakh_header_applies_to_a_full_year_revenue_column():
    """A real BLS-format full-year figure: Indian digit grouping AND a lakh
    header, the two together being where the bug bit hardest."""
    data = PDFExtractor().scrape_figures(
        "BLS INTERNATIONAL SERVICES LIMITED\n"
        "STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER ENDED\n"
        "(Amount in Rs. in lakhs)\n"
        "Total revenue 2,99,821.51\n")
    assert data.revenue == pytest.approx(2_99_821.51 * 1e5)


def test_a_share_count_above_the_domain_floor_ignores_a_currency_header():
    """A share count is not currency: an absolute count must survive intact
    even when a "(in thousands)" header sits above it."""
    data = PDFExtractor().scrape_figures(
        "(in thousands, except per share data)\n"
        "Total revenue 2,341,000\n"
        "Shares outstanding 3,210,875,752.\n")
    assert data.shares_outstanding == pytest.approx(3_210_875_752)


def test_a_share_count_below_the_domain_floor_takes_the_header_unit():
    """...but no listed company has 3,210 shares, so that one IS in
    thousands. This is the sole surviving magnitude test, and it rests on a
    real floor rather than on a guess about currency magnitudes."""
    data = PDFExtractor().scrape_figures(
        "(in thousands, except per share data)\n"
        "Total revenue 2,341,000\n"
        "Shares outstanding 3,210\n")
    assert data.shares_outstanding == pytest.approx(3_210_000)
