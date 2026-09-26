"""Guards for the ticker-load intake path (SEC XBRL instead of an uploaded PDF).

Two distinct risks live here, both of which produce a plausible-looking wrong
answer rather than an error — which is the failure mode this product exists to
avoid:

1. **False provenance.** The IB desk now has two intake paths. A rationale
   reading "Scraped from PDF." next to a figure that came from SEC XBRL is a
   specific, authoritative-sounding lie, and being able to say where a number
   came from is the thing this tool sells.
2. **A lazily-loaded dependency nobody awaits.** ``run_report`` ends in
   ``AnalysisReport.summary_frame()``, which imports pandas — but pandas was
   moved out of the browser's core boot packages for load-time reasons. When
   that happened, every IB-desk report run started dying on
   ``ModuleNotFoundError`` unless the visitor happened to open Fama-French
   first and trigger the lazy load by accident. That is a silent, total
   breakage of the paid feature, so it gets a permanent guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.pipeline.assumptions import AutoAssumer
from src.pipeline.pdf_extractor import ExtractedFinancials

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JS = ROOT / "public" / "assets" / "terminal.js"
BOOT_PACKAGES_JS = ROOT / "public" / "assets" / "boot-packages.js"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def _edgar(**kw) -> ExtractedFinancials:
    return ExtractedFinancials(backends_used=["sec-edgar-xbrl"], **kw)


def _pdf(**kw) -> ExtractedFinancials:
    return ExtractedFinancials(backends_used=["pypdf"], **kw)


def test_xbrl_data_is_never_described_as_scraped_from_a_pdf():
    assert AutoAssumer._disclosed_source(_edgar()) == "From the company's SEC XBRL filing data."
    assert "PDF" not in AutoAssumer._disclosed_source(_edgar())


def test_uploaded_filing_is_still_described_as_a_pdf():
    assert AutoAssumer._disclosed_source(_pdf()) == "Scraped from PDF."


def test_unknown_provenance_falls_back_to_the_pdf_wording():
    """The PDF path is the older one and leaves backends_used unset on some
    rehydrated records; defaulting to the SEC wording there would invent a
    source that was never used."""
    assert AutoAssumer._disclosed_source(ExtractedFinancials()) == "Scraped from PDF."
    assert AutoAssumer._disclosed_source(ExtractedFinancials(backends_used=[])) == "Scraped from PDF."


@pytest.mark.parametrize("backend", ["sec-edgar-xbrl", "SEC-EDGAR-XBRL", "sec-edgar"])
def test_provenance_match_is_case_insensitive(backend):
    assert "SEC XBRL" in AutoAssumer._disclosed_source(
        ExtractedFinancials(backends_used=[backend]))


def test_full_rationale_from_xbrl_data_contains_no_pdf_citation():
    """End-to-end: build a real assumption set from EDGAR-shaped data and
    assert nothing in the audit trail claims a PDF it never read."""
    data = _edgar(
        company_name="Apple Inc.", ticker="AAPL", revenue=416_161_000_000.0,
        free_cash_flows=[98_767_000_000.0, 108_807_000_000.0, 99_584_000_000.0],
        net_income=112_010_000_000.0, total_debt=82_347_000_000.0,
        cash_and_equivalents=62_399_000_000.0, shares_outstanding=14_608_963_000.0,
        current_price=332.27, depreciation_amortization=11_445_000_000.0,
        rd_expense=34_550_000_000.0, capital_expenditures=12_715_000_000.0,
        tax_rate=0.156,
    )
    assumptions = AutoAssumer().build(data)
    citations = " ".join(assumptions.rationale.values())
    assert "Scraped from PDF" not in citations
    assert "SEC XBRL" in citations


def test_pdf_sourced_data_still_cites_the_pdf():
    """The mirror of the test above — the fix must not have flipped every
    citation to SEC for filings that really were scraped from a PDF."""
    data = _pdf(
        company_name="Acme Corp", revenue=1_000_000_000.0,
        free_cash_flows=[100_000_000.0, 110_000_000.0],
        depreciation_amortization=50_000_000.0, rd_expense=20_000_000.0,
        capital_expenditures=30_000_000.0,
    )
    citations = " ".join(AutoAssumer().build(data).rationale.values())
    assert "Scraped from PDF" in citations
    assert "SEC XBRL" not in citations


def test_a_regressed_beta_is_never_described_as_a_filing_disclosure():
    """Beta has no XBRL concept — searching a filer's whole us-gaap taxonomy
    for "beta" returns nothing, because no company reports how its returns
    co-move with the market. The ticker path computes it; calling that "from
    the company's SEC XBRL filing data" would be a false citation on the one
    input most likely to be challenged."""
    label = AutoAssumer._beta_source(_edgar())
    assert "XBRL" not in label
    assert "regression" in label.lower()
    assert "market statistic" in label.lower()


def test_market_only_listings_describe_beta_the_same_way():
    """A non-US listing gets price + beta and nothing else; its beta is
    computed by the same regression and must say so."""
    label = AutoAssumer._beta_source(ExtractedFinancials(backends_used=["market-data"]))
    assert "regression" in label.lower()


def test_a_pdf_sourced_beta_still_cites_the_pdf():
    """Filings do occasionally quote a beta in a valuation note, and that one
    really was read off the document."""
    assert AutoAssumer._beta_source(_pdf()) == "Scraped from PDF."
    assert AutoAssumer._beta_source(ExtractedFinancials()) == "Scraped from PDF."


def test_beta_and_general_provenance_do_not_share_wording():
    """The two must stay distinct: a filing figure and a regressed statistic
    have genuinely different origins, and collapsing them is how the false
    citation got introduced in the first place."""
    data = _edgar()
    assert AutoAssumer._beta_source(data) != AutoAssumer._disclosed_source(data)


def test_full_rationale_with_a_beta_names_the_regression_not_the_filing():
    data = _edgar(
        company_name="Apple Inc.", revenue=416_161_000_000.0, beta=1.087,
        free_cash_flows=[98_767_000_000.0, 108_807_000_000.0],
        depreciation_amortization=11_445_000_000.0, rd_expense=34_550_000_000.0,
        capital_expenditures=12_715_000_000.0,
    )
    beta_line = AutoAssumer().build(data).rationale[("CAPM", "beta")]
    assert "XBRL" not in beta_line, f"beta must not claim a filing source: {beta_line}"
    assert "regression" in beta_line.lower()


# --------------------------------------------------------------------------- #
# The lazy-pandas breakage
# --------------------------------------------------------------------------- #
def test_pandas_is_still_absent_from_the_core_boot_packages():
    """If pandas is ever put back into the core set the guard below becomes
    redundant rather than wrong — but this test tells us that happened,
    instead of leaving a stale comment behind."""
    core = BOOT_PACKAGES_JS.read_text()
    assert "FINMODELS_CORE_PACKAGES" in core
    assert "pandas" not in core.split("FINMODELS_CORE_PACKAGES")[1].split("\n")[0]


def test_report_runner_awaits_pandas_before_calling_run_report():
    """``run_report`` -> ``summary_frame()`` -> ``import pandas``.

    pandas is not a boot package, so the browser must load it on demand before
    that call. Without this the IB desk's central action — running the report —
    fails with ModuleNotFoundError for every user who didn't happen to open
    Fama-French first. Asserted against the source because the failure only
    reproduces inside a real Pyodide runtime, which pytest has no access to.
    """
    js = TERMINAL_JS.read_text()
    # Isolate the `run:` entry of the state.ib.fns object.
    start = js.index("        run: async (paramsJson) => {")
    body = js[start:start + 1200]
    assert "await ensurePandas()" in body, (
        "state.ib.fns.run must await ensurePandas() before reaching "
        "web_bridge.run_report, which imports pandas via summary_frame()")
    # ...and it must come before the bridge call it protects.
    assert body.index("await ensurePandas()") < body.index("rawRun("), (
        "ensurePandas() must be awaited BEFORE rawRun(), not after")


def test_summary_frame_still_needs_pandas():
    """The guard above only matters while this dependency exists. If
    summary_frame() stops importing pandas, this fails and points at the
    guard that can then be retired."""
    runner_src = (ROOT / "src" / "pipeline" / "runner.py").read_text()
    assert re.search(r"def summary_frame", runner_src)
    frame_body = runner_src.split("def summary_frame")[1][:400]
    assert "import pandas" in frame_body


# --------------------------------------------------------------------------- #
# Metering
# --------------------------------------------------------------------------- #
def _js_method_body(js: str, signature: str) -> str:
    """Return one method of the state.ib.fns object literal, start to close.

    Slicing a fixed number of characters instead makes the test fail the next
    time the method grows, which is a false alarm that trains people to edit
    the assertion rather than read it. The methods here are indented eight
    spaces inside the object literal, so their closing "        }," is an
    unambiguous terminator.
    """
    start = js.index(signature)
    end = js.index("\n        },", start)
    return js[start:end]


def test_ticker_load_is_metered_like_an_upload():
    """A ticker load is an analysis and consumes an upload credit.

    Routing it around the gate would have handed anyone an unlimited free tier
    just by typing a symbol instead of dragging a PDF.
    """
    body = _js_method_body(TERMINAL_JS.read_text(),
                           "        loadFundamentals: async (fieldsJson) => {")
    assert "await uploadGate()" in body, "ticker loads must check the plan gate"
    assert "await consumeUpload(" in body, "a successful ticker load must consume a credit"
    assert "gate.metered" in body, "only meter when the plan says this account is metered"


def test_reloading_the_same_ticker_does_not_bill_twice():
    """A ticker box has none of a file picker's friction, so accidental
    repeats are easy — and being charged three times for looking at Apple
    once is a refund request, not a rounding error."""
    body = _js_method_body(TERMINAL_JS.read_text(),
                           "        loadFundamentals: async (fieldsJson) => {")
    assert "billedTickers" in body, "repeat loads of one company must be recognised"
    assert "alreadyBilled" in body
    # The guard has to sit on the metering call, not merely be computed.
    meter_line = [ln for ln in body.splitlines() if "gate.metered" in ln][0]
    assert "alreadyBilled" in meter_line, (
        "the already-billed check must gate the metering branch itself")
