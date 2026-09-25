"""The paid server path (api/premium.py) must report what the free report does.

The two premium models are computed on the server, so their headline, status
and reasons used to be re-implemented by hand in api/premium.py. The copy
drifted: a cash-burning company's Reverse DCF is solved in revenue mode, and
the server printed the raw solver output ("20713980000.0") where the report
prints "N% revenue growth"; the country's long-run growth cap never reached
the server; a PARTIAL model was shown as UNASSESSED with no reason.

These tests run the same real company fields (SEC XBRL pulls from the live
universe, 2026-09) through both paths and compare what a user sees.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import api.premium as premium
from src.pipeline import AnalysisRunner, AutoAssumer, ManualAssumer, ManualOverrides
from src.pipeline.pdf_extractor import ExtractedFinancials
from src.pipeline.runner import AnalysisReport

FIELDS = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                     / "paid_path_fields_2026_09.json").read_text())
HDEBT = "Ind AS 116 Hidden-Debt Normalizer"
RDCF = "Reverse DCF / Market-Implied Expectations"
MNEMONIC = {HDEBT: "HDEBT", RDCF: "RDCF"}


def _data(ticker: str) -> ExtractedFinancials:
    allowed = set(ExtractedFinancials.__dataclass_fields__)
    return ExtractedFinancials(**{k: v for k, v in FIELDS[ticker].items() if k in allowed})


def _free_row(ticker: str, model: str, **auto_kw):
    """What the free report produces for `model`: its summary row and reason."""
    data = _data(ticker)
    assumptions = AutoAssumer(**auto_kw).build(data)
    report = AnalysisRunner(data).run(assumptions, [model], "auto")
    if model not in report.results:
        return None, assumptions
    row = report.summary_frame().iloc[0]
    return row, assumptions


def _paid(ticker: str, model: str, **body):
    return premium._run_extracted(MNEMONIC[model], model,
                                  {"extracted": FIELDS[ticker], "mode": "auto", **body})


CASES = [(t, m) for t in FIELDS for m in (RDCF, HDEBT)]


@pytest.mark.parametrize("ticker,model", CASES)
def test_headline_and_status_match_the_free_report(ticker, model):
    row, assumptions = _free_row(ticker, model)
    paid = _paid(ticker, model)
    if row is None:                       # the free path could not run it either
        assert paid["status"] == "INSUFFICIENT DATA"
        return
    assert paid["headline"] == row["Headline result"], (ticker, model)
    assert paid["status"] == row["Status"], (ticker, model)


@pytest.mark.parametrize("ticker,model", CASES)
def test_a_partial_or_unassessed_row_carries_its_reason(ticker, model):
    row, assumptions = _free_row(ticker, model)
    if row is None or row["Status"] == "OK":
        pytest.skip("nothing to explain")
    paid = _paid(ticker, model)
    assert paid["status_reasons"] == {model: assumptions.partial[model]}


def test_a_cash_burner_reads_as_revenue_growth_not_a_raw_number():
    """Rivian burns cash, so its Reverse DCF is solved on revenue growth."""
    paid = _paid("RIVN", RDCF)
    assert paid["headline"].endswith("% revenue growth"), paid["headline"]
    assert paid["results"]["growth_profile"] == "revenue"


def test_the_countrys_long_run_growth_cap_reaches_the_server():
    """The free report builds every model with the selected market's growth cap;
    the paid models used to be built with the default 2.5% whatever the market."""
    kw = {"risk_free_rate": 0.07, "equity_risk_premium": 0.07, "terminal_growth_cap": 0.05}
    row, _ = _free_row("INFY", RDCF, **kw)
    paid = _paid("INFY", RDCF, live_rf=0.07, erp=0.07, lt_growth=0.05)
    default = _paid("INFY", RDCF, live_rf=0.07, erp=0.07)
    assert paid["headline"] == row["Headline result"]
    assert paid["headline"] != default["headline"]


def test_manual_mode_matches_the_free_report():
    ov = {"discount_rate": 0.11, "terminal_growth": 0.03}
    data = _data("AAPL")
    auto = AutoAssumer()
    assumptions = ManualAssumer(auto).build(data, ManualOverrides(**ov))
    row = AnalysisRunner(data).run(assumptions, [RDCF], "manual").summary_frame().iloc[0]
    paid = _paid("AAPL", RDCF, mode="manual", overrides=ov)
    assert paid["headline"] == row["Headline result"]


def test_the_currency_symbol_follows_the_filing():
    paid = _paid("TM", HDEBT)
    assert paid["headline"].startswith("¥"), paid["headline"]


def test_a_us_gaap_filer_is_refused_by_the_server_too():
    """The browser skips Ind AS 116 for US GAAP filers, but the endpoint must not
    depend on that: called directly it used to compute a figure that only echoes
    the company's net debt."""
    paid = _paid("AAPL", HDEBT)
    assert paid["status"] == "INSUFFICIENT DATA"
    assert "US GAAP" in paid["errors"]


