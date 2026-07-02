"""Orchestrator: takes extracted data + assumptions + selected models, runs.

Wires the three earlier stages together. Given an :class:`AssumptionSet` and a
list of model names to run, produces an :class:`AnalysisReport` — the single
object the exporters (PDF / XLSX / Google Docs) consume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .. import (
    BinomialTreeModel, BlackScholesModel, CAPMModel, DiscountedCashFlowModel,
    FamaFrenchModel, GordonGrowthModel, HestonModel,
    ModernPortfolioTheoryModel, MonteCarloOptionModel, ValueAtRiskModel,
)
from ..base_model import BaseFinancialModel
from .assumptions import AssumptionSet
from .pdf_extractor import ExtractedFinancials

logger = logging.getLogger(__name__)

#: Registry of models the analyser can run, keyed by human name.
AVAILABLE_MODELS: dict[str, type[BaseFinancialModel]] = {
    "Discounted Cash Flow": DiscountedCashFlowModel,
    "Gordon Growth Model": GordonGrowthModel,
    "Modern Portfolio Theory": ModernPortfolioTheoryModel,
    "Value at Risk / CVaR": ValueAtRiskModel,
    "Capital Asset Pricing Model": CAPMModel,
    "Fama-French 3-Factor": FamaFrenchModel,
    "Black-Scholes-Merton": BlackScholesModel,
    "Binomial Tree (CRR)": BinomialTreeModel,
    "Monte Carlo (GBM)": MonteCarloOptionModel,
    "Heston Stochastic Volatility": HestonModel,
}


@dataclass
class AnalysisReport:
    """Single object collecting everything the exporters need.

    Attributes:
        company: Extracted financials (kept for the report header).
        assumptions: The set of numeric assumptions used in this run.
        results: ``{model_name: {result_key: value}}`` from each model.
        errors: ``{model_name: exception_string}`` for models that failed.
        mode: ``"auto"`` or ``"manual"`` — labelled in the exported header.
    """

    company: ExtractedFinancials
    assumptions: AssumptionSet
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    mode: str = "auto"

    def summary_frame(self) -> "pd.DataFrame":
        """Return a tidy DataFrame — one row per model with headline outputs."""
        rows = []
        for name, res in self.results.items():
            headline = self._headline(name, res)
            rows.append({"Model": name, "Headline result": headline,
                         "Status": "OK"})
        for name, err in self.errors.items():
            rows.append({"Model": name, "Headline result": "-", "Status": err})
        return pd.DataFrame(rows)

    @staticmethod
    def _headline(name: str, res: dict[str, Any]) -> str:
        """Pick the single most-useful number per model for the summary row."""
        picks = {
            "Discounted Cash Flow": ("enterprise_value", "$"),
            "Gordon Growth Model": ("price", "$"),
            "Modern Portfolio Theory": ("tangency_sharpe", ""),
            "Value at Risk / CVaR": ("var", "$"),
            "Capital Asset Pricing Model": ("expected_return", "%"),
            "Fama-French 3-Factor": ("r_squared", ""),
            "Black-Scholes-Merton": ("price", "$"),
            "Binomial Tree (CRR)": ("price", "$"),
            "Monte Carlo (GBM)": ("price", "$"),
            "Heston Stochastic Volatility": ("price", "$"),
        }
        key, unit = picks.get(name, (None, ""))
        if key and key in res:
            value = res[key]
            if isinstance(value, (int, float, np.floating)):
                if unit == "%":
                    return f"{value*100:.2f}%"
                if unit == "$":
                    return f"${value:,.2f}"
                return f"{value:.4f}"
        return str(next(iter(res.values()), "-"))


class AnalysisRunner:
    """Run a selected subset of models with a supplied :class:`AssumptionSet`.

    Fama-French has an out-of-band dependency (live factor data), so the runner
    fetches those factors here rather than in the assumer.
    """

    def __init__(self, extractor_data: ExtractedFinancials) -> None:
        self.data = extractor_data

    def run(
        self, assumptions: AssumptionSet, selected: list[str], mode: str = "auto"
    ) -> AnalysisReport:
        """Instantiate and run every selected model.

        Args:
            assumptions: Kwargs per model to feed into constructors.
            selected: Human-readable model names to run.
            mode: ``"auto"`` or ``"manual"`` — recorded on the report.

        Returns:
            A populated :class:`AnalysisReport`.
        """
        report = AnalysisReport(company=self.data, assumptions=assumptions, mode=mode)
        for name in selected:
            cls = AVAILABLE_MODELS.get(name)
            if cls is None:
                report.errors[name] = f"Unknown model: {name}"
                continue
            try:
                kwargs = dict(assumptions.kwargs_by_model.get(name, {}))
                if name == "Fama-French 3-Factor":
                    kwargs = self._build_ff_kwargs()
                elif name == "Value at Risk / CVaR":
                    kwargs.pop("returns", None)   # parametric path only
                model = cls(**kwargs)
                report.results[name] = self._sanitise(model.calculate())
            except Exception as exc:  # keep going even if one model fails
                logger.warning("Model %s failed: %s", name, exc)
                report.errors[name] = f"{type(exc).__name__}: {exc}"
        return report

    def _build_ff_kwargs(self) -> dict[str, Any]:
        """Prepare Fama-French inputs from live factor data + synthetic asset."""
        factors = FamaFrenchModel.load_factors().tail(120)   # last 10 years
        rng = np.random.default_rng(0)
        beta_hint = self.data.beta or 1.0
        asset = (factors["RF"].to_numpy()
                 + beta_hint * factors["Mkt-RF"].to_numpy()
                 + rng.normal(0, 0.005, len(factors)))
        return {"asset_returns": asset, "factors": factors}

    @staticmethod
    def _sanitise(res: dict[str, Any]) -> dict[str, Any]:
        """Flatten numpy scalars/arrays so the dict is JSON/PDF/Excel-safe."""
        out: dict[str, Any] = {}
        for k, v in res.items():
            if isinstance(v, np.ndarray):
                out[k] = v.tolist()
            elif isinstance(v, (np.floating, np.integer)):
                out[k] = float(v)
            else:
                out[k] = v
        return out
