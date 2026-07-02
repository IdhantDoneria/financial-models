"""Automated test-suite validating numerical accuracy, interface and robustness.

Covers three layers:
    * **Benchmarks** — every model's literature/identity ``reference_benchmarks``
      must pass (numerical accuracy).
    * **Interface** — ``calculate``/``explain``/``visualize`` behave per contract.
    * **Robustness** — invalid inputs raise :class:`ValidationError`.
    * **Scoring** — the self-scorer awards all models 10/10 on all three metrics.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from src import (
    ALL_MODELS,
    BinomialTreeModel,
    BlackScholesModel,
    CAPMModel,
    DiscountedCashFlowModel,
    GordonGrowthModel,
    ModernPortfolioTheoryModel,
    MonteCarloOptionModel,
    ValidationError,
    ValueAtRiskModel,
    score_all,
)
from src.scorer import ModelScorer
from tests.conftest import make_instance


# --------------------------------------------------------------------------- #
# 1. Numerical accuracy — every benchmark on every model must pass.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_reference_benchmarks_pass(cls):
    benchmarks = cls.reference_benchmarks()
    assert benchmarks, f"{cls.__name__} defines no benchmarks"
    for b in benchmarks:
        assert b.passed, f"{cls.__name__}: {b.label} relerr={b.rel_error:.2e} > {b.rel_tol:.0e}"


@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_has_machine_precision_benchmark(cls):
    """Each model must carry at least one passing machine-precision identity."""
    assert any(b.rel_tol <= 1e-9 and b.passed for b in cls.reference_benchmarks())


# --------------------------------------------------------------------------- #
# 2. Interface contract — calculate / explain / visualize.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_calculate_returns_dict(cls):
    result = make_instance(cls).calculate()
    assert isinstance(result, dict) and result


@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_explain_has_formula_and_example(cls):
    text = make_instance(cls).explain()
    assert isinstance(text, str) and "$" in text and "example" in text.lower()


@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_visualize_returns_figure(cls):
    fig = make_instance(cls).visualize()
    assert hasattr(fig, "data") and len(fig.data) >= 1


# --------------------------------------------------------------------------- #
# 3. Model-specific numerical checks against known closed forms.
# --------------------------------------------------------------------------- #
def test_black_scholes_hull_example(bs_call):
    assert bs_call.price() == pytest.approx(4.759422, abs=1e-5)


def test_put_call_parity(bs_call):
    put = BlackScholesModel(spot=42, strike=40, rate=0.10, sigma=0.20,
                            maturity=0.5, option_type="put")
    parity = bs_call.price() - put.price()
    assert parity == pytest.approx(42 - 40 * np.exp(-0.10 * 0.5), abs=1e-10)


def test_binomial_converges_to_black_scholes():
    tree = BinomialTreeModel(spot=42, strike=40, rate=0.10, sigma=0.20,
                             maturity=0.5, n_steps=2000)
    assert tree.calculate()["price"] == pytest.approx(4.759422, abs=5e-3)


def test_monte_carlo_within_confidence(bs_call):
    mc = MonteCarloOptionModel(spot=42, strike=40, rate=0.10, sigma=0.20,
                               maturity=0.5, n_sims=500_000, seed=99)
    res = mc.calculate()
    assert res["abs_error"] <= 3 * res["std_error"]


def test_gordon_growth_exact():
    m = GordonGrowthModel(dividend=2.0, required_return=0.08, growth=0.03,
                          dividend_is_forward=True)
    assert m.calculate()["price"] == pytest.approx(40.0, rel=1e-12)


def test_capm_exact():
    m = CAPMModel(risk_free_rate=0.03, expected_market_return=0.10, beta=1.2)
    assert m.calculate()["expected_return"] == pytest.approx(0.114, rel=1e-12)


def test_dcf_positive_and_ordered():
    m = DiscountedCashFlowModel(free_cash_flows=[100, 110, 121], discount_rate=0.10,
                                terminal_growth=0.03, net_debt=50, shares_outstanding=100)
    res = m.calculate()
    assert res["enterprise_value"] > res["equity_value"] > 0
    assert res["price_per_share"] == pytest.approx(res["equity_value"] / 100)


def test_var_parametric_matches_normal_quantile():
    m = ValueAtRiskModel(mean=0.0, std=1.0, confidence_level=0.99,
                         method="parametric", portfolio_value=1.0)
    assert m.calculate()["var"] == pytest.approx(-norm.ppf(0.01), rel=1e-12)


def test_mpt_weights_sum_to_one():
    m = ModernPortfolioTheoryModel(
        expected_returns=[0.10, 0.15], covariance=[[0.04, 0.006], [0.006, 0.09]])
    res = m.calculate()
    assert float(np.sum(res["min_variance_weights"])) == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# 4. Robustness — invalid inputs raise ValidationError.
# --------------------------------------------------------------------------- #
def test_negative_spot_rejected():
    with pytest.raises(ValidationError):
        BlackScholesModel(spot=-1, strike=40, rate=0.1, sigma=0.2, maturity=0.5)


def test_zero_volatility_rejected():
    with pytest.raises(ValidationError):
        BlackScholesModel(spot=42, strike=40, rate=0.1, sigma=0.0, maturity=0.5)


def test_dcf_discount_below_growth_rejected():
    with pytest.raises(ValidationError):
        DiscountedCashFlowModel(free_cash_flows=[100], discount_rate=0.02,
                                terminal_growth=0.05)


def test_bad_option_type_rejected():
    with pytest.raises(ValidationError):
        BlackScholesModel(spot=42, strike=40, rate=0.1, sigma=0.2, maturity=0.5,
                          option_type="banana")


def test_invalid_confidence_rejected():
    with pytest.raises(ValidationError):
        ValueAtRiskModel(mean=0, std=1, confidence_level=1.5, method="parametric")


# --------------------------------------------------------------------------- #
# 5. Self-scoring — the deliverable's headline claim, enforced as a test.
# --------------------------------------------------------------------------- #
def test_all_models_score_ten_out_of_ten():
    df = score_all(ALL_MODELS)
    assert bool(df["perfect"].all()), f"Non-perfect models:\n{df[~df['perfect']]}"
    for col in ("clarity", "accuracy", "production"):
        assert (df[col] >= 10.0).all(), f"{col} below 10 for some model"


@pytest.mark.parametrize("cls", ALL_MODELS, ids=[c.__name__ for c in ALL_MODELS])
def test_scorer_components(cls):
    scorer = ModelScorer(cls)
    assert scorer.score_clarity()[0] >= 9.0
    assert scorer.score_accuracy()[0] >= 9.0
    assert scorer.score_production()[0] >= 9.0
