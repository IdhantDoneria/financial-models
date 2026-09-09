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
# 1d. Regressions for the real BLS-filing footnote-misattribution bug —
#     revenue/net_income were previously scraped from a wrong-but-plausible
#     number in the auditor's "Other Matters" section (unreviewed
#     subsidiaries' figures) instead of the real consolidated statement line,
#     because that footnote sentence happened to contain the FIRST match of
#     the "total revenue" / "profit after tax" keyword patterns.
# --------------------------------------------------------------------------- #
def test_footnote_scoped_revenue_is_disqualified_for_the_real_consolidated_figure():
    """(Real BLS International text, trimmed) — the auditor's caveat about
    nine unreviewed subsidiaries states a "total revenues (before
    consolidation adjustment)" figure that is NOT the company's actual
    consolidated revenue; the real figure sits earlier in the document under
    the Ind AS label "Income from operations". Both numbers are individually
    plausible, so this was silently wrong rather than obviously broken."""
    text = (
        "I Income from operations 89,052.66 81,456.42 71,056.53 2,99,821.51\n"
        "II Other income 2,269.45 3,040.01 2,507.41 9,515.60\n"
        "III Total income (I+II) 91,322.11 84,496.43 73,563.94 3,09,337.11\n"
        "...\n"
        "We did not review the interim financial information of nine "
        "subsidiaries, whose interim financial information reflects total "
        "revenues (before consolidation adjustment) of Rs. 38,753.24 lakhs "
        "for the quarter ended June 30, 2026.\n"
    )
    ex = PDFExtractor()
    val = ex._first_after(
        text, [r"total\s+revenue", r"net\s+revenue",
               r"income\s+from\s+operations", r"total\s+income",
               r"(?<!segment )revenues?\b"],
        apply_scale=True, disqualify=PDFExtractor._FOOTNOTE_SCOPE_DISQUALIFIERS,
    )
    # Scaled 1e5 (lakhs) — the document-wide scale guess picks up "lakhs"
    # from the (disqualified) footnote sentence itself, same as it
    # legitimately would from a real filing's header; that's independent of
    # which keyword match wins, so it correctly applies here too.
    assert val == pytest.approx(89_052.66 * 1e5, rel=0.01), \
        f"got {val} — picked the disqualified footnote figure or the wrong subtotal"


def test_income_from_operations_recognised_as_indian_filing_revenue_label():
    """Many Indian BSE/NSE quarterly filings never use the word "revenue"
    for the consolidated top line at all."""
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "(Amount in Rs. lakhs)\nI Income from operations 89,052.66\n"
    )
    assert data.revenue == pytest.approx(89_052.66 * 1e5, rel=0.01)


def test_segment_revenue_subtable_not_mistaken_for_consolidated_total():
    """(Real BLS International text) — "1 Segment revenue" introduces a
    per-segment breakdown table, not the consolidated figure; the bare
    "revenues?" catch-all pattern must not match it."""
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "1 Segment revenue\n"
        "A) Visa and consular services 56,008.72 47,172.01\n"
        "B) Digital services 33,043.94 34,284.41\n"
    )
    assert data.revenue is None   # no consolidated-total pattern matched


def test_arithmetic_formula_reference_not_mistaken_for_the_figure():
    """(Real BLS International text) — a subtotal row labelled "Total income
    (I+II)" (sometimes OCR'd as "(1+11)") must not have the "1" inside that
    formula reference read as the figure."""
    ex = PDFExtractor()
    val = PDFExtractor._parse_number("Total income (1+11) 91,322.11 84,496.43")
    assert val == pytest.approx(91_322.11)


def test_broken_thousands_separator_space_is_normalised():
    """(Real BLS International text, OCR-degraded) — a scan of "20,162.22"
    came back "20 162.22" (space where the comma should be) on the actual
    Net Profit row; left alone, the number regex reads "20" as a complete
    value and stops there, landing 1,000x too small once scale is applied."""
    val = PDFExtractor._parse_number("20 162.22 18 690.01 18 097.57 72 380.02")
    assert val == pytest.approx(20_162.22)


