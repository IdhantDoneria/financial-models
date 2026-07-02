"""Financial Models — 10 canonical models with a shared, tested interface.

This package exposes ten financial models, each a subclass of
:class:`~src.base_model.BaseFinancialModel` implementing ``calculate``,
``explain`` and ``visualize``, plus a :class:`~src.scorer.ModelScorer` that grades
them on pedagogical clarity, numerical accuracy and production readiness.

Import the registry to iterate over every model::

    from src import ALL_MODELS
    for model_cls in ALL_MODELS:
        print(model_cls.name, model_cls.reference_benchmarks())
"""

from __future__ import annotations

from .base_model import BaseFinancialModel, Benchmark, ModelError, ValidationError
from .binomial import BinomialTreeModel
from .black_scholes import BlackScholesModel
from .capm import CAPMModel
from .dcf import DiscountedCashFlowModel
from .fama_french import FamaFrenchModel
from .gordon_growth import GordonGrowthModel
from .monte_carlo import MonteCarloOptionModel
from .mpt import ModernPortfolioTheoryModel
from .scorer import ModelScore, ModelScorer, score_all
from .stochastic_volatility import HestonModel
from .var_cvar import ValueAtRiskModel

__version__ = "1.0.0"

#: Every model class, ordered by category for the navigation UI.
ALL_MODELS: list[type[BaseFinancialModel]] = [
    DiscountedCashFlowModel,
    GordonGrowthModel,
    ModernPortfolioTheoryModel,
    ValueAtRiskModel,
    CAPMModel,
    FamaFrenchModel,
    BlackScholesModel,
    BinomialTreeModel,
    MonteCarloOptionModel,
    HestonModel,
]

__all__ = [
    "BaseFinancialModel", "Benchmark", "ModelError", "ValidationError",
    "BinomialTreeModel", "BlackScholesModel", "CAPMModel",
    "DiscountedCashFlowModel", "FamaFrenchModel", "GordonGrowthModel",
    "MonteCarloOptionModel", "ModernPortfolioTheoryModel", "HestonModel",
    "ValueAtRiskModel", "ModelScore", "ModelScorer", "score_all", "ALL_MODELS",
]
