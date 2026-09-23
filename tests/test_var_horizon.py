"""Regression tests: historical/Monte-Carlo VaR & CVaR must scale with
``horizon_days`` via the square-root-of-time rule, consistent with the
parametric method (see ``src/var_cvar.py`` module docstring).
"""

from __future__ import annotations

import numpy as np
import pytest

from src import ValueAtRiskModel

_RNG = np.random.default_rng(7)
_SAMPLE = _RNG.normal(0.0005, 0.01, size=5000)


def test_historical_var_h1_matches_unscaled_quantile():
    """h=1 must reproduce the pre-fix (unscaled) numbers exactly."""
    m = ValueAtRiskModel(returns=_SAMPLE, confidence_level=0.95,
                         horizon_days=1, portfolio_value=1e6, method="historical")
    res = m.calculate()
    alpha = 0.05
    q = float(np.quantile(_SAMPLE, alpha))
    tail = _SAMPLE[_SAMPLE <= q]
    assert res["var"] == pytest.approx(-q * 1e6, rel=1e-12)
    assert res["cvar"] == pytest.approx(-float(np.mean(tail)) * 1e6, rel=1e-12)


def test_historical_var_scales_with_sqrt_horizon():
    m1 = ValueAtRiskModel(returns=_SAMPLE, confidence_level=0.95,
                          horizon_days=1, portfolio_value=1e6, method="historical")
    m10 = ValueAtRiskModel(returns=_SAMPLE, confidence_level=0.95,
                           horizon_days=10, portfolio_value=1e6, method="historical")
    res1, res10 = m1.calculate(), m10.calculate()
    assert res10["var"] == pytest.approx(res1["var"] * np.sqrt(10), rel=1e-12)
    assert res10["cvar"] == pytest.approx(res1["cvar"] * np.sqrt(10), rel=1e-12)


def test_monte_carlo_var_scales_with_sqrt_horizon():
    m1 = ValueAtRiskModel(mean=0.0005, std=0.01, confidence_level=0.95,
                          horizon_days=1, portfolio_value=1e6, method="monte_carlo",
                          seed=42, n_sims=50_000)
    m10 = ValueAtRiskModel(mean=0.0005, std=0.01, confidence_level=0.95,
                           horizon_days=10, portfolio_value=1e6, method="monte_carlo",
                           seed=42, n_sims=50_000)
    res1, res10 = m1.calculate(), m10.calculate()
    assert res10["var"] == pytest.approx(res1["var"] * np.sqrt(10), rel=1e-12)
    assert res10["cvar"] == pytest.approx(res1["cvar"] * np.sqrt(10), rel=1e-12)


def test_historical_var_agrees_with_parametric_for_large_gaussian_sample():
    """For Gaussian data, historical and parametric VaR/CVaR should agree
    within sampling tolerance once both are scaled to the same horizon."""
    mu, sigma = 0.0005, 0.01
    sample = np.random.default_rng(123).normal(mu, sigma, size=200_000)
    hist = ValueAtRiskModel(returns=sample, confidence_level=0.95,
                            horizon_days=10, portfolio_value=1e6, method="historical")
    param = ValueAtRiskModel(mean=mu, std=sigma, confidence_level=0.95,
                             horizon_days=10, portfolio_value=1e6, method="parametric")
    res_hist, res_param = hist.calculate(), param.calculate()
    assert res_hist["var"] == pytest.approx(res_param["var"], rel=0.02)
    assert res_hist["cvar"] == pytest.approx(res_param["cvar"], rel=0.03)