def test_net_profit_for_the_period_tolerant_of_ocr_corrupted_word():
    """(Real BLS International text, OCR-degraded) — a scan turned "Net
    Profit for the period/year" into "Net Profit for the neriod/vear",
    breaking a pattern that requires the literal word "period"/"quarter"/
    "year". The label "Net Profit for the " itself is distinctive enough to
    match regardless of what OCR did to the word after it."""
    ex = PDFExtractor()
    data = ex.scrape_figures(
        "(Amount in Rs. lakhs)\n"
        "IX Net Profit for the neriod/vear r VII-Vlll1 20 162.22 18 690.01\n"
    )
    assert data.net_income == pytest.approx(20_162.22 * 1e5, rel=0.01)


# --------------------------------------------------------------------------- #
# 1b-2. Currency detection — a DCF/RDCF headline built from an undetected
#       non-USD filing and labelled "$" would misrepresent the actual scale
#       by whatever the real FX rate is (a real live test on the BLS filing
#       produced a "$54,280,899,174.28" DCF headline for INR-denominated
#       figures).
# --------------------------------------------------------------------------- #
def test_lakh_or_crore_anywhere_implies_indian_rupees():
    """The lakh/crore numbering system is used exclusively for INR
    reporting — its presence is a stronger, already-battle-tested signal
    (the scale detector already finds it reliably) than trying to
    separately re-detect INR from a currency symbol/code."""
    assert PDFExtractor._detect_currency("Amount in (Rs.) in lakhs\nTotal income 91,322.11") == "INR"
    assert PDFExtractor._detect_currency("(Rs. in Crores)\nTotal revenue 387.53") == "INR"


def test_currency_symbol_requires_digit_adjacency_not_bare_mention():
    """(Real BLS International text) — "BLS £-Services Limited" is a
    subsidiary's name, not a GBP figure, and a separate UK-based
    acquisition mentioned elsewhere in the same filing has nothing to do
    with what currency the CONSOLIDATED statement itself is denominated
    in. A bare symbol/code with no adjacent number must not flip the
    detected currency away from the real one (INR, signalled by "Rs."
    elsewhere in the same document)."""
    text = (
        "13) BLS Worldwide PTY Limited - South Africa\n"
        "14) BLS £-Services Limited (formerly known as BLS UK Limited)\n"
        "...\n"
        "Rs. 38,753.24 lakhs for the quarter ended June 30, 2026.\n"
    )
    assert PDFExtractor._detect_currency(text) == "INR"


def test_dollar_sign_with_no_other_signal_still_means_usd():
    """The pipeline's original, implicit assumption — unchanged when a
    filing gives no more specific currency signal at all."""
    assert PDFExtractor._detect_currency("Total revenue $ 12,450 million") == "USD"
    assert PDFExtractor._detect_currency("No currency mentioned anywhere here.") == "USD"


