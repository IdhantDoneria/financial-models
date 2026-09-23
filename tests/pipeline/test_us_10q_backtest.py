"""Real US 10-Q regression coverage for the PDF extractor.

Every fixture in ``tests/fixtures/`` before this file was either an Indian
SEBI-format quarterly filing or a Tesla 10-K excerpt (an ANNUAL filing) — no
existing test exercised the one structural feature unique to a US 10-Q: two
side-by-side reporting-period columns for BOTH the current and prior year
("Three Months Ended" and "Nine/Six Months Ended"). A keyword-then-nearest-
number extractor that isn't tested against this layout can silently read the
year-to-date column, or a prior-year column, instead of the current quarter
— a wrong number that still looks entirely plausible downstream.

Fixtures (tests/fixtures/):
  - apple_10q_fy2026q3_raw_text_excerpts.txt — Apple Inc.'s real fiscal Q3
    2026 10-Q (SEC accession 0000320193-26-000020). "Three Months Ended /
    Nine Months Ended" layout. Apple's income statement never uses the word
    "revenue" at all — its consolidated top line is "Total net sales" —
    which is exactly what exposed the real bug documented in
    ``test_apple_revenue_used_to_read_deferred_revenue_off_the_balance_sheet``
    below.
  - coca_cola_10q_fy2025q2_raw_text_excerpts.txt — The Coca-Cola Company's
    real calendar Q2 2025 10-Q (SEC accession 0000021344-25-000061). "Three
    Months Ended / Six Months Ended" layout — a different YTD span than
    Apple's fiscal filing. Coca-Cola's income statement carries a
    consolidated PRE-noncontrolling-interest subtotal ("Consolidated Net
    Income") directly above the true bottom line ("Net Income Attributable
    to Shareowners of The Coca-Cola Company") — both contain the literal
    substring "net income", which is what exposed the second real bug (see
    ``test_ko_net_income_used_to_read_the_pre_nci_subtotal`` below).

Ground truth for every assertion below was pulled independently from SEC
XBRL — either ``companyfacts``/``companyconcept`` for the exact facts as
reported in these filings' own accession numbers, or derived by summing the
four most recent XBRL-reported quarters for the TTM checks — never
re-derived from the fixture text itself.

Run: pytest tests/pipeline/test_us_10q_backtest.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.pipeline import AnalysisRunner, AutoAssumer, PDFExtractor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

DCF_MODEL = "Discounted Cash Flow"
MN = 1e6   # SEC filings here are reported "in millions"


def _web_bridge():
    """Load public/py/web_bridge.py the same way test_real_filing_backtest.py
    does — it isn't a package, so it's loaded straight off disk by path."""
    root = FIXTURES.parent.parent
    spec = importlib.util.spec_from_file_location(
        "web_bridge_us10q_test", root / "public" / "py" / "web_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def apple_text() -> str:
    return (FIXTURES / "apple_10q_fy2026q3_raw_text_excerpts.txt").read_text()


@pytest.fixture(scope="module")
def ko_text() -> str:
    return (FIXTURES / "coca_cola_10q_fy2025q2_raw_text_excerpts.txt").read_text()


# --------------------------------------------------------------------------- #
# Ground truth, pulled from SEC XBRL companyfacts/companyconcept independent
# of the fixture text — see the module docstring.
# --------------------------------------------------------------------------- #

# Apple Inc., accession 0000320193-26-000020, fiscal Q3 2026
# (period 2026-03-29 .. 2026-06-27), us-gaap:RevenueFromContractWithCustomer-
# ExcludingAssessedTax / us-gaap:NetIncomeLoss.
AAPL_Q3_REVENUE = 109_417 * MN
AAPL_Q3_NET_INCOME = 29_789 * MN
AAPL_9MO_REVENUE = 364_357 * MN
AAPL_9MO_NET_INCOME = 101_464 * MN
# TTM ending 2026-06-27 = the four most recent XBRL-reported 3-month
# periods: Q4 FY25 (FY25 annual 416,161 minus FY25 9mo 313,695 = 102,466)
# + Q1 FY26 (143,756) + Q2 FY26 (111,184) + Q3 FY26 (109,417).
AAPL_TTM_REVENUE = (102_466 + 143_756 + 111_184 + 109_417) * MN
AAPL_TTM_NET_INCOME = (27_466 + 42_097 + 29_578 + 29_789) * MN   # same method

# The Coca-Cola Company, accession 0000021344-25-000061, calendar Q2 2025
# (period 2025-03-29 .. 2025-06-27), us-gaap:Revenues / us-gaap:NetIncomeLoss
# (the "Net Income Attributable to Shareowners" line — see the net-income
# bug test below for why that specific line, not "Consolidated Net Income").
KO_Q2_REVENUE = 12_535 * MN
KO_Q2_NET_INCOME = 3_810 * MN
KO_6MO_REVENUE = 23_664 * MN
KO_6MO_NET_INCOME = 7_140 * MN
# TTM ending 2025-06-27 = Q3 FY24 (11,854) + Q4 FY24 (FY24 annual 47,061
# minus FY24 9mo 35,517 = 11,544) + Q1 FY25 (11,129) + Q2 FY25 (12,535).
KO_TTM_REVENUE = (11_854 + 11_544 + 11_129 + 12_535) * MN
KO_TTM_NET_INCOME = (2_848 + 2_195 + 3_330 + 3_810) * MN


def _assert_annualised_is_ttm_or_x4(annualised: float, quarterly: float, ttm: float,
                                     label: str, used_ttm: bool) -> None:
    """After annualisation, a flow must equal either the real TTM or exactly
    4x the quarterly figure — nothing else — and the basis label must say
    which one actually happened (see web_bridge._period_basis_label)."""
    x4 = quarterly * 4.0
    if used_ttm:
        assert annualised == pytest.approx(ttm, rel=1e-4)
        assert label == "QUARTERLY (TRAILING 12M)"
    else:
        assert annualised == pytest.approx(x4, rel=1e-6)
        assert label == "QUARTERLY (x4 RUN-RATE)"


# --------------------------------------------------------------------------- #
# Apple — "Three Months Ended / Nine Months Ended", no NCI, no word
# "revenue" anywhere in its own income statement.
# --------------------------------------------------------------------------- #

def test_apple_company_identity(apple_text):
    data = PDFExtractor().scrape_figures(apple_text)
    assert data.company_name and "Apple" in data.company_name


def test_apple_revenue_used_to_read_deferred_revenue_off_the_balance_sheet(apple_text):
    """THE REAL BUG this fixture found: Apple's income statement calls its
    top line "Total net sales", never "revenue" — so none of the revenue
    keyword patterns matched it, and extraction fell through all the way to
    the generic ``(?<!segment )revenues?\\b`` catch-all, whose first
    real-document hit was "Deferred revenue" — a balance-sheet CURRENT
    LIABILITY ($9,538M) — not a $109,417M quarter of sales. Off by >10x, and
    plausible enough (a real, positive dollar figure) to pass every
    downstream sanity check. Fixed by adding a "total net sales" pattern
    ahead of the catch-all in pdf_extractor.py."""
    data = PDFExtractor().scrape_figures(apple_text)
    assert data.revenue == pytest.approx(AAPL_Q3_REVENUE, rel=1e-6)
    assert data.revenue != pytest.approx(9_538 * MN, rel=1e-3), (
        "regressed back to reading the Deferred revenue balance-sheet line")


def test_apple_revenue_and_net_income_are_three_month_not_ytd_or_prior_year(apple_text):
    data = PDFExtractor().scrape_figures(apple_text)
    assert data.revenue == pytest.approx(AAPL_Q3_REVENUE, rel=1e-4)
    assert data.net_income == pytest.approx(AAPL_Q3_NET_INCOME, rel=1e-4)
    # Not the nine-month year-to-date column two columns to the right...
    assert data.revenue != pytest.approx(AAPL_9MO_REVENUE, rel=1e-3)
    assert data.net_income != pytest.approx(AAPL_9MO_NET_INCOME, rel=1e-3)
    # ...and not the prior-year comparative quarter one column to the right.
    assert data.revenue != pytest.approx(94_036 * MN, rel=1e-3)
    assert data.net_income != pytest.approx(23_434 * MN, rel=1e-3)


def test_apple_scale_in_millions_is_applied(apple_text):
    """A raw, unscaled "109417" would be a ludicrous $109 thousand for a
    trillion-dollar company; confirms the "(In millions...)" table header is
    actually being applied, not just a coincidentally-plausible raw digit
    string."""
    data = PDFExtractor().scrape_figures(apple_text)
    assert data.revenue == pytest.approx(109_417_000_000, rel=1e-6)
    assert data.revenue > 1e10   # tens of billions, the real order of magnitude


def test_apple_detected_as_quarterly(apple_text):
    assert _web_bridge()._detect_period(apple_text) == "quarterly"


def test_apple_annualisation_and_basis_label(apple_text):
    wb = _web_bridge()
    data = PDFExtractor().scrape_figures(apple_text)
    quarterly_revenue = data.revenue
    used_ttm = bool(data.ttm_flows)
    wb._annualise_quarterly(data, dividend_is_periodic=True)
    wb._ANALYZER.update(data=data, report=None, period="quarterly")
    _assert_annualised_is_ttm_or_x4(
        data.revenue, quarterly_revenue, AAPL_TTM_REVENUE,
        wb._period_basis_label(), used_ttm)


def test_apple_full_pipeline_produces_a_finite_same_order_ev(apple_text):
    data = PDFExtractor().scrape_figures(apple_text)
    wb = _web_bridge()
    if wb._detect_period(apple_text) == "quarterly":
        wb._annualise_quarterly(data, dividend_is_periodic=True)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, [DCF_MODEL], "auto")
    assert report.errors == {}
    ev = report.results[DCF_MODEL]["enterprise_value"]
    assert ev == ev and ev not in (float("inf"), float("-inf"))   # finite
    # Real Apple market cap is on the order of trillions of dollars; the
    # DCF's own assumption-driven EV should land in the same broad decade,
    # not off by orders of magnitude (a scale/units bug would blow this up
    # or shrink it by 1e3/1e6).
    assert 1e11 < ev < 1e14


# --------------------------------------------------------------------------- #
# Coca-Cola — "Three Months Ended / Six Months Ended" (a different YTD span
# than Apple's), WITH a noncontrolling interest, so its income statement
# carries two different "...net income..." labelled rows.
# --------------------------------------------------------------------------- #

def test_ko_company_identity(ko_text):
    data = PDFExtractor().scrape_figures(ko_text)
    assert data.company_name and "COCA" in data.company_name.upper()


def test_ko_net_income_used_to_read_the_pre_nci_subtotal(ko_text):
    """THE SECOND REAL BUG this fixture found: Coca-Cola's income statement
    states "Consolidated Net Income" (before deducting noncontrolling
    interests, $3,803M) directly above "Net Income Attributable to
    Shareowners of The Coca-Cola Company" (the real bottom line, $3,810M).
    Both contain the literal substring "net income", so the bare `net\\s+
    income` pattern — tried first — stopped at the pre-NCI subtotal instead
    of the true bottom line. The absolute dollar gap is small for a company
    this size (0.18%) but the *label* extraction pointed at is simply wrong,
    and for a company with a larger noncontrolling interest the gap would
    not stay small. Fixed by trying a "net income attributable to
    shareowners/shareholders/the company" pattern before the bare one."""
    data = PDFExtractor().scrape_figures(ko_text)
    assert data.net_income == pytest.approx(KO_Q2_NET_INCOME, rel=1e-6)
    assert data.net_income != pytest.approx(3_803 * MN, rel=1e-4), (
        "regressed back to reading the pre-NCI Consolidated Net Income subtotal")


