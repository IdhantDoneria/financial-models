"""Shared pytest fixtures: representative, valid instances of every model."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

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


def _sample_returns(seed: int = 1, n: int = 240) -> tuple[np.ndarray, pd.DataFrame]:
    """Build a small aligned (asset_returns, factors) pair for factor models."""
    rng = np.random.default_rng(seed)
    mkt = rng.normal(0.008, 0.04, n)
    smb = rng.normal(0.0, 0.03, n)
    hml = rng.normal(0.0, 0.03, n)
    rf = np.full(n, 0.002)
    asset = rf + 0.9 * mkt + 0.3 * smb - 0.2 * hml + rng.normal(0, 0.01, n)
    factors = pd.DataFrame({"Mkt-RF": mkt, "SMB": smb, "HML": hml, "RF": rf})
    return asset, factors


def make_instance(cls: type) -> object:
    """Return a representative, valid instance of ``cls`` for interface tests."""
    asset, factors = _sample_returns()
    builders = {
        DiscountedCashFlowModel: lambda: cls(
            free_cash_flows=[100, 110, 121, 133], discount_rate=0.10,
            terminal_growth=0.03, net_debt=50, shares_outstanding=100),
        GordonGrowthModel: lambda: cls(dividend=3.0, required_return=0.09, growth=0.04),
        ModernPortfolioTheoryModel: lambda: cls(
            expected_returns=[0.10, 0.15, 0.08],
            covariance=[[0.04, 0.006, 0.0], [0.006, 0.09, 0.01], [0.0, 0.01, 0.02]],
            risk_free_rate=0.03),
        ValueAtRiskModel: lambda: cls(
            returns=np.random.default_rng(0).normal(0.0005, 0.02, 1000),
            confidence_level=0.99, method="historical", portfolio_value=1_000_000),
        CAPMModel: lambda: cls(risk_free_rate=0.03, expected_market_return=0.10, beta=1.2),
        FamaFrenchModel: lambda: cls(asset_returns=asset, factors=factors),
        BlackScholesModel: lambda: cls(spot=42, strike=40, rate=0.10, sigma=0.20,
                                       maturity=0.5, option_type="call"),
        BinomialTreeModel: lambda: cls(spot=42, strike=40, rate=0.10, sigma=0.20,
                                       maturity=0.5, n_steps=300),
        MonteCarloOptionModel: lambda: cls(spot=42, strike=40, rate=0.10, sigma=0.20,
                                           maturity=0.5, n_sims=50_000, seed=1),
        HestonModel: lambda: cls(spot=100, strike=100, rate=0.02, maturity=1.0,
                                 v0=0.04, kappa=1.5, theta=0.04, xi=0.3, rho=-0.6),
    }
    return builders[cls]()


@pytest.fixture
def bs_call() -> BlackScholesModel:
    """Hull Example 15.6 call option."""
    return BlackScholesModel(spot=42, strike=40, rate=0.10, sigma=0.20,
                             maturity=0.5, option_type="call")