# --------------------------------------------------------------------------- #
# Guards that keep the shared code shareable
# --------------------------------------------------------------------------- #
def test_the_server_can_import_the_shared_report_code_without_pandas():
    """api/premium.py now imports AnalysisReport. requirements.txt leaves pandas
    out to stay under Vercel's 500MB Python bundle, so the import must not need
    it. Runs in a fresh interpreter with pandas and plotly blocked."""
    import subprocess
    import sys
    code = (
        "import sys, importlib.abc\n"
        "class B(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, n, p, t=None):\n"
        "        if n.split('.')[0] in ('pandas', 'plotly'):\n"
        "            raise ImportError('blocked ' + n)\n"
        "sys.meta_path.insert(0, B())\n"
        "import api.premium as p\n"
        "assert p.AnalysisReport._headline('Reverse DCF / Market-Implied Expectations',"
        " {'implied_fcf_cagr': 0.05}) == '5.00%'\n")
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-600:]


@pytest.mark.parametrize("mnemonic", ["HDEBT", "RDCF"])
def test_the_calculator_headline_table_agrees_with_the_report_headline(mnemonic):
    """_HEADLINE_PICK (also checked against api/mcp.py) must name the key the
    report's own headline rule picks, or the calculator and the report would
    print different numbers for the same model."""
    key, unit = premium._HEADLINE_PICK[mnemonic]
    name = premium.PREMIUM_MODELS[mnemonic]
    probe = {key: 0.0625 if unit == "%" else 1234.5, "other": 999.0}
    assert premium._headline(mnemonic, probe) == AnalysisReport._headline(name, probe)
    expected = "6.25%" if unit == "%" else "$1,234.50"
    assert premium._headline(mnemonic, probe) == expected


def test_the_browser_sends_the_growth_cap_and_shows_the_servers_reasons():
    js = (Path(__file__).resolve().parents[2] / "public" / "assets" / "terminal.js").read_text()
    call = js[js.index('fetch("api/premium", {\n                method'):]
    call = call[:call.index("const j = await r.json()")]
    assert "lt_growth: params.lt_growth" in call
    assert "baseOut.status_reasons" in js[js.index("for (const pr of premiumResults)"):]


def test_the_free_plan_blurb_matches_what_the_report_runs():
    """The company report runs six models (PR #73); "all 10 models" overstated it.
    The blurb lives in api/_lib/billing.js and, as an offline fallback, in
    terminal.js; the two copies must say the same thing."""
    root = Path(__file__).resolve().parents[2]
    billing = (root / "api" / "_lib" / "billing.js").read_text()
    terminal = (root / "public" / "assets" / "terminal.js").read_text()
    text = "10 company analyses / month · ticker or PDF · six-model valuation report · all 10 calculators · every assumption sourced · SCEN engine"
    joined = billing.replace('" +\n                 "', "")
    assert text in joined
    assert text in terminal
    assert "all 10 models" not in billing and "all 10 models" not in terminal


def test_an_unsolvable_reverse_dcf_does_not_headline_its_enterprise_value():
    """When no growth rate justifies the price both CAGR keys are None. The
    headline used to fall through to the first value in the result, the implied
    enterprise value, and print it under an OK badge."""
    unsolved = {"implied_ev": 40629713091.5, "implied_fcf_cagr": None,
                "growth_profile": "constant", "solver_note": "no root in range"}
    text = AnalysisReport._headline(RDCF, unsolved)
    assert "40629713091" not in text and "no growth rate" in text
    revenue = dict(unsolved, growth_profile="revenue", implied_revenue_cagr=None)
    assert "40629713091" not in AnalysisReport._headline(RDCF, revenue)


