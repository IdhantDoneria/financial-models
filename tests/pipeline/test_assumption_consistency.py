"""Regressions for assumption-layer defects found in the September 2026
accuracy audit. Each was reproduced on real data before it was fixed:

* MANUAL overrides of the risk-free rate / expected market return never
  reached WACC (``_wacc`` read the constructor's rf/ERP), while the rationale
  printed the overridden rate beside the unchanged number.
* Ticker loads ran every option, VaR and MPT model on a flat 25% volatility,
  18% market volatility and 0.6 correlation, although the loader already had
  five years of monthly returns in hand (TSLA's real volatility is 58%, KO's
  16%).
* Reverse DCF required a total addressable market that only one secondary
  output uses, so it never ran on an automatic analysis.
"""

from __future__ import annotations

import pytest

from src import ReverseDCFModel
from src.pipeline import AutoAssumer, ManualAssumer, ManualOverrides
from src.pipeline.pdf_extractor import ExtractedFinancials

RDCF = "Reverse DCF / Market-Implied Expectations"


def _ticker(**kw) -> ExtractedFinancials:
    base = dict(
        revenue=281_724_000_000.0, free_cash_flows=[59.5e9, 74.1e9, 71.6e9],
        fcf_history_order="oldest_first", total_debt=40e9, cash_and_equivalents=30e9,
        shares_outstanding=7.43e9, current_price=497.42, beta=1.05,
        revenue_growth=0.146, backends_used=["sec-edgar-xbrl"],
    )
    base.update(kw)
    return ExtractedFinancials(**base)


# --------------------------------------------------------------------------- #
# Overrides reach WACC
# --------------------------------------------------------------------------- #
def test_risk_free_override_moves_wacc_and_matches_its_rationale():
    auto = AutoAssumer(risk_free_rate=0.0425, equity_risk_premium=0.05)
    data = _ticker()
    base = auto.build(data).market_context["wacc"]
    over = ManualAssumer(auto).build(data, ManualOverrides(risk_free_rate=0.07))
    # Equity weight ~0.99 here, so WACC should rise by ~the full 275bp.
    assert over.market_context["wacc"] - base == pytest.approx(0.0275, abs=0.003)
    assert "rf 7.00%" in over.rationale[("DCF", "discount_rate")]


def test_market_return_override_gives_dcf_and_capm_one_cost_of_equity():
    auto = AutoAssumer(risk_free_rate=0.0425, equity_risk_premium=0.05)
    data = _ticker(total_debt=0.0)          # all-equity: WACC == cost of equity
    a = ManualAssumer(auto).build(data, ManualOverrides(expected_market_return=0.15))
    capm = a.kwargs_by_model["Capital Asset Pricing Model"]
    ke = capm["risk_free_rate"] + capm["beta"] * (
        capm["expected_market_return"] - capm["risk_free_rate"])
    assert a.market_context["wacc"] == pytest.approx(ke, rel=1e-9)


def test_no_override_leaves_auto_wacc_unchanged():
    """The fix threads rf/ERP through explicitly; with nothing overridden the
    number must be exactly what the constructor's rates give."""
    auto = AutoAssumer(risk_free_rate=0.0425, equity_risk_premium=0.05)
    data = _ticker(total_debt=0.0)
    assert auto.build(data).market_context["wacc"] == pytest.approx(0.0425 + 1.05 * 0.05)


# --------------------------------------------------------------------------- #
# Measured volatility / correlation
# --------------------------------------------------------------------------- #
def test_realised_volatility_replaces_the_flat_default():
    data = _ticker(realized_volatility=0.581, market_volatility=0.154,
                   market_correlation=0.46, return_observations=60)
    a = AutoAssumer().build(data)
    assert a.kwargs_by_model["Black-Scholes-Merton"]["sigma"] == pytest.approx(0.581)
    assert a.kwargs_by_model["Heston Stochastic Volatility"]["v0"] == pytest.approx(0.581 ** 2)
    cov = a.kwargs_by_model["Modern Portfolio Theory"]["covariance"]
    assert cov[1][1] == pytest.approx(0.154 ** 2)
    assert cov[0][1] == pytest.approx(0.46 * 0.581 * 0.154)
    assert "measured" in a.rationale[("Options/MPT/VaR", "volatility")].lower()
    assert "60 monthly returns" in a.rationale[("MPT", "market volatility / correlation")]


def test_disclosed_and_manual_volatility_still_outrank_the_measured_one():
    data = _ticker(realized_volatility=0.58, disclosed_volatility=0.40)
    assert AutoAssumer().build(data).market_context["volatility"] == pytest.approx(0.40)
    a = ManualAssumer().build(data, ManualOverrides(volatility=0.30))
    assert a.market_context["volatility"] == pytest.approx(0.30)
    assert "Manually overridden" in a.rationale[("Options/MPT/VaR", "volatility")]


def test_without_return_history_the_constants_remain_and_say_so():
    a = AutoAssumer().build(ExtractedFinancials(revenue=1e9))
    assert a.market_context["volatility"] == pytest.approx(0.25)
    assert "not derived" in a.rationale[("MPT", "market volatility / correlation")]


# --------------------------------------------------------------------------- #
# Reverse DCF without a TAM
# --------------------------------------------------------------------------- #
def test_reverse_dcf_runs_on_a_ticker_load_without_a_tam():
    a = AutoAssumer().build(_ticker())
    assert RDCF not in a.unavailable
    res = ReverseDCFModel(**a.kwargs_by_model[RDCF]).calculate()
    assert res["implied_fcf_cagr"] is not None
    assert res["implied_tam_capture"] is None


def test_reverse_dcf_still_needs_a_real_price_and_share_count():
    a = AutoAssumer().build(_ticker(current_price=None))
    assert "share price" in a.unavailable[RDCF]
    assert "TAM" not in a.unavailable[RDCF]


def test_reverse_dcf_with_a_tam_still_reports_capture():
    res = ReverseDCFModel(
        current_price=50.0, shares_outstanding=100.0, net_debt=0.0, base_fcf=200.0,
        base_revenue=1000.0, total_addressable_market=10_000.0,
        discount_rate=0.10, terminal_growth=0.03).calculate()
    assert res["implied_tam_capture"] == pytest.approx(
        1000.0 * (1 + res["implied_fcf_cagr"]) ** 5 / 10_000.0)