def test_headline_uses_the_filings_own_currency_symbol(synthetic_pdf):
    """A DCF headline for a filing whose figures are in INR must be
    prefixed with ₹, not an unconditional $ that misrepresents the scale
    by whatever the real USD/INR rate is."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(
        revenue=1_000_000, net_income=100_000, currency="INR",
        free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    report = AnalysisRunner(data).run(
        AutoAssumer().build(data), ["Discounted Cash Flow"], mode="auto")
    df = report.summary_frame()
    headline = df.loc[df["Model"] == "Discounted Cash Flow", "Headline result"].iloc[0]
    assert headline.startswith("₹"), f"got {headline!r}"

    # A USD (default) filing still gets the original "$" prefix.
    usd_data = PDFExtractor().extract(synthetic_pdf)
    usd_report = AnalysisRunner(usd_data).run(
        AutoAssumer().build(usd_data), ["Discounted Cash Flow"], mode="auto")
    usd_df = usd_report.summary_frame()
    usd_headline = usd_df.loc[usd_df["Model"] == "Discounted Cash Flow", "Headline result"].iloc[0]
    assert usd_headline.startswith("$"), f"got {usd_headline!r}"


# --------------------------------------------------------------------------- #
# 1b-3. Real same-document YoY growth/margin/tax-rate — replaces the generic
#       5%/15%/25% FCF-synthesis constants with the filing's own prior-year
#       comparative column, but ONLY for the specific, regulation-mandated
#       SEBI LODR Regulation 33 quarterly-results table layout (confirmed
#       against the real BLS International filing's exact header/column
#       structure) — not attempted on any other filing shape.
# --------------------------------------------------------------------------- #
_SEBI_QUARTERLY_SNIPPET = (
    "STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026\n"
    "Amount in (Rs.) in lakhs\n"
    "SI. No Particulars Quarter ended Year Ended\n"
    "June 30, 2026 March 31, 2026 June 30, 2025 March 31, 2026\n"
    "Unaudited Audited Unaudited Audited\n"
    "I Income from operations 89,052.66 81,456.42 71,056.53 2,99,821.51\n"
    "II Other income 2,269.45 3,040.01 2,507.41 9,515.60\n"
    "III Total income (I+II) 91,322.11 84,496.43 73,563.94 3,09,337.11\n"
    "VII Profit before tax (V-VI) 23,564.20 20,355.58 20,019.15 79,713.96\n"
    "VIII Total tax expenses 3,401.98 1,665.57 1,921.58 7,333.94\n"
    "IX Net Profit for the period/year 20,162.22 18,690.01 18,097.57 72,380.02\n"
)


def test_yoy_revenue_growth_derived_from_sebi_quarterly_comparative_column():
    """(Real BLS International filing structure) — current quarter vs. the
    same quarter one year earlier (column 3 of 4), not the immediately
    preceding quarter (column 2) — SEBI's mandated layout puts the
    sequential-quarter comparator before the year-ago one."""
    data = PDFExtractor().scrape_figures(_SEBI_QUARTERLY_SNIPPET)
    # (89,052.66 - 71,056.53) / 71,056.53
    assert data.revenue_growth == pytest.approx(0.2533, rel=0.01)


def test_yoy_operating_margin_and_tax_rate_derived_from_same_table():
    data = PDFExtractor().scrape_figures(_SEBI_QUARTERLY_SNIPPET)
    # 23,564.20 / 89,052.66
    assert data.operating_margin == pytest.approx(0.2646, rel=0.01)
    # 3,401.98 / 23,564.20
    assert data.tax_rate == pytest.approx(0.1444, rel=0.01)


def test_yoy_derivation_never_overrides_an_explicitly_stated_value():
    """An explicit "Revenue growth 12%" statement elsewhere in the same
    document must win over the derived column-comparison figure — the
    derivation only fills a genuine gap, never second-guesses a number the
    filing states outright."""
    text = "Revenue growth 12%\n" + _SEBI_QUARTERLY_SNIPPET
    data = PDFExtractor().scrape_figures(text)
    assert data.revenue_growth == pytest.approx(0.12)


def test_yoy_derivation_is_a_noop_without_the_sebi_header(synthetic_pdf):
    """A filing that doesn't match the confirmed SEBI quarterly-results
    table layout gets no derived growth/margin/tax-rate at all — this
    technique is not attempted on a filing shape it hasn't been validated
    against, rather than guessing from an unconfirmed table structure."""
    data = PDFExtractor().extract(synthetic_pdf)   # a plain 10-K-style PDF
    assert PDFExtractor._derive_yoy_metrics(data.raw_text) == {}


def test_dcf_fcf_synthesis_uses_the_derived_growth_and_margin_not_generic_defaults():
    """End-to-end: the real 25.3% growth / 26.5% margin (not the generic
    5%/15%) should be what actually seeds the synthesized FCF trajectory
    AutoAssumer hands to the DCF model when no explicit FCF line exists."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = PDFExtractor().scrape_figures(_SEBI_QUARTERLY_SNIPPET)
    assert not data.free_cash_flows   # this snippet has no labelled FCF row
    auto = AutoAssumer()
    fcfs = auto._synth_fcfs(data, wacc=0.09)
    generic_fcfs = auto._synth_fcfs(
        ExtractedFinancials(revenue=data.revenue), wacc=0.09)   # no derived ratios
    assert fcfs != generic_fcfs
    expected_base = data.revenue * data.operating_margin
    assert fcfs[0] == pytest.approx(expected_base * (1 + data.revenue_growth))


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