def test_the_server_reports_an_unsolvable_reverse_dcf_the_same_way():
    """The live probe that exposed it: an extreme equity risk premium."""
    paid = _paid("INFY", RDCF, erp=5)
    assert paid["results"]["implied_fcf_cagr"] is None, "fixture no longer unsolvable; pick another"
    assert "no growth rate" in paid["headline"]


@pytest.mark.parametrize("code,prefix", [("USD", "$"), ("JPY", "¥"), ("CHF", "CHF "),
                                          ("DKK", "DKK "), ("TWD", "TWD "), (None, "$")])
def test_an_unmapped_currency_is_labelled_with_its_code_not_a_dollar_sign(code, prefix):
    from src.pipeline.pdf_extractor import PDFExtractor
    assert PDFExtractor.currency_prefix(code) == prefix


def test_novo_nordisk_kroner_are_not_printed_as_dollars():
    """NVO reports in DKK, which the symbol map lacked: its Ind AS 116 row read
    "$113,066,000,000.00"."""
    fields = dict(FIELDS["INFY"], currency="DKK")
    body = {"extracted": fields, "mode": "auto"}
    paid = premium._run_extracted("HDEBT", HDEBT, body)
    assert "DKK " in paid["headline"] and "$" not in paid["headline"]
    data = ExtractedFinancials(**{k: v for k, v in fields.items()
                                  if k in ExtractedFinancials.__dataclass_fields__})
    report = AnalysisRunner(data).run(AutoAssumer().build(data), [HDEBT], "auto")
    assert report.summary_frame().iloc[0]["Headline result"] == paid["headline"]


# --------------------------------------------------------------------------- #
# Exports carry the paid rows
# --------------------------------------------------------------------------- #
def _bridge():
    import importlib.util
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("web_bridge_paid_test", root / "public" / "py" / "web_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exports_include_the_server_computed_paid_rows(tmp_path):
    """The two Pro models run on the server, and the exporters read the Python
    report, which only held the models run in the browser: every PDF, Excel and
    Word export silently left out the rows the plan is paid for."""
    import openpyxl
    wb = _bridge()
    data = _data("INFY")
    wb._ANALYZER.update(data=data, report=None, period="annual")
    ran = json.loads(wb.run_report(json.dumps(
        {"mode": "auto", "selected": ["Discounted Cash Flow"], "erp": 0.07, "live_rf": 0.07})))
    assert ran["ok"]
    assert RDCF not in wb._ANALYZER["report"].results        # the state before the fix

    paid = _paid("INFY", RDCF, live_rf=0.07, erp=0.07)
    denied = "Ind AS 116 needs no server figure here"
    out = json.loads(wb.attach_premium(json.dumps(
        {"results": {RDCF: paid["results"]}, "errors": {HDEBT: denied, "Made-up model": "x"}})))
    assert out["ok"]
    report = wb._ANALYZER["report"]
    assert report.results[RDCF] == paid["results"]
    assert report.errors[HDEBT] == denied and "Made-up model" not in report.errors

    row = report.summary_frame().set_index("Model").loc[RDCF]
    assert row["Headline result"] == paid["headline"] and row["Status"] == paid["status"]

    import importlib.util
    formats = ["xlsx", "pdf"] + (["docx"] if importlib.util.find_spec("docx") else [])   # CI lacks python-docx
    for fmt in formats:
        exported = json.loads(wb.export_report(fmt))
        assert exported["ok"], exported
    exported = json.loads(wb.export_report("xlsx"))
    import base64, io
    book = openpyxl.load_workbook(io.BytesIO(base64.b64decode(exported["b64"])))
    text = " ".join(str(c.value) for ws in book for r in ws.iter_rows() for c in r if c.value)
    assert "Reverse DCF" in text and paid["headline"] in text


def test_attaching_before_any_report_is_refused_not_crashed():
    wb = _bridge()
    wb._ANALYZER.update(data=None, report=None, period=None)
    assert json.loads(wb.attach_premium("{}"))["ok"] is False


def test_the_offline_billing_banner_does_not_promise_the_pro_models_for_free():
    """With Razorpay unconnected the banner said "every feature is currently free",
    while api/premium.py still refuses the two Pro models to a free account."""
    js = (Path(__file__).resolve().parents[2] / "public" / "assets" / "terminal.js").read_text()
    banner = js[js.index("BILLING OFFLINE"):]
    banner = banner[:banner.index("</div>`")]
    assert "every feature is currently free" not in banner
    assert "ANALYST PRO" in banner and "Reverse DCF" in banner