def test_ko_revenue_and_net_income_are_three_month_not_ytd_or_prior_year(ko_text):
    data = PDFExtractor().scrape_figures(ko_text)
    assert data.revenue == pytest.approx(KO_Q2_REVENUE, rel=1e-4)
    assert data.net_income == pytest.approx(KO_Q2_NET_INCOME, rel=1e-4)
    # Not the six-month year-to-date column...
    assert data.revenue != pytest.approx(KO_6MO_REVENUE, rel=1e-3)
    assert data.net_income != pytest.approx(KO_6MO_NET_INCOME, rel=1e-3)
    # ...and not the prior-year comparative quarter.
    assert data.revenue != pytest.approx(12_363 * MN, rel=1e-3)
    assert data.net_income != pytest.approx(2_411 * MN, rel=1e-3)


def test_ko_scale_in_millions_is_applied(ko_text):
    data = PDFExtractor().scrape_figures(ko_text)
    assert data.revenue == pytest.approx(12_535_000_000, rel=1e-6)
    assert data.revenue > 1e9


def test_ko_detected_as_quarterly(ko_text):
    assert _web_bridge()._detect_period(ko_text) == "quarterly"


def test_ko_annualisation_and_basis_label(ko_text):
    wb = _web_bridge()
    data = PDFExtractor().scrape_figures(ko_text)
    quarterly_revenue = data.revenue
    used_ttm = bool(data.ttm_flows)
    wb._annualise_quarterly(data, dividend_is_periodic=True)
    wb._ANALYZER.update(data=data, report=None, period="quarterly")
    _assert_annualised_is_ttm_or_x4(
        data.revenue, quarterly_revenue, KO_TTM_REVENUE,
        wb._period_basis_label(), used_ttm)


def test_ko_full_pipeline_produces_a_finite_same_order_ev(ko_text):
    data = PDFExtractor().scrape_figures(ko_text)
    wb = _web_bridge()
    if wb._detect_period(ko_text) == "quarterly":
        wb._annualise_quarterly(data, dividend_is_periodic=True)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, [DCF_MODEL], "auto")
    assert report.errors == {}
    ev = report.results[DCF_MODEL]["enterprise_value"]
    assert ev == ev and ev not in (float("inf"), float("-inf"))
    # Real Coca-Cola market cap is on the order of hundreds of billions of
    # dollars.
    assert 1e10 < ev < 1e13
