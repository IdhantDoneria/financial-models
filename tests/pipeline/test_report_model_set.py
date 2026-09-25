"""The company report runs a trimmed model set; every model stays a calculator.

Trimmed after the 2026-09 audit of 83 live tickers: Modern Portfolio Theory
returned 0.32 for every stock, and Binomial, Monte Carlo and Heston repeated
Black-Scholes to within 2% on the same hypothetical at-the-money call. Gordon
Growth is skipped when the dividend is a small part of earnings, and the
Ind AS 116 model is skipped for US GAAP filers (in terminal.js).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.pipeline import AutoAssumer, ManualAssumer, ManualOverrides
from src.pipeline.assumptions import _GORDON_MIN_PAYOUT
from src.pipeline.pdf_extractor import ExtractedFinancials
from src.pipeline.runner import AVAILABLE_MODELS

TERMINAL_JS = (Path(__file__).resolve().parents[2] / "public" / "assets" / "terminal.js").read_text()

CUT = ["Modern Portfolio Theory", "Binomial Tree (CRR)", "Monte Carlo (GBM)",
       "Heston Stochastic Volatility"]


def _js_list(name: str) -> list[str]:
    body = re.search(rf"const {name} = \[(.*?)\];", TERMINAL_JS, re.S).group(1)
    return re.findall(r'"([^"]+)"', body)


def test_report_grid_drops_the_four_models_that_add_nothing_on_a_company():
    grid = _js_list("IB_MODELS")
    assert grid == ["Discounted Cash Flow", "Gordon Growth Model", "Value at Risk / CVaR",
                    "Capital Asset Pricing Model", "Fama-French 3-Factor", "Black-Scholes-Merton"]
    for name in CUT:
        assert name not in grid


def test_the_cut_models_are_still_calculators():
    """Trimming the report must not remove the engine: the terminal calculators,
    the landing pages and the MCP tools all run through these classes."""
    for name in CUT:
        assert name in AVAILABLE_MODELS


def test_the_two_premium_models_stay_in_the_report_grid():
    assert _js_list("IB_PREMIUM_MODELS") == [
        "Ind AS 116 Hidden-Debt Normalizer", "Reverse DCF / Market-Implied Expectations"]


def test_no_manual_slider_is_left_feeding_only_a_cut_model():
    ids = re.findall(r'\{ id: "([a-z0-9_]+)"', re.search(
        r"const IB_OVERRIDES = \[(.*?)\n\];", TERMINAL_JS, re.S).group(1))
    assert "monte_carlo_paths" not in ids
    assert not [i for i in ids if i.startswith("heston_")]
    # the option sliders that Black-Scholes still uses are kept
    assert {"volatility", "option_maturity", "strike_ratio"} <= set(ids)
    # and every slider that remains is a real override the Python side accepts
    for i in ids:
        assert hasattr(ManualOverrides(), i), i


def test_ind_as_model_is_skipped_for_us_gaap_filers_without_a_server_call():
    src = TERMINAL_JS[TERMINAL_JS.index("const HDEBT ="):]
    src = src[:src.index("Promise.all(premiumSelected.map") + 600]
    assert 'accounting_standard === "us-gaap"' in src
    assert "notApplicable: true" in src
    # the not-applicable branch returns before the fetch to api/premium
    assert src.index("notApplicable: true") < TERMINAL_JS[TERMINAL_JS.index("const HDEBT ="):].index('fetch("api/premium"')


def _payer(dividend_per_share, net_income, **kw) -> ExtractedFinancials:
    base = dict(free_cash_flows=[100.0, 120.0, 90.0, 150.0], fcf_history_order="oldest_first",
                revenue_growth=0.10, tax_rate=0.20, currency="USD", backends_used=[],
                current_price=50.0, shares_outstanding=100.0, beta=1.0,
                dividend_per_share=dividend_per_share, net_income=net_income)
    base.update(kw)
    return ExtractedFinancials(**base)


def test_gordon_is_skipped_when_the_dividend_is_a_small_part_of_earnings():
    """Apple, Nvidia and GM came out at 0.04x, 0.00x and 0.08x of their prices:
    the model values the dividend alone, so buyback-heavy companies read as a
    fraction of what they are worth. 0.5 x 100 sh / 1,000 = 5% payout."""
    a = AutoAssumer().build(_payer(0.5, 1000.0))
    reason = a.unavailable["Gordon Growth Model"]
    assert "5% of net income" in reason and "DCF" in reason


def test_gordon_runs_when_the_dividend_is_the_main_return_channel():
    just_over = _GORDON_MIN_PAYOUT * 1000.0 / 100.0          # dividend per share at exactly the floor
    assert "Gordon Growth Model" not in AutoAssumer().build(_payer(just_over, 1000.0)).unavailable
    assert "Gordon Growth Model" not in AutoAssumer().build(_payer(6.0, 1000.0)).unavailable
    assert "Gordon Growth Model" in AutoAssumer().build(_payer(just_over * 0.99, 1000.0)).unavailable


def test_gordon_still_runs_when_payout_cannot_be_measured():
    """No net income (or a loss) gives no payout ratio; that is not evidence the
    dividend is small, so the model is not blocked on it."""
    assert "Gordon Growth Model" not in AutoAssumer().build(_payer(2.0, None)).unavailable
    assert "Gordon Growth Model" not in AutoAssumer().build(_payer(2.0, -50.0)).unavailable


def test_a_manual_dividend_growth_overrides_the_payout_gate():
    data = _payer(0.5, 1000.0)
    assert "Gordon Growth Model" in AutoAssumer().build(data).unavailable
    manual = ManualAssumer(AutoAssumer()).build(data, ManualOverrides(dividend_growth=0.03))
    assert "Gordon Growth Model" not in manual.unavailable


def test_a_zero_dividend_is_gated_not_run():
    """Uber reports a dividend of 0.0, not a missing one; it used to reach the
    model and raise `'dividend' must be > 0, got 0.0` in the report."""
    a = AutoAssumer().build(_payer(0.0, 1000.0))
    assert "pays no dividend" in a.unavailable["Gordon Growth Model"]