def test_extracts_interest_expense_for_real_cost_of_debt():
    ex = PDFExtractor()
    data = ex.scrape_figures("(Dollars in millions)\nInterest expense $ 40\n")
    assert data.interest_expense == pytest.approx(40e6, rel=0.01)
    data2 = ex.scrape_figures("(Rs. in lakhs)\nFinance costs 774.92\n")
    assert data2.interest_expense == pytest.approx(774.92e5, rel=0.01)


def test_extracts_disclosed_stock_comp_volatility_and_feeds_option_models():
    """(Real Tesla 10-K text) — a real, filing-disclosed volatility figure
    should be used instead of the generic 25% default for the option-
    pricing models. The negative lookahead on current_price's own "share
    price" pattern exists specifically because this phrase is common —
    confirm it's now actually captured, not just excluded from the wrong
    field."""
    ex = PDFExtractor()
    text = (
        "Year Ended December 31,\n2025 2024 2023\n"
        "Risk-free interest rate 3.95 % 3.92 % 3.90 %\n"
        "Expected term (in years) 4.7 4.3 4.5\n"
        "Expected volatility 60 % 59 % 63 %\n"
        "Dividend yield — % — % — %\n"
    )
    data = ex.scrape_figures(text)
    assert data.disclosed_volatility == pytest.approx(0.60, rel=0.01)
    assert data.current_price is None   # still not falsely matched as a price

    assumptions = AutoAssumer().build(data)
    assert assumptions.market_context["volatility"] == pytest.approx(0.60, rel=0.01)
    assert assumptions.kwargs_by_model["Black-Scholes-Merton"]["sigma"] == pytest.approx(0.60, rel=0.01)
    rationale = assumptions.rationale[("Options/MPT/VaR", "volatility")]
    assert "60%" in rationale and "stock-comp footnote" in rationale


def test_volatility_default_used_when_nothing_disclosed(synthetic_pdf):
    """No regression — a filing that never states an expected volatility
    (this fixture doesn't) still falls back to the generic 25% default."""
    data = PDFExtractor().extract(synthetic_pdf)
    assert data.disclosed_volatility is None
    assumptions = AutoAssumer().build(data)
    assert assumptions.market_context["volatility"] == pytest.approx(0.25)


def test_mpt_discloses_its_market_vol_and_correlation_are_assumed():
    """MPT's market volatility and correlation are fixed constants with no
    real substitute this app can currently compute — they must at least be
    disclosed as assumed rather than presented as if derived."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000)
    assumptions = AutoAssumer().build(data)
    rationale = assumptions.rationale[("MPT", "market volatility / correlation")]
    assert "not derived" in rationale
    assert "18%" in rationale and "0.60" in rationale
    # The covariance matrix's off-diagonal/second-diagonal entries must
    # actually use these disclosed constants, not some other silent value.
    cov = assumptions.kwargs_by_model["Modern Portfolio Theory"]["covariance"]
    assert cov[1][1] == pytest.approx(0.18 ** 2)


def test_total_debt_sums_current_and_long_term_columns_when_confirmed():
    """(Real Tesla 10-K text, trimmed) — a debt-schedule table with a
    confirmed "Current ... Long-Term" header sums the first two columns
    for real total debt (current-portion + long-term-portion), instead of
    reading just the first number and understating total debt by ~5x."""
    ex = PDFExtractor()
    text = (
        "(in millions)\n"
        "Net Carrying Value\n"
        "Unpaid Principal Balance\n"
        "Unused Committed Amount\n"
        "Contractual Interest Rates\n"
        "Contractual Maturity DateCurrent Long-Term\n"
        "Total debt 1,569 6,584 $ 8,177 $ 6,429\n"
        "Finance leases 71 152\n"
    )
    data = ex.scrape_figures(text)
    assert data.total_debt == pytest.approx((1_569 + 6_584) * 1e6, rel=0.01)


def test_total_debt_reads_a_single_figure_normally_without_current_longterm_header():
    """The common case — a plain "Total debt $X" line with no Current/
    Long-Term column breakdown nearby — must be completely unaffected by
    the summing logic above."""
    ex = PDFExtractor()
    data = ex.scrape_figures("(Dollars in millions)\nTotal debt $ 3,500\n")
    assert data.total_debt == pytest.approx(3_500e6, rel=0.01)


# --------------------------------------------------------------------------- #
# 2b. Real capital structure + cost of debt for WACC
# --------------------------------------------------------------------------- #
def test_wacc_uses_real_equity_weight_and_cost_of_debt_when_plausible():
    """When market cap (price x shares) and total debt are both known, and
    the implied interest-expense/total-debt ratio is a plausible real cost
    of debt, WACC should use them instead of the fixed 80/20 + rf+150bp
    defaults."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(
        revenue=1_000_000, net_income=100_000, current_price=50.0,
        shares_outstanding=10_000_000, total_debt=200_000_000,
        interest_expense=10_000_000,   # 5% of total_debt — plausible
        free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    a = AutoAssumer().build(data)
    # market cap = 50 * 10,000,000 = 500,000,000; we = 500M / (500M + 200M)
    expected_we = 500_000_000 / 700_000_000
    expected_kd = 10_000_000 / 200_000_000
    rf = 0.0425
    expected_wacc = expected_we * (rf + 1.0 * 0.05) + (1 - expected_we) * expected_kd * (1 - 0.25)
    assert a.market_context["wacc"] == pytest.approx(expected_wacc, rel=1e-6)
    assert "market cap" in a.rationale[("DCF", "discount_rate")]


