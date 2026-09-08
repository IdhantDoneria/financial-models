"""End-to-end tests for the PDF-analyser pipeline.

Validates every stage:
    1. **Extractor** — builds a synthetic 10-K PDF, extracts, verifies scraped
       fields.
    2. **AutoAssumer** — every model receives well-formed kwargs; missing
       inputs get sensible defaults.
    3. **ManualAssumer** — every override propagates.
    4. **Runner** — runs all twelve models on synthetic data without exceptions.
    5. **Exporters** — PDF and XLSX outputs are created and non-empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from src.pipeline import (
    AVAILABLE_MODELS,
    AnalysisRunner,
    AutoAssumer,
    ManualAssumer,
    ManualOverrides,
    PDFExtractor,
    export_pdf,
    export_xlsx,
)


# --------------------------------------------------------------------------- #
# Fixture: synthetic company PDF
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def synthetic_pdf(tmp_path_factory) -> Path:
    """Build a small synthetic 10-K-style PDF for the extractor tests."""
    path = tmp_path_factory.mktemp("pdf") / "synth.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    doc.build([
        Paragraph("Acme Corporation", styles["Title"]),
        Paragraph("NYSE: ACME", styles["Normal"]),
        Paragraph("Consolidated Financial Statements (in $ millions)", styles["Normal"]),
        Paragraph("For the fiscal year ended December 31, 2025", styles["Normal"]),
        Spacer(1, 20),
        Paragraph("Total revenue     $ 12,450", styles["Normal"]),
        Paragraph("Net income        $ 1,830", styles["Normal"]),
        Paragraph("Total debt        $ 3,500", styles["Normal"]),
        Paragraph("Cash and cash equivalents $ 850", styles["Normal"]),
        Paragraph("Weighted-average shares outstanding 425 million", styles["Normal"]),
        Paragraph("Share price $ 87.50", styles["Normal"]),
        Paragraph("Beta 1.15", styles["Normal"]),
        Paragraph("Revenue growth 12%", styles["Normal"]),
        Paragraph("Operating margin 18%", styles["Normal"]),
        Paragraph("Effective tax rate 22%", styles["Normal"]),
    ])
    return path


# --------------------------------------------------------------------------- #
# 1. Extraction
# --------------------------------------------------------------------------- #
def test_extract_recovers_company_and_headline_figures(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assert data.company_name and "Acme" in data.company_name
    assert data.ticker == "ACME"
    assert data.fiscal_year == 2025
    assert data.revenue == pytest.approx(12_450 * 1e6, rel=0.01)
    assert data.total_debt == pytest.approx(3_500 * 1e6, rel=0.01)
    assert data.cash_and_equivalents == pytest.approx(850 * 1e6, rel=0.01)
    assert data.net_debt == pytest.approx(2_650 * 1e6, rel=0.01)
    assert data.current_price == pytest.approx(87.5, rel=0.01)
    assert data.beta == pytest.approx(1.15, rel=0.01)
    assert 0 < (data.revenue_growth or 0) < 1     # decimalised from percent
    assert data.backends_used                     # at least one backend fired


def test_extractor_rejects_missing_file():
    from src.base_model import ValidationError
    with pytest.raises(ValidationError):
        PDFExtractor().extract("/no/such/file.pdf")


# --------------------------------------------------------------------------- #
# 1b. Extraction robustness — regression tests for real-world extraction bugs
#     found by uploading actual filings (Tesla's FY2025 10-K, BLS
#     International's Q1 FY26-27 results) to production. Each test reproduces
#     the exact real snippet that broke, trimmed to just the sentence that
#     caused it — see GitHub issues #21 and #22 for the full write-ups.
# --------------------------------------------------------------------------- #
def test_parenthesis_only_negates_the_matched_number_not_the_whole_window():
    """An unrelated parenthesised phrase before/after the real number must
    not flip its sign — this exact snippet (from Tesla's 10-K) previously
    turned a genuine 27% tax rate into -27%."""
    text = "Effective tax rate 27 % 20 % (50)% Our provision for income taxes"
    ex = PDFExtractor()
    assert ex._first_after(text, [r"effective\s+tax\s+rate"], window=40) == 27.0


def test_parenthesis_before_the_number_does_not_negate_it():
    """(BLS International's snippet) — an unrelated qualifying phrase in
    parens ahead of the number must not negate it either."""
    text = "total revenues (before consolidation adjustment) of Rs. 38,753.24 lakhs"
    ex = PDFExtractor()
    val = ex._first_after(text, [r"total\s+revenue"], apply_scale=True)
    assert val == pytest.approx(38_753.24 * 1e5)   # positive, and lakh-scaled


def test_genuinely_parenthesised_number_is_still_negative():
    """The fix must not break the legitimate case — a number actually
    wrapped in its own parentheses is still an accounting negative."""
    ex = PDFExtractor()
    assert ex._parse_number("Net loss $(1,234)") == -1234.0


def test_scale_header_far_from_document_start_is_still_found():
    """A full 10-K's "(in millions)" table headers routinely sit 100+ pages
    past the document's opening boilerplate — far past a fixed first-6000-
    character scan window. The scale must be picked up locally, near the
    actual match, not just from the top of the document."""
    ex = PDFExtractor()
    text = (
        "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\n"
        + ("Forward-looking statements boilerplate. " * 2000)  # >> 6000 chars
        + "\nConsolidated Balance Sheets\n(in millions, except per share data)\n"
        "Total debt 1,569 6,584 $ 8,177 $ 6,429\n"
    )
    assert len(text) > 20_000
    val = ex._first_after(text, [r"total\s+debt"], apply_scale=True)
    assert val == pytest.approx(1_569 * 1e6)


def test_lakh_and_crore_scale_recognised():
    """Indian filings use lakh (1e5) / crore (1e7), not million/billion —
    previously not recognised at all, silently understating every figure
    by 100,000x or more."""
    ex = PDFExtractor()
    lakh_text = "Amount in (Rs.) in lakhs\nTotal revenue 38,753.24"
    assert ex._first_after(lakh_text, [r"total\s+revenue"], apply_scale=True) \
        == pytest.approx(38_753.24 * 1e5)
    crore_text = "(Rs. in Crores)\nTotal revenue 387.53"
    assert ex._first_after(crore_text, [r"total\s+revenue"], apply_scale=True) \
        == pytest.approx(387.53 * 1e7)


def test_shares_outstanding_rejects_implausibly_small_footnote_number():
    """(Tesla's snippet) — "shares outstanding" appearing in an unrelated
    stock-compensation footnote must not be accepted as the real share
    count; a real public company never has under 1,000 shares outstanding."""
    text = (
        "ic weighted average shares outstanding until vested. 61 The following table "
        "presents the reconciliation. Diluted shares outstanding were 3,210,875,752."
    )
    ex = PDFExtractor()
    val = ex._first_after(
        text, [r"shares\s+outstanding"], apply_scale=True,
        plausible=PDFExtractor._plausible_share_count,
    )
    assert val == pytest.approx(3_210_875_752)


def test_revenue_growth_rejects_bare_calendar_year():
    """(Tesla's snippet) — "revenue growth" followed by a narrative mention
    of the fiscal year, with no percentage nearby, must not be read as a
    2025% growth rate."""
    text = "profits for further revenue growth. We ended 2025 with $44.06 billion in cash."
    ex = PDFExtractor()
    val = ex._first_after(
        text, [r"revenue\s+growth"], window=40,
        plausible=PDFExtractor._not_year_like,
    )
    assert val is None   # no plausible candidate — correctly abstains


def test_current_price_ignores_option_pricing_volatility_assumption():
    """(Tesla's snippet) — "share price volatility" in a stock-comp footnote
    must not be read as the market price."""
    text = "Expected share price volatility 60 %\nDividend yield — %"
    ex = PDFExtractor()
    val = ex._first_after(
        text, [r"share\s+price(?!\s+volatility)"],
        plausible=lambda v: 0 < v < 1_000_000,
    )
    assert val is None


def test_number_immediately_before_a_year_is_treated_as_a_date_not_a_figure():
    """(Tesla's snippet) — "...on June 30, 2025)" must not read "30" as the
    figure being searched for. The year itself ("2025", not immediately
    followed by another date-tail) passes the date-fragment check alone —
    real callers combine it with a plausibility bound (as current_price's
    actual extraction does) to also reject bare years; this test uses that
    same combination rather than the date check in isolation."""
    text = "based on the closing price for shares as reported on June 30, 2025."
    ex = PDFExtractor()
    val = ex._first_after(
        text, [r"closing\s+price"],
        plausible=lambda v: 0 < v < 1_000_000 and PDFExtractor._not_year_like(v),
    )
    assert val is None   # "30" (date fragment) and "2025" (bare year) both rejected


def test_company_name_prefers_sec_cover_page_registrant_line():
    """(Tesla's snippet) — every SEC 10-K cover page opens with "UNITED
    STATES" / "SECURITIES AND EXCHANGE COMMISSION" before the real company
    name; the actual name sits right before the "(Exact name of
    registrant...)" boilerplate."""
    text = (
        "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nFORM 10-K\n"
        "Tesla, Inc.(Exact name of registrant as specified in its charter)\n"
    )
    assert PDFExtractor._extract_company_name(text) == "Tesla, Inc"


def test_company_name_prefers_indian_filing_signoff_line():
    """(BLS International's snippet) — BSE/NSE regulatory letters open with
    the filing date, not the company name; the name reliably appears in the
    closing "For and on behalf of" signoff instead."""
    text = (
        "August 07, 2026\nTo,\nBSE Limited\n...\n"
        "For and on behalf of,\nBLS International Services Limited\n"
    )
    assert PDFExtractor._extract_company_name(text) == "BLS International Services Limited"


def test_nse_symbol_recognised_as_ticker():
    """(BLS International's snippet) — NYSE/NASDAQ/LSE cover-page tickers
    were recognised; the "NSE Symbol: X" wording Indian filings use instead
    was not."""
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "For and on behalf of,\nBLS International Services Limited\n"
        "NSE Symbol: BLS\nBSE Scrip Code: 540073\n"
    )
    assert data.ticker == "BLS"


# --------------------------------------------------------------------------- #
# 1c. Extraction of the HDEBT owner-earnings fields (D&A, R&D, capex)
# --------------------------------------------------------------------------- #
def test_extracts_depreciation_rd_and_capex_for_owner_earnings():
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "(Dollars in millions)\n"
        "Depreciation and amortization $ 600\n"
        "Research and development expenses $ 900\n"
        "Purchases of property and equipment $ 700\n"
    )
    assert data.depreciation_amortization == pytest.approx(600e6, rel=0.01)
    assert data.rd_expense == pytest.approx(900e6, rel=0.01)
    assert data.capital_expenditures == pytest.approx(700e6, rel=0.01)


def test_capex_reported_as_a_parenthesised_outflow_is_still_a_positive_amount():
    """A cash-flow-statement line reads as a parenthesised outflow, but the
    downstream owner-earnings formula (net_income - maintenance_capex, …)
    expects a positive expenditure magnitude, not a signed accounting entry."""
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "(In millions)\nPurchases of property and equipment (700)\n"
    )
    assert data.capital_expenditures == pytest.approx(700e6, rel=0.01)


# --------------------------------------------------------------------------- #
# 2. Auto assumer — every model gets kwargs
# --------------------------------------------------------------------------- #
def test_auto_assumer_covers_every_model(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    for name in AVAILABLE_MODELS:
        assert name in assumptions.kwargs_by_model, f"missing kwargs for {name}"
    ctx = assumptions.market_context
    assert 0 < ctx["risk_free_rate"] < 0.10
    assert ctx["terminal_growth"] <= ctx["risk_free_rate"]   # Gordon constraint


def test_hdebt_defaults_to_no_adjustment_when_nothing_is_disclosed(synthetic_pdf):
    """The synthetic filing has no lease/reverse-factoring/contingent-
    liability disclosures — the honest default is $0 hidden debt, not a
    guessed number, so adjusted figures should equal the reported ones."""
    data = PDFExtractor().extract(synthetic_pdf)
    kw = AutoAssumer().build(data).kwargs_by_model["Ind AS 116 Hidden-Debt Normalizer"]
    assert kw["annual_lease_payment"] == 0.0
    assert kw["reverse_factoring_exposure"] == 0.0
    assert kw["cl1_amount"] == 0.0 and kw["cl1_probability"] == 0.0
    assert kw["lease_discount_rate"] > 0   # must stay positive (model requires it)


def test_rdcf_tam_defaults_to_ten_times_base_revenue(synthetic_pdf):
    """No filing states its own TAM in a form a regex can trust — the
    auto-assumer's documented placeholder is 10x current revenue."""
    data = PDFExtractor().extract(synthetic_pdf)
    kw = AutoAssumer().build(data).kwargs_by_model["Reverse DCF / Market-Implied Expectations"]
    assert kw["total_addressable_market"] == pytest.approx(10.0 * kw["base_revenue"])


def test_hdebt_and_rdcf_share_the_same_wacc_and_terminal_growth_as_dcf(synthetic_pdf):
    """RDCF explicitly reuses DiscountedCashFlowModel's own EV formula
    internally — its discount_rate/terminal_growth should be the same
    numbers DCF gets, not an independently-guessed second set."""
    data = PDFExtractor().extract(synthetic_pdf)
    a = AutoAssumer().build(data)
    dcf_kw = a.kwargs_by_model["Discounted Cash Flow"]
    rdcf_kw = a.kwargs_by_model["Reverse DCF / Market-Implied Expectations"]
    assert rdcf_kw["discount_rate"] == pytest.approx(dcf_kw["discount_rate"])
    assert rdcf_kw["terminal_growth"] == pytest.approx(dcf_kw["terminal_growth"])


# --------------------------------------------------------------------------- #
# 3. Manual overrides propagate
# --------------------------------------------------------------------------- #
def test_manual_overrides_reach_kwargs(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    ov = ManualOverrides(risk_free_rate=0.06, beta=2.0, discount_rate=0.14,
                         terminal_growth=0.035, volatility=0.5)
    a = ManualAssumer().build(data, ov)
    assert a.market_context["risk_free_rate"] == pytest.approx(0.06)
    assert a.market_context["wacc"] == pytest.approx(0.14)
    assert a.market_context["beta"] == pytest.approx(2.0)
    assert a.market_context["volatility"] == pytest.approx(0.5)
    assert a.kwargs_by_model["Black-Scholes-Merton"]["sigma"] == pytest.approx(0.5)
    assert a.kwargs_by_model["Discounted Cash Flow"]["discount_rate"] == pytest.approx(0.14)


def test_hdebt_rdcf_manual_overrides_reach_kwargs(synthetic_pdf):
    """The footnote-only figures the auto-assumer can't extract (lease
    payment, reverse factoring, contingent liabilities, TAM) must be
    settable by hand — that's the whole point of exposing them as
    ManualOverrides fields instead of leaving them permanently at $0."""
    data = PDFExtractor().extract(synthetic_pdf)
    ov = ManualOverrides(
        annual_lease_payment=200_000_000, lease_term_years=7,
        reverse_factoring_exposure=150_000_000,
        cl1_amount=500_000_000, cl1_probability=0.3,
        total_addressable_market=200_000_000_000,
    )
    a = ManualAssumer().build(data, ov)
    hdebt = a.kwargs_by_model["Ind AS 116 Hidden-Debt Normalizer"]
    assert hdebt["annual_lease_payment"] == pytest.approx(200_000_000)
    assert hdebt["lease_term_years"] == 7
    assert hdebt["reverse_factoring_exposure"] == pytest.approx(150_000_000)
    assert hdebt["cl1_amount"] == pytest.approx(500_000_000)
    assert hdebt["cl1_probability"] == pytest.approx(0.3)
    rdcf = a.kwargs_by_model["Reverse DCF / Market-Implied Expectations"]
    assert rdcf["total_addressable_market"] == pytest.approx(200_000_000_000)


# --------------------------------------------------------------------------- #
# 4. Runner executes every model without exceptions
# --------------------------------------------------------------------------- #
def test_runner_all_models_produce_results(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, list(AVAILABLE_MODELS), mode="auto")
    assert not report.errors, f"models failed: {report.errors}"
    assert set(report.results) == set(AVAILABLE_MODELS)
    for name, res in report.results.items():
        assert res and isinstance(res, dict)


def test_runner_respects_selection_subset(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    subset = ["Discounted Cash Flow", "CAPM"]  # note: 'CAPM' is not the registered key
    subset = ["Discounted Cash Flow", "Capital Asset Pricing Model"]
    a = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(a, subset, mode="auto")
    assert set(report.results) == set(subset)


# --------------------------------------------------------------------------- #
# 5. Exporters produce non-empty files
# --------------------------------------------------------------------------- #
def test_export_pdf_and_xlsx(synthetic_pdf, tmp_path):
    data = PDFExtractor().extract(synthetic_pdf)
    report = AnalysisRunner(data).run(
        AutoAssumer().build(data), list(AVAILABLE_MODELS), mode="auto"
    )
    pdf = export_pdf(report, tmp_path / "report.pdf")
    xlsx = export_xlsx(report, tmp_path / "report.xlsx")
    assert pdf.exists() and pdf.stat().st_size > 2000
    assert xlsx.exists() and xlsx.stat().st_size > 3000
    # PDF header sanity
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_summary_frame_has_row_per_selected_model(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    a = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(a, list(AVAILABLE_MODELS), mode="auto")
    df = report.summary_frame()
    assert len(df) == len(AVAILABLE_MODELS)
    assert set(df.columns) == {"Model", "Headline result", "Status"}
