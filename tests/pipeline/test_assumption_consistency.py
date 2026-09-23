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
    number must be exactly what the constructor's rates give (with the
    regressed 1.05 beta Blume-adjusted to 0.67*1.05 + 0.33)."""
    auto = AutoAssumer(risk_free_rate=0.0425, equity_risk_premium=0.05)
    data = _ticker(total_debt=0.0)
    beta_adj = 0.67 * 1.05 + 0.33
    assert auto.build(data).market_context["wacc"] == pytest.approx(0.0425 + beta_adj * 0.05)


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


# --------------------------------------------------------------------------- #
# Approved valuation conventions (A-G). Each expectation is written out by
# hand from the convention, not read back from the implementation.
# --------------------------------------------------------------------------- #
DCF = "Discounted Cash Flow"


def _us(**kw) -> ExtractedFinancials:
    base = dict(free_cash_flows=[100.0, 120.0, 90.0, 150.0], fcf_history_order="oldest_first",
                revenue_growth=0.10, tax_rate=0.20, currency="USD",
                backends_used=["sec-edgar-xbrl"])
    base.update(kw)
    return ExtractedFinancials(**base)


def test_base_is_the_average_of_the_latest_three_years():
    """B: [120, 90, 150] are the latest three; 100 (oldest) is excluded."""
    a = AutoAssumer().build(_us())
    assert a.kwargs_by_model[RDCF]["base_fcf"] == pytest.approx((120 + 90 + 150) / 3)


def test_interest_and_stock_comp_adjust_each_year_before_averaging():
    """F: FCFF_i = FCF_i + interest_i*(1-t) - SBC_i, per aligned year."""
    data = _us(interest_expense_series=[None, 10.0, 20.0, 30.0],
               sbc_series=[None, 5.0, 5.0, 15.0], interest_paid_classification="operating")
    expected = ((120 + 10 * 0.8 - 5) + (90 + 20 * 0.8 - 5) + (150 + 30 * 0.8 - 15)) / 3
    assert AutoAssumer().build(data).kwargs_by_model[RDCF]["base_fcf"] == pytest.approx(expected)


def test_interest_classified_as_financing_is_not_added_back():
    """F: under IFRS/Ind AS interest paid can sit in financing, so OCF was
    never charged with it; adding it back would count it twice."""
    data = _us(interest_expense_series=[None, 10.0, 20.0, 30.0],
               interest_paid_classification="financing", currency="INR")
    a = AutoAssumer().build(data)
    assert a.kwargs_by_model[RDCF]["base_fcf"] == pytest.approx((120 + 90 + 150) / 3)
    assert "classified as financing" in a.rationale[("DCF", "free_cash_flows")]


def test_operating_leases_are_debt_with_their_interest_added_back():
    """D: the liability joins net debt; its implied interest (lease rate =
    rf + 150bp, after tax) joins FCF, so the lease is counted exactly once."""
    data = _us(total_debt=0.0, cash_and_equivalents=0.0,
               operating_lease_liabilities=1000.0, finance_lease_liabilities=500.0)
    a = AutoAssumer(risk_free_rate=0.04).build(data)
    assert a.kwargs_by_model[DCF]["net_debt"] == pytest.approx(1500.0)
    lease_interest = 1000.0 * (0.04 + 0.015) * (1 - 0.20)
    assert a.kwargs_by_model[RDCF]["base_fcf"] == pytest.approx((120 + 90 + 150) / 3 + lease_interest)


def test_terminal_growth_is_capped_by_the_market_not_a_flat_2_5_percent():
    """C: min(rf, market long-run growth)."""
    india = AutoAssumer(risk_free_rate=0.069, terminal_growth_cap=0.05).build(_us())
    assert india.market_context["terminal_growth"] == pytest.approx(0.05)
    japan = AutoAssumer(risk_free_rate=0.0105, terminal_growth_cap=0.02).build(_us())
    assert japan.market_context["terminal_growth"] == pytest.approx(0.0105)


def test_forecast_fades_to_terminal_growth_over_ten_years():
    """A: year-1 growth is the company's; year-10 growth equals terminal g."""
    fcfs = AutoAssumer().build(_us()).kwargs_by_model[DCF]["free_cash_flows"]
    assert len(fcfs) == 10
    assert fcfs[1] / fcfs[0] - 1 == pytest.approx(0.10 + (0.025 - 0.10) / 9)
    assert fcfs[9] / fcfs[8] - 1 == pytest.approx(0.025)


def test_only_a_regressed_beta_is_blume_adjusted():
    """E: regressed (ticker) 0.29 -> 0.67*0.29 + 0.33; a PDF-stated beta and a
    manual one are used as given."""
    assert AutoAssumer().build(_us(beta=0.29)).market_context["beta"] == pytest.approx(0.5243)
    assert AutoAssumer().build(_us(beta=0.29, backends_used=[])).market_context["beta"] \
        == pytest.approx(0.29)
    assert ManualAssumer().build(_us(beta=0.29), ManualOverrides(beta=0.8)) \
        .market_context["beta"] == pytest.approx(0.8)


def test_gordon_discounts_dividends_at_the_cost_of_equity():
    """G: required return is ke, not WACC (which includes cheaper debt)."""
    data = _us(dividend_per_share=2.0, beta=1.0, backends_used=[], total_debt=500.0,
               current_price=50.0, shares_outstanding=100.0)
    a = AutoAssumer(risk_free_rate=0.04, equity_risk_premium=0.05).build(data)
    assert a.kwargs_by_model["Gordon Growth Model"]["required_return"] == pytest.approx(0.09)
    assert a.market_context["wacc"] < 0.09


