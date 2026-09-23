"""Regression tests: horizon (``h``) scaling for VaR & CVaR must split drift
and dispersion -- drift scales as ``h``, dispersion (the de-meaned quantile /
tail mean, or ``sigma``) scales as ``sqrt(h)`` -- consistent between the
parametric and historical/Monte-Carlo estimators (see ``src/var_cvar.py``
module docstring).
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

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


def test_historical_var_scales_with_drift_dispersion_split():
    """VaR(h) must equal -(mean*h + (q - mean)*sqrt(h)) * V, not a flat
    sqrt(h) scaling of the whole h=1 quantile."""
    m1 = ValueAtRiskModel(returns=_SAMPLE, confidence_level=0.95,
                          horizon_days=1, portfolio_value=1e6, method="historical")
    m10 = ValueAtRiskModel(returns=_SAMPLE, confidence_level=0.95,
                           horizon_days=10, portfolio_value=1e6, method="historical")
    res1, res10 = m1.calculate(), m10.calculate()

    alpha = 0.05
    mean = float(np.mean(_SAMPLE))
    q = float(np.quantile(_SAMPLE, alpha))
    tail = _SAMPLE[_SAMPLE <= q]
    tail_mean = float(np.mean(tail))
    sqrt10 = np.sqrt(10)
    expected_var10 = -(mean * 10 + (q - mean) * sqrt10) * 1e6
    expected_cvar10 = -(mean * 10 + (tail_mean - mean) * sqrt10) * 1e6

    assert res10["var"] == pytest.approx(expected_var10, rel=1e-12)
    assert res10["cvar"] == pytest.approx(expected_cvar10, rel=1e-12)
    # Sanity: this is NOT the naive flat sqrt(h) scaling of the h=1 number.
    assert res10["var"] != pytest.approx(res1["var"] * sqrt10, rel=1e-6)


def test_monte_carlo_var_scales_with_drift_dispersion_split():
    m1 = ValueAtRiskModel(mean=0.0005, std=0.01, confidence_level=0.95,
                          horizon_days=1, portfolio_value=1e6, method="monte_carlo",
                          seed=42, n_sims=50_000)
    m10 = ValueAtRiskModel(mean=0.0005, std=0.01, confidence_level=0.95,
                           horizon_days=10, portfolio_value=1e6, method="monte_carlo",
                           seed=42, n_sims=50_000)
    res1, res10 = m1.calculate(), m10.calculate()
    sqrt10 = np.sqrt(10)
    # Same-seed draws are identical between the two models, so we can
    # reconstruct the h=10 result from the h=1 sample's mean/quantile.
    sample = m1._simulate(42, 50_000)
    alpha = 0.05
    mean = float(np.mean(sample))
    q = float(np.quantile(sample, alpha))
    tail_mean = float(np.mean(sample[sample <= q]))
    expected_var10 = -(mean * 10 + (q - mean) * sqrt10) * 1e6
    assert res10["var"] == pytest.approx(expected_var10, rel=1e-12)
    assert res10["var"] != pytest.approx(res1["var"] * sqrt10, rel=1e-6)


def test_parametric_var_h10_matches_closed_form_hand_calculation():
    """Hand-calculated closed form: VaR = -(mu*h + z*sigma*sqrt(h)) * V,
    CVaR = -(mu*h - sigma*sqrt(h)*phi(z)/alpha) * V."""
    mu, sigma = 0.07, 0.20
    alpha = 0.05
    h = 10
    V = 1_000_000.0
    z = norm.ppf(alpha)

    m = ValueAtRiskModel(mean=mu, std=sigma, confidence_level=0.95,
                         horizon_days=h, portfolio_value=V, method="parametric")
    res = m.calculate()

    expected_var = -(mu * h + z * sigma * np.sqrt(h)) * V
    expected_cvar = -(mu * h - sigma * np.sqrt(h) * norm.pdf(z) / alpha) * V

    # Independent hand calculation (values pinned so a regression is caught).
    assert expected_var == pytest.approx(340296.7757511152, rel=1e-9)
    assert expected_cvar == pytest.approx(604574.1261047862, rel=1e-9)

    assert res["var"] == pytest.approx(expected_var, rel=1e-12)
    assert res["cvar"] == pytest.approx(expected_cvar, rel=1e-12)


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


def test_historical_vs_parametric_agreement_h10_large_gaussian_sample():
    """Larger, independent check at mu_annual=0.07, sigma_annual=0.20,
    95% confidence, h=10, $1M portfolio: historical and parametric must
    agree closely on Gaussian data once the drift/dispersion split matches."""
    mu, sigma = 0.07, 0.20
    h = 10
    V = 1_000_000.0
    sample = np.random.default_rng(2026).normal(mu, sigma, size=500_000)

    hist = ValueAtRiskModel(returns=sample, confidence_level=0.95,
                            horizon_days=h, portfolio_value=V, method="historical")
    param = ValueAtRiskModel(mean=mu, std=sigma, confidence_level=0.95,
                             horizon_days=h, portfolio_value=V, method="parametric")
    res_hist, res_param = hist.calculate(), param.calculate()

    assert res_hist["var"] == pytest.approx(res_param["var"], rel=0.02)
    assert res_hist["cvar"] == pytest.approx(res_param["cvar"], rel=0.03)
