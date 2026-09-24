"""Orchestrator: takes extracted data + assumptions + selected models, runs.

Wires the three earlier stages together. Given an :class:`AssumptionSet` and a
list of model names to run, produces an :class:`AnalysisReport` — the single
object the exporters (PDF / XLSX / Google Docs) consume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

from .. import (
    BinomialTreeModel, BlackScholesModel, CAPMModel, DiscountedCashFlowModel,
    FamaFrenchModel, GordonGrowthModel, HestonModel, IndASHiddenDebtModel,
    ModernPortfolioTheoryModel, MonteCarloOptionModel, ReverseDCFModel,
    ValueAtRiskModel,
)
from ..base_model import BaseFinancialModel
from .assumptions import AssumptionSet
from .pdf_extractor import ExtractedFinancials, PDFExtractor

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
    "Ind AS 116 Hidden-Debt Normalizer": IndASHiddenDebtModel,
    "Reverse DCF / Market-Implied Expectations": ReverseDCFModel,
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
        import pandas as pd

        currency_symbol = PDFExtractor.CURRENCY_SYMBOLS.get(
            self.company.currency, "$")
        rows = []
        for name, res in self.results.items():
            is_partial = name in self.assumptions.partial
            headline = self._headline(name, res, currency_symbol, partial=is_partial,
                                      price=self.company.current_price)
            status = ("OK" if not is_partial
                      else "PARTIAL" if name in self.assumptions.partly_assessed
                      else "UNASSESSED")
            rows.append({"Model": name, "Headline result": headline,
                         "Status": status})
        for name, err in self.errors.items():
            rows.append({"Model": name, "Headline result": "-", "Status": err})
        return pd.DataFrame(rows)

    #: DCF equity value / market cap outside this band draws a plain-language
    #: warning. It is deliberately wide: it flags a model that is far from the
    #: market, not one that merely disagrees with it.
    _DIVERGENCE_BAND = (0.4, 2.5)

    def divergence_note(self) -> str | None:
        """Say so when the DCF equity value is far from the market cap.

        A valuation 7x the market cap reads as a bargain, and 0.05x as a
        disaster, when the cause is usually the model's inputs (a base year
        that isn't representative, a growth rate the market doesn't share, or a
        capital structure that drags WACC down, as a captive finance arm's
        debt does). The number computes cleanly either way, so this states the
        gap instead of leaving the user to work it out."""
        dcf = self.results.get("Discounted Cash Flow")
        price, shares = self.company.current_price, self.company.shares_outstanding
        if not dcf or not price or not shares:
            return None
        equity = dcf.get("equity_value")
        if not isinstance(equity, (int, float, np.floating)):
            return None
        ratio = equity / (price * shares)
        lo, hi = self._DIVERGENCE_BAND
        if lo <= ratio <= hi:
            return None
        if ratio <= 0:
            gap = ("a negative equity value: the DCF's enterprise value is smaller than "
                   "the company's net debt")
        else:
            gap = f"{ratio:.2f}x the market capitalisation"
        return (f"The DCF equity value is {gap}. A gap this wide usually means the "
                "model's inputs differ from the market's: growth expectations (the "
                "Reverse DCF shows what the market is pricing in), a base year that "
                "isn't representative, or a capital structure the model reads badly "
                "(a captive finance arm's debt pulls WACC down). Treat the DCF as one "
                "input, not a price target.")

    #: Models whose headline unit switches from "$" to "%" when
    #: AssumptionSet.partial flags them — VaR/CVaR's dollar figure only
    #: means something with a real portfolio value; without one, the model
    #: still runs (on a $1 unit notional — see AutoAssumer.build) and the
    #: SAME numeric result is a real, meaningful %-of-portfolio loss instead.
    _PERCENT_WHEN_PARTIAL = {"Value at Risk / CVaR"}

    @staticmethod
    def _headline(
        name: str, res: dict[str, Any], currency_symbol: str = "$", partial: bool = False,
        price: float | None = None,
    ) -> str:
        """Pick the single most-useful number per model for the summary row.

        Args:
            currency_symbol: Prefix for a "$"-unit headline — the filing's
                own detected currency (:attr:`ExtractedFinancials.currency`),
                not necessarily USD. Defaults to "$" so any caller that
                hasn't been updated to pass it keeps today's behaviour.
            partial: Whether AssumptionSet.partial flags this model — see
                :attr:`_PERCENT_WHEN_PARTIAL`.
        """
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
            "Ind AS 116 Hidden-Debt Normalizer": ("adjusted_net_debt", "$"),
            "Reverse DCF / Market-Implied Expectations": ("implied_fcf_cagr", "%"),
        }
        # The DCF answers "what is a share worth?", so with a share count its
        # headline is value per share beside the price the user can compare it
        # to. A bare enterprise value ("$56,731,867,744.10") answered nothing
        # a reader could act on. Without shares, the enterprise value, labelled.
        if name == "Discounted Cash Flow":
            pps, ev = res.get("price_per_share"), res.get("enterprise_value")
            if isinstance(pps, (int, float, np.floating)):
                return (f"{currency_symbol}{pps:,.2f} / share"
                        + (f" · price {currency_symbol}{price:,.2f}" if price else ""))
            if isinstance(ev, (int, float, np.floating)):
                return f"{currency_symbol}{ev:,.0f} enterprise value"
        if name == "Reverse DCF / Market-Implied Expectations" and res.get("growth_profile") == "revenue":
            g = res.get("implied_revenue_cagr")
            return f"{g * 100:.2f}% revenue growth" if isinstance(g, (int, float)) else "-"
        key, unit = picks.get(name, (None, ""))
        if partial and name in AnalysisReport._PERCENT_WHEN_PARTIAL:
            unit = "%"
        if key and key in res:
            value = res[key]
            if isinstance(value, (int, float, np.floating)):
                if unit == "%":
                    return f"{value*100:.2f}%"
                if unit == "$":
                    return f"{currency_symbol}{value:,.2f}"
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
            # The assumer itself has already decided this model can't produce
            # a trustworthy result from what was actually disclosed (see
            # AssumptionSet.unavailable) — skip it rather than run it on
            # fabricated inputs.
            reason = assumptions.unavailable.get(name)
            if reason is not None:
                report.errors[name] = reason
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
        """Prepare Fama-French inputs: the company's REAL monthly returns on
        the matching factor months when the listing qualifies (see
        :func:`src.pipeline.assumptions.ff_real_returns`), else the
        disclosed synthetic illustration.

        A qualifying listing never silently falls back to the synthetic
        series: too few overlapping months raises instead, so the report
        shows the model as failed rather than an illustration presented
        under a "real regression" rationale.
        """
        from .assumptions import FF_MIN_MONTHS, ff_real_returns

        real = ff_real_returns(self.data)
        if real is not None:
            factors = FamaFrenchModel.load_factors()
            months = sorted(set(real) & {int(m) for m in factors.index})[-60:]
            if len(months) < FF_MIN_MONTHS:
                raise ValueError(
                    f"only {len(months)} months overlap between this company's "
                    f"returns and the bundled factor data (need {FF_MIN_MONTHS})")
            window = factors.loc[months]
            return {"asset_returns": np.array([real[m] for m in months]), "factors": window}

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