def test_wacc_falls_back_to_default_when_derived_cost_of_debt_is_implausible():
    """(Real Tesla 10-K text) — Tesla's own "Total debt" line is a
    multi-column table ("1,569 6,584 $8,177 $6,429"); the single-column
    extractor grabs the first (current-portion) figure, not the real
    total. Naively dividing interest expense by that understated number
    produces a nonsensical ~21.5% cost of debt — this must be rejected in
    favour of the existing rf+150bp default, not used to corrupt WACC."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(
        revenue=1_000_000, net_income=100_000, current_price=50.0,
        shares_outstanding=10_000_000,
        total_debt=1_569_000_000, interest_expense=338_000_000,   # ~21.5%, implausible
        free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    a = AutoAssumer().build(data)
    rf = 0.0425
    kd_default = rf + 0.015
    we_real = (50.0 * 10_000_000) / (50.0 * 10_000_000 + 1_569_000_000)   # weight is unaffected
    expected_wacc = we_real * (rf + 1.0 * 0.05) + (1 - we_real) * kd_default * (1 - 0.25)
    assert a.market_context["wacc"] == pytest.approx(expected_wacc, rel=1e-6)
    assert "rf+150bp default" in a.rationale[("DCF", "discount_rate")]


def test_wacc_uses_default_weights_when_market_cap_or_debt_unknown(synthetic_pdf):
    """No regression for the common case — a filing missing debt or
    market-cap data (e.g. BLS International's quarterly announcement, which
    states neither share price nor total debt) still gets the original
    80/20 default weighting, not a crash or a garbage derived weight."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    a = AutoAssumer().build(data)
    assert "80/20 default" in a.rationale[("DCF", "discount_rate")]


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


def test_hdebt_marked_partial_when_all_footnote_inputs_are_defaulted(synthetic_pdf):
    """A $0 hidden-debt adjustment because nothing was ever disclosed reads
    identically to a $0 adjustment because there's genuinely nothing to
    adjust — only the second is actually informative. The report layer
    needs to be able to tell them apart instead of showing "OK $0.00"
    either way."""
    data = PDFExtractor().extract(synthetic_pdf)
    hdebt = "Ind AS 116 Hidden-Debt Normalizer"
    assumptions = AutoAssumer().build(data)
    assert hdebt in assumptions.partial
    assert "unassessed" in assumptions.partial[hdebt].lower()

    report = AnalysisRunner(data).run(assumptions, [hdebt], mode="auto")
    assert hdebt in report.results   # still runs — this only flags the result
    df = report.summary_frame()
    assert df.loc[df["Model"] == hdebt, "Status"].iloc[0] == "UNASSESSED"