def test_gordon_refuses_when_cost_of_equity_hugs_dividend_growth():
    """G: ke 3.5% vs 3% dividend growth leaves a 0.5% gap — the old floor
    would have printed a value ~200x the dividend; now it's unavailable."""
    data = _us(dividend_per_share=2.0, beta=0.1, backends_used=[])
    a = AutoAssumer(risk_free_rate=0.03, equity_risk_premium=0.05).build(data)
    assert "Gordon Growth Model" in a.unavailable


def test_reverse_dcf_uses_the_forward_dcfs_horizon_and_fade():
    """The reverse solve must invert the SAME model the forward DCF runs:
    feed it the forward DCF's own value per share and it has to recover the
    forward DCF's year-1 growth exactly."""
    from src import DiscountedCashFlowModel
    data = _us(current_price=1.0, shares_outstanding=100.0, total_debt=0.0,
               cash_and_equivalents=0.0)
    a = AutoAssumer().build(data)
    fwd = DiscountedCashFlowModel(**a.kwargs_by_model[DCF]).calculate()
    kw = dict(a.kwargs_by_model[RDCF], current_price=fwd["price_per_share"])
    assert kw["years"] == 10 and kw["growth_profile"] == "fade"
    res = ReverseDCFModel(**kw).calculate()
    assert res["implied_initial_growth"] == pytest.approx(0.10, rel=1e-8)
    # headline = the path's equivalent average annual growth
    path = a.kwargs_by_model[DCF]["free_cash_flows"]
    assert res["implied_fcf_cagr"] == pytest.approx(
        (path[-1] / kw["base_fcf"]) ** (1 / 10) - 1, rel=1e-8)


def test_ifrs_filer_with_unknown_interest_classification_gets_no_add_back():
    data = _us(interest_expense_series=[None, 10.0, 20.0, 30.0], currency="USD",
               accounting_standard="ifrs-full", interest_paid_classification=None)
    a = AutoAssumer().build(data)
    assert a.kwargs_by_model[RDCF]["base_fcf"] == pytest.approx((120 + 90 + 150) / 3)
    assert "IFRS filer" in a.rationale[("DCF", "free_cash_flows")]
    # ...but explicit (or lease-evidence-inferred) "operating" still adds back
    data.interest_paid_classification = "operating"
    assert AutoAssumer().build(data).kwargs_by_model[RDCF]["base_fcf"] > (120 + 90 + 150) / 3


# --------------------------------------------------------------------------- #
# Fama-French on the company's real returns
# --------------------------------------------------------------------------- #
FF = "Fama-French 3-Factor"


def _returns_from_factors(b_mkt, s_smb, h_hml, alpha=0.001, months=60):
    """Monthly returns built from the bundled REAL factor rows with known
    loadings — a regression on the right months must recover them exactly."""
    from src import FamaFrenchModel
    f = FamaFrenchModel.load_factors().tail(months)
    r = f["RF"] + alpha + b_mkt * f["Mkt-RF"] + s_smb * f["SMB"] + h_hml * f["HML"]
    return [[int(m), float(v)] for m, v in r.items()]


def test_us_listing_regresses_its_real_returns():
    from src.pipeline import AnalysisRunner
    data = _us(monthly_returns=_returns_from_factors(0.7, -0.3, 0.4),
               return_benchmark="^GSPC")
    a = AutoAssumer().build(data)
    assert FF not in a.partial
    assert "Real regression" in a.rationale[("FF3", "asset returns")]
    res = AnalysisRunner(data).run(a, [FF]).results[FF]
    assert res["beta_mkt"] == pytest.approx(0.7, abs=1e-8)
    assert res["beta_smb"] == pytest.approx(-0.3, abs=1e-8)
    assert res["beta_hml"] == pytest.approx(0.4, abs=1e-8)


def test_non_us_listing_keeps_the_disclosed_illustration():
    data = _us(monthly_returns=_returns_from_factors(1.0, 0, 0), return_benchmark="^NSEI")
    assert "non-US index" in AutoAssumer().build(data).partial[FF]


def test_too_little_overlap_fails_loudly_instead_of_faking_it():
    from src.pipeline import AnalysisRunner
    data = _us(monthly_returns=[[209901 + i, 0.01] for i in range(30)],   # future months
               return_benchmark="^GSPC")
    report = AnalysisRunner(data).run(AutoAssumer().build(data), [FF])
    assert FF in report.errors and "overlap" in report.errors[FF]


def test_hidden_debt_on_a_ticker_load_counts_leases_and_reports_partial():
    """Leases on the balance sheet are known (and already debt), so hidden
    debt is PARTIAL — contingencies still need MANUAL input — not UNASSESSED;
    and its reported net debt matches the DCF's."""
    from src.pipeline import AnalysisRunner
    hd = "Ind AS 116 Hidden-Debt Normalizer"
    data = _us(total_debt=100.0, cash_and_equivalents=0.0, operating_lease_liabilities=40.0,
               net_income=50.0, current_price=10.0, shares_outstanding=100.0)
    a = AutoAssumer().build(data)
    assert a.kwargs_by_model[hd]["reported_net_debt"] == pytest.approx(140.0)
    assert "Leases are covered" in a.partial[hd]
    df = AnalysisRunner(data).run(a, [hd]).summary_frame()
    assert df.loc[df["Model"] == hd, "Status"].iloc[0] == "PARTIAL"
    # no lease data at all -> still honestly UNASSESSED
    b = AutoAssumer().build(_us(net_income=50.0))
    df2 = AnalysisRunner(_us(net_income=50.0)).run(b, [hd]).summary_frame()
    assert df2.loc[df2["Model"] == hd, "Status"].iloc[0] == "UNASSESSED"
