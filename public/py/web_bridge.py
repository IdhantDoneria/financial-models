"""Browser-side bridge between the terminal UI and the real model classes.

Runs inside Pyodide. The front end calls :func:`run_model` with a mnemonic and
a flat dict of slider values; this module translates those scalars into each
model's constructor arguments (synthesizing series where a model consumes
series — covariance assembly for MPT, seeded return draws for VaR, factor-
history windowing for Fama-French) and returns results, the ``explain()``
markdown and the ``visualize()`` figure as JSON.

The model classes themselves are imported *unchanged* from ``src/`` — this file
contains zero pricing/valuation logic.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

import numpy as np

from src import (
    BinomialTreeModel,
    BlackScholesModel,
    CAPMModel,
    DiscountedCashFlowModel,
    FamaFrenchModel,
    GordonGrowthModel,
    HestonModel,
    ModernPortfolioTheoryModel,
    MonteCarloOptionModel,
    ValueAtRiskModel,
)

_TRADING_DAYS = 252
_SEED = 20260702  # deterministic demo draws: same sliders -> same numbers


# --------------------------------------------------------------------------- #
# Builders: flat slider params -> model instance (+ optional extra outputs)
# --------------------------------------------------------------------------- #
def _build_dcf(p: dict) -> DiscountedCashFlowModel:
    # Project the FCF path from a base amount and constant growth ($M units;
    # shares are in millions, so per-share values land in plain dollars).
    years = int(p["years"])
    fcfs = [p["base_fcf"] * (1.0 + p["fcf_growth"]) ** t for t in range(1, years + 1)]
    return DiscountedCashFlowModel(
        free_cash_flows=fcfs,
        discount_rate=p["discount_rate"],
        terminal_growth=p["terminal_growth"],
        net_debt=p["net_debt"],
        shares_outstanding=p["shares_outstanding"],
    )


def _build_gg(p: dict) -> GordonGrowthModel:
    return GordonGrowthModel(
        dividend=p["dividend"],
        required_return=p["required_return"],
        growth=p["growth"],
    )


def _build_mpt(p: dict) -> ModernPortfolioTheoryModel:
    mu = np.array([p["mu1"], p["mu2"], p["mu3"]])
    sig = np.array([p["sigma1"], p["sigma2"], p["sigma3"]])
    rho = max(float(p["rho"]), -0.45)  # keep the 3-asset matrix positive-definite
    corr = np.full((3, 3), rho)
    np.fill_diagonal(corr, 1.0)
    cov = corr * np.outer(sig, sig)
    return ModernPortfolioTheoryModel(
        expected_returns=mu, covariance=cov, risk_free_rate=p["risk_free_rate"]
    )


def _build_var(p: dict) -> ValueAtRiskModel:
    # Scale annual mu/sigma to daily; historical & MC methods consume a seeded
    # synthetic daily return sample, parametric takes the moments directly.
    mu_d = p["mu_annual"] / _TRADING_DAYS
    sd_d = p["sigma_annual"] / math.sqrt(_TRADING_DAYS)
    common = dict(
        confidence_level=p["confidence"],
        horizon_days=int(p["horizon_days"]),
        portfolio_value=p["portfolio_value"],
        method=p["method"],
    )
    if p["method"] == "parametric":
        return ValueAtRiskModel(mean=mu_d, std=sd_d, **common)
    rng = np.random.default_rng(_SEED)
    returns = rng.normal(mu_d, sd_d, size=10 * _TRADING_DAYS)
    return ValueAtRiskModel(returns=returns, **common)


def _build_capm(p: dict) -> CAPMModel:
    return CAPMModel(
        risk_free_rate=p["risk_free_rate"],
        expected_market_return=p["expected_market_return"],
        beta=p["beta"],
    )


def _build_ff3(p: dict) -> tuple[FamaFrenchModel, dict]:
    # Real Ken French history (bundled snapshot) + user-chosen "true" loadings
    # -> synthetic asset returns; the regression must recover the loadings.
    factors = FamaFrenchModel.load_factors().tail(int(p["window"]))
    rng = np.random.default_rng(_SEED)
    eps = rng.normal(0.0, p["idio_sigma"], size=len(factors))
    r = (factors["RF"] + p["alpha"] + p["b_mkt"] * factors["Mkt-RF"]
         + p["s_smb"] * factors["SMB"] + p["h_hml"] * factors["HML"] + eps)
    extras = {
        "true_alpha": p["alpha"], "true_b_mkt": p["b_mkt"],
        "true_s_smb": p["s_smb"], "true_h_hml": p["h_hml"],
        "sample_start": str(factors.index[0]), "sample_end": str(factors.index[-1]),
    }
    return FamaFrenchModel(asset_returns=r.to_numpy(), factors=factors), extras


def _build_bsm(p: dict) -> BlackScholesModel:
    return BlackScholesModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        dividend_yield=p["dividend_yield"],
    )


def _build_crr(p: dict) -> BinomialTreeModel:
    return BinomialTreeModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        exercise=p["exercise"], dividend_yield=p["dividend_yield"],
        n_steps=int(p["n_steps"]),
    )


def _build_mc(p: dict) -> MonteCarloOptionModel:
    return MonteCarloOptionModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        dividend_yield=p["dividend_yield"], n_sims=int(p["n_sims"]),
        antithetic=bool(p["antithetic"]), seed=_SEED,
    )


def _build_hes(p: dict) -> HestonModel:
    return HestonModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"],
        maturity=p["maturity"], v0=p["v0"], kappa=p["kappa"],
        theta=p["theta"], xi=p["xi"], rho=p["rho"],
        option_type=p["option_type"],
    )


BUILDERS: dict[str, Callable[[dict], Any]] = {
    "DCF": _build_dcf, "GG": _build_gg, "MPT": _build_mpt, "VAR": _build_var,
    "CAPM": _build_capm, "FF3": _build_ff3, "BSM": _build_bsm,
    "CRR": _build_crr, "MC": _build_mc, "HES": _build_hes,
}


# --------------------------------------------------------------------------- #
# JSON sanitation: numpy scalars/arrays and non-finite floats -> JSON-safe
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (np.integer, int, bool)) or value is None:
        return value
    return str(value)


def run_model(mnemonic: str, params_json: str) -> str:
    """Build + run one model; returns a JSON payload for the front end."""
    params = json.loads(params_json)
    built = BUILDERS[mnemonic](params)
    model, extras = built if isinstance(built, tuple) else (built, {})

    t0 = time.perf_counter()
    results = model.calculate()
    calc_ms = (time.perf_counter() - t0) * 1000.0

    try:
        figure = model.visualize().to_json()
    except Exception as exc:  # chart failure must not take down the numbers
        figure = None
        extras = {**extras, "figure_error": str(exc)}

    return json.dumps({
        "ok": True,
        "results": _clean(results),
        "extras": _clean(extras),
        "explain": model.explain(),
        "figure": figure,
        "calc_ms": round(calc_ms, 2),
    })