def test_hdebt_not_marked_partial_once_any_footnote_figure_is_supplied(synthetic_pdf):
    """Supplying even one real footnote figure (not all four) is enough to
    stop treating the result as unassessed — some real disclosure was
    actually incorporated."""
    data = PDFExtractor().extract(synthetic_pdf)
    hdebt = "Ind AS 116 Hidden-Debt Normalizer"
    ov = ManualOverrides(annual_lease_payment=50_000_000)
    assumptions = ManualAssumer().build(data, ov)
    assert hdebt not in assumptions.partial


def test_var_falls_back_to_unit_notional_not_fabricated_market_cap():
    """No filing states its own market cap unless price AND share count
    are both disclosed (the common case, especially for quarterly filings
    — real BLS/Tesla fixtures both come back None for both). The old
    (price or 100.0)*(shares or 1,000,000) fallback substituted a
    fabricated $100M notional, so a real dollar VaR figure was reported
    against a market cap wrong by orders of magnitude for any real
    company. Falls back to VaR's own $1 unit-notional default instead —
    still a real, meaningful %-of-portfolio answer, just not a dollar one."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assumptions = AutoAssumer().build(data)
    kw = assumptions.kwargs_by_model["Value at Risk / CVaR"]
    assert kw["portfolio_value"] == pytest.approx(1.0)
    assert "Value at Risk / CVaR" in assumptions.partial

    report = AnalysisRunner(data).run(assumptions, ["Value at Risk / CVaR"], mode="auto")
    assert "Value at Risk / CVaR" in report.results   # still runs
    df = report.summary_frame()
    row = df.loc[df["Model"] == "Value at Risk / CVaR"].iloc[0]
    assert row["Status"] == "UNASSESSED"
    assert row["Headline result"].endswith("%"), \
        f"expected a % headline on the unit-notional fallback, got {row['Headline result']!r}"


def test_var_uses_the_real_market_cap_and_dollar_headline_when_known():
    """When price and shares ARE both disclosed, VaR should use the real
    market cap (not the unit-notional fallback) and report a real dollar
    figure, not a %."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000, current_price=50.0,
                               shares_outstanding=10_000_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assumptions = AutoAssumer().build(data)
    kw = assumptions.kwargs_by_model["Value at Risk / CVaR"]
    assert kw["portfolio_value"] == pytest.approx(50.0 * 10_000_000)
    assert "Value at Risk / CVaR" not in assumptions.partial

    report = AnalysisRunner(data).run(assumptions, ["Value at Risk / CVaR"], mode="auto")
    df = report.summary_frame()
    row = df.loc[df["Model"] == "Value at Risk / CVaR"].iloc[0]
    assert row["Status"] == "OK"
    assert row["Headline result"].startswith("$"), \
        f"expected a $ headline with a real market cap, got {row['Headline result']!r}"


def test_fama_french_always_flagged_partial_regardless_of_filing_data(synthetic_pdf):
    """Unlike HDEBT/VaR, Fama-French's issue isn't missing filing data —
    even a fully-populated filing (this fixture has revenue, beta,
    everything) doesn't fix it, because AnalysisRunner._build_ff_kwargs
    builds a synthetic asset return series from the SAME beta assumption
    it's then regressed against. That's circular by construction; no
    amount of extracted data changes that, so the flag must always be
    present."""
    data = PDFExtractor().extract(synthetic_pdf)   # a fully-populated fixture
    assert data.beta is not None   # confirms this isn't a missing-data case
    assumptions = AutoAssumer().build(data)
    assert "Fama-French 3-Factor" in assumptions.partial

    report = AnalysisRunner(data).run(assumptions, ["Fama-French 3-Factor"], mode="auto")
    assert "Fama-French 3-Factor" in report.results   # still runs
    df = report.summary_frame()
    row = df.loc[df["Model"] == "Fama-French 3-Factor"].iloc[0]
    assert row["Status"] == "UNASSESSED"


def test_gordon_growth_blocked_when_no_dividend_disclosed(synthetic_pdf):
    """(Real Tesla 10-K structure) — a company that pays no dividend at
    all (or one whose DPS simply wasn't extracted) must not get a
    fabricated "2% of price" dividend fed into Gordon Growth: the model
    can't represent $0, and there's no honest partial output the way
    DCF's per-share value can go unset, so this blocks entirely rather
    than running on a guessed number — same treatment as Reverse DCF's
    missing-TAM case."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000, current_price=50.0,
                               shares_outstanding=10_000_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assert data.dividend_per_share is None
    assumptions = AutoAssumer().build(data)
    kw = assumptions.kwargs_by_model["Gordon Growth Model"]
    assert kw["dividend"] is None
    assert "Gordon Growth Model" in assumptions.unavailable

    report = AnalysisRunner(data).run(assumptions, ["Gordon Growth Model"], mode="auto")
    assert "Gordon Growth Model" not in report.results
    assert "Gordon Growth Model" in report.errors


def test_gordon_growth_runs_normally_once_a_real_dividend_is_known():
    """A company that DOES disclose a real dividend per share should run
    normally — this is a gap in extraction/disclosure, not a blanket
    block on the model."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000, current_price=50.0,
                               shares_outstanding=10_000_000, dividend_per_share=1.5,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assumptions = AutoAssumer().build(data)
    assert "Gordon Growth Model" not in assumptions.unavailable
    report = AnalysisRunner(data).run(assumptions, ["Gordon Growth Model"], mode="auto")
    assert "Gordon Growth Model" in report.results
    assert report.results["Gordon Growth Model"]["price"] > 0


_OPTION_MODELS = ("Black-Scholes-Merton", "Binomial Tree (CRR)",
                  "Monte Carlo (GBM)", "Heston Stochastic Volatility")


def test_option_models_flagged_partial_when_spot_is_fabricated():
    """Unlike DCF/RDCF/VaR, a fabricated $100 spot for the option models
    isn't blocking-level misleading — they're presented in the UI as
    pricing-mechanics demonstrations with an adjustable slider default,
    not company-specific predictions — but they should still be flagged
    for the same reporting consistency HDEBT/VaR get: a user comparing
    models in one report shouldn't see plain "OK" on all four with no
    indication the spot was invented."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000)
    assumptions = AutoAssumer().build(data)
    for model in _OPTION_MODELS:
        assert model in assumptions.partial, f"{model} should be flagged partial"

    report = AnalysisRunner(data).run(assumptions, list(_OPTION_MODELS), mode="auto")
    df = report.summary_frame()
    for model in _OPTION_MODELS:
        assert model in report.results, f"{model} should still run"
        status = df.loc[df["Model"] == model, "Status"].iloc[0]
        assert status == "UNASSESSED", f"{model} status: {status}"


def test_option_models_not_flagged_when_a_real_share_price_is_known():
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, current_price=87.5)
    assumptions = AutoAssumer().build(data)
    for model in _OPTION_MODELS:
        assert model not in assumptions.partial, f"{model} should not be flagged"


def test_rdcf_tam_is_not_fabricated_and_blocks_auto_mode(synthetic_pdf):
    """No filing states its own TAM in a form a regex can trust, and unlike
    WACC/terminal growth there's no formula-based proxy that's meaningfully
    better than a guess — a 10x-revenue placeholder looked precise but
    wasn't. TAM is now a required MANUAL input: left unset (not fabricated)
    in auto mode, and Reverse DCF is marked unavailable until it's
    supplied, even when price/shares are both known (as they are in this
    fixture)."""
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    kw = assumptions.kwargs_by_model["Reverse DCF / Market-Implied Expectations"]
    assert kw["total_addressable_market"] is None
    reason = assumptions.unavailable["Reverse DCF / Market-Implied Expectations"]
    assert "addressable market" in reason

    # Supplying TAM manually (with price/shares already present from the
    # filing) is enough to make it available again.
    ov = ManualOverrides(total_addressable_market=200_000_000_000)
    manual = ManualAssumer().build(data, ov)
    assert "Reverse DCF / Market-Implied Expectations" not in manual.unavailable


def test_rdcf_marked_unavailable_when_price_and_shares_both_undisclosed():
    """A filing with no cover-page market data (e.g. a quarterly
    results-only announcement) must not have Reverse DCF silently run on a
    fabricated $100/1,000,000-share placeholder — the whole model exists to
    invert the market's *real* current price, so a fake one produces a
    confident-looking number describing nothing."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assert data.current_price is None and data.shares_outstanding is None
    assumptions = AutoAssumer().build(data)
    assert "Reverse DCF / Market-Implied Expectations" in assumptions.unavailable
    reason = assumptions.unavailable["Reverse DCF / Market-Implied Expectations"]
    assert "share price" in reason and "share count" in reason

    report = AnalysisRunner(data).run(
        assumptions, ["Reverse DCF / Market-Implied Expectations"], mode="auto")
    assert "Reverse DCF / Market-Implied Expectations" not in report.results
    assert "Reverse DCF / Market-Implied Expectations" in report.errors
    assert report.errors["Reverse DCF / Market-Implied Expectations"] == reason


def test_rdcf_marked_unavailable_when_only_one_of_price_or_shares_is_known():
    """Half a real market cap is still not a real market cap — either
    missing input should block the model, not just both together."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    price_only = ExtractedFinancials(
        revenue=1_000_000, net_income=100_000, current_price=42.0,
        free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assert "Reverse DCF / Market-Implied Expectations" in AutoAssumer().build(price_only).unavailable

    shares_only = ExtractedFinancials(
        revenue=1_000_000, net_income=100_000, shares_outstanding=5_000_000,
        free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assert "Reverse DCF / Market-Implied Expectations" in AutoAssumer().build(shares_only).unavailable


def test_dcf_still_reports_enterprise_value_without_share_data(synthetic_pdf):
    """DCF is not blocked by missing price/shares — enterprise and equity
    value don't depend on either, and price_per_share already correctly
    comes back None (see DiscountedCashFlowModel) rather than being
    computed against a fabricated share count. Only Reverse DCF, whose
    entire output IS a market-implied number, needs the harder gate above."""
    from src.pipeline.pdf_extractor import ExtractedFinancials
    data = ExtractedFinancials(revenue=1_000_000, net_income=100_000,
                               free_cash_flows=[80_000, 85_000, 90_000, 95_000, 100_000])
    assumptions = AutoAssumer().build(data)
    assert "Discounted Cash Flow" not in assumptions.unavailable
    report = AnalysisRunner(data).run(assumptions, ["Discounted Cash Flow"], mode="auto")
    assert "Discounted Cash Flow" not in report.errors
    assert report.results["Discounted Cash Flow"]["price_per_share"] is None
    assert isinstance(report.results["Discounted Cash Flow"]["enterprise_value"], float)


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
    """Reverse DCF and Gordon Growth are the expected exceptions in pure
    auto mode — no filing states a trustworthy TAM (see
    test_rdcf_tam_is_not_fabricated_and_blocks_auto_mode) or, on this
    fixture, a dividend per share (see
    test_gordon_growth_blocked_when_no_dividend_disclosed) — both are
    required manual inputs and correctly land in errors, not results,
    until supplied."""
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, list(AVAILABLE_MODELS), mode="auto")
    rdcf = "Reverse DCF / Market-Implied Expectations"
    gordon = "Gordon Growth Model"
    assert set(report.errors) == {rdcf, gordon}, f"unexpected failures: {report.errors}"
    assert set(report.results) == set(AVAILABLE_MODELS) - {rdcf, gordon}
    for name, res in report.results.items():
        assert res and isinstance(res, dict)

    # Supplying TAM/dividend manually unblocks both, same as everything else.
    ov = ManualOverrides(total_addressable_market=200_000_000_000)
    manual_assumptions = ManualAssumer().build(data, ov)
    manual_report = AnalysisRunner(data).run(manual_assumptions, [rdcf], mode="manual")
    assert not manual_report.errors, f"models failed: {manual_report.errors}"
    assert rdcf in manual_report.results


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
