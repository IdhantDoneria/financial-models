"""Assumption engines for the analyser: Auto (IB heuristic) and Manual (overrides).

Each model in :mod:`src` requires a specific set of numeric inputs. Some come
straight from the extracted financials; others (WACC, terminal growth,
volatility, correlations, …) must be *assumed* — either by an experienced
practitioner or by the software.

* :class:`AutoAssumer` implements the practitioner heuristic: it fills every
  missing input with an IB / hedge-fund-manager-style default (Damodaran-style
  WACC via CAPM, terminal g ≈ risk-free rate, sector-median betas, etc.).
* :class:`ManualAssumer` reads through :class:`ManualOverrides` — a plain
  dataclass populated by the ipywidgets sliders and text boxes in the notebook
  — and lets the user override any auto-derived value.

Both produce an :class:`AssumptionSet`: a single dict of ``{model_name:
{kwargs}}`` that :mod:`src.pipeline.runner` feeds into each model's constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pdf_extractor import ExtractedFinancials


# --------------------------------------------------------------------------- #
# Assumption container
# --------------------------------------------------------------------------- #
@dataclass
class AssumptionSet:
    """Per-model constructor kwargs, plus the market context they were built in.

    Attributes:
        kwargs_by_model: ``{"Discounted Cash Flow": {"free_cash_flows": [...],
            "discount_rate": 0.09, ...}, ...}``.
        market_context: The risk-free rate, market return and horizon actually
            used, kept for audit trails in the exported report.
        rationale: One-line human-readable justification per assumption, keyed
            by ``(model, param)``.
    """

    kwargs_by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    market_context: dict[str, float] = field(default_factory=dict)
    rationale: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class ManualOverrides:
    """User-supplied overrides from the notebook widgets.

    Every field is optional; ``None`` means "keep the auto value". Populated by
    ``ManualAssumer.from_widgets`` in the notebook.
    """

    risk_free_rate: float | None = None
    expected_market_return: float | None = None
    beta: float | None = None
    discount_rate: float | None = None           # WACC (DCF)
    terminal_growth: float | None = None
    dividend_growth: float | None = None         # Gordon
    volatility: float | None = None              # Options
    option_maturity: float | None = None
    strike_ratio: float | None = None            # strike / spot
    var_confidence: float | None = None
    var_horizon_days: int | None = None
    monte_carlo_paths: int | None = None
    heston_kappa: float | None = None
    heston_theta: float | None = None
    heston_xi: float | None = None
    heston_rho: float | None = None


# --------------------------------------------------------------------------- #
# Auto assumer
# --------------------------------------------------------------------------- #
class AutoAssumer:
    """Fill every missing model input with a practitioner-style default.

    Defaults are configurable — the defaults picked here mirror what Damodaran's
    Investment Valuation and typical sell-side desks use as base cases:

    * Risk-free rate: 4.25% (10Y US Treasury, editable).
    * Equity risk premium: 5% → expected market return = rf + ERP.
    * Sector-neutral beta: 1.0 when none is scraped.
    * WACC: CAPM cost of equity (equity 80% / debt 20% blend with 25% tax).
    * Terminal growth: min(rf, 2.5%) — never exceeds the risk-free rate.
    * Volatility: 25% annualised when nothing is scraped.
    * Heston params: literature "typical equity index" values (κ=1.5, θ=0.04,
      ξ=0.3, ρ=−0.6) tied to the annualised volatility guess.
    """

    def __init__(
        self,
        *,
        risk_free_rate: float = 0.0425,
        equity_risk_premium: float = 0.05,
        tax_rate: float = 0.25,
        target_equity_weight: float = 0.80,
        default_beta: float = 1.0,
        default_volatility: float = 0.25,
    ) -> None:
        self.rf = risk_free_rate
        self.erp = equity_risk_premium
        self.tax = tax_rate
        self.we = target_equity_weight
        self.wd = 1 - target_equity_weight
        self.default_beta = default_beta
        self.default_vol = default_volatility

    # ------------------------------------------------------------------ #
    def _wacc(self, beta: float) -> float:
        """Weighted average cost of capital: E/V * ke + D/V * kd * (1-t)."""
        cost_of_equity = self.rf + beta * self.erp
        cost_of_debt = self.rf + 0.015   # +150bp credit spread over risk-free
        return self.we * cost_of_equity + self.wd * cost_of_debt * (1 - self.tax)

    def build(
        self, data: ExtractedFinancials, overrides: ManualOverrides | None = None
    ) -> AssumptionSet:
        """Produce a full :class:`AssumptionSet` from extracted data + overrides.

        Args:
            data: Financials scraped from the PDF (some fields may be ``None``).
            overrides: Optional per-parameter overrides from the manual UI.

        Returns:
            A populated :class:`AssumptionSet` covering all ten models.
        """
        o = overrides or ManualOverrides()
        rf = o.risk_free_rate if o.risk_free_rate is not None else self.rf
        beta = o.beta if o.beta is not None else (
            data.beta if data.beta is not None else self.default_beta)
        erm = o.expected_market_return if o.expected_market_return is not None \
            else rf + self.erp
        wacc = o.discount_rate if o.discount_rate is not None else self._wacc(beta)
        # Terminal growth cannot exceed the risk-free rate (Gordon constraint).
        g_terminal = o.terminal_growth if o.terminal_growth is not None else min(rf, 0.025)
        vol = o.volatility if o.volatility is not None else self.default_vol
        # Fabricated FCF trajectory: revenue × margin × (1+g)^t when actual FCFs missing.
        fcfs = data.free_cash_flows or self._synth_fcfs(data, wacc)
        spot = data.current_price or 100.0  # normalised units when unknown
        strike = o.strike_ratio * spot if o.strike_ratio else spot
        # Dividend for Gordon: use scraped DPS or 2% of price as a default.
        dividend = data.dividend_per_share or 0.02 * spot
        g_div = o.dividend_growth if o.dividend_growth is not None else 0.03

        kwargs: dict[str, dict[str, Any]] = {
            "Discounted Cash Flow": {
                "free_cash_flows": fcfs,
                "discount_rate": wacc,
                "terminal_growth": g_terminal,
                "net_debt": data.net_debt or 0.0,
                "shares_outstanding": data.shares_outstanding or None,
            },
            "Gordon Growth Model": {
                "dividend": dividend,
                "required_return": max(wacc, g_div + 0.005),
                "growth": g_div,
                "dividend_is_forward": False,
            },
            "Modern Portfolio Theory": {
                # Two-asset proxy: the target company + broad market benchmark.
                "expected_returns": [rf + beta * self.erp, rf + self.erp],
                "covariance": [[vol**2, 0.6 * vol * 0.18],
                               [0.6 * vol * 0.18, 0.18**2]],
                "risk_free_rate": rf,
            },
            "Value at Risk / CVaR": {
                "mean": (rf + beta * self.erp) / 252,
                "std": vol / (252**0.5),
                "confidence_level": o.var_confidence if o.var_confidence is not None else 0.95,
                "horizon_days": o.var_horizon_days if o.var_horizon_days is not None else 10,
                "portfolio_value": (data.current_price or 100.0) * (data.shares_outstanding or 1_000_000),
                "method": "parametric",
            },
            "Capital Asset Pricing Model": {
                "risk_free_rate": rf,
                "expected_market_return": erm,
                "beta": beta,
            },
            "Fama-French 3-Factor": {"_needs_factor_data": True},   # runner handles this
            "Black-Scholes-Merton": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call",
            },
            "Binomial Tree (CRR)": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call", "exercise": "american", "n_steps": 500,
            },
            "Monte Carlo (GBM)": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call",
                "n_sims": o.monte_carlo_paths if o.monte_carlo_paths is not None else 100_000,
                "seed": 42,
            },
            "Heston Stochastic Volatility": {
                "spot": spot, "strike": strike, "rate": rf,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "v0": vol**2,
                "kappa": o.heston_kappa if o.heston_kappa is not None else 1.5,
                "theta": o.heston_theta if o.heston_theta is not None else vol**2,
                "xi": o.heston_xi if o.heston_xi is not None else 0.3,
                "rho": o.heston_rho if o.heston_rho is not None else -0.6,
                "option_type": "call",
            },
        }

        rationale: dict[tuple[str, str], str] = {}
        rationale[("DCF", "discount_rate")] = (
            f"WACC via CAPM: {self.we:.0%} equity @ (rf {rf:.2%} + β {beta:.2f}·ERP "
            f"{self.erp:.2%}) + {self.wd:.0%} debt @ (rf+150bp)·(1-{self.tax:.0%})."
        )
        rationale[("DCF", "terminal_growth")] = (
            f"Capped at min(rf={rf:.2%}, 2.5%) — Gordon constraint g < r."
        )
        rationale[("CAPM", "beta")] = (
            "Scraped from PDF." if data.beta else f"Sector-neutral default = {beta}."
        )
        return AssumptionSet(
            kwargs_by_model=kwargs,
            market_context={"risk_free_rate": rf, "expected_market_return": erm,
                            "beta": beta, "volatility": vol, "wacc": wacc,
                            "terminal_growth": g_terminal},
            rationale=rationale,
        )

    def _synth_fcfs(self, data: ExtractedFinancials, wacc: float) -> list[float]:
        """Fabricate a 5-year FCF projection when the PDF has none."""
        if data.revenue is None:
            base = 100.0                                     # placeholder units
        else:
            margin = data.operating_margin or 0.15           # 15% FCF margin default
            base = data.revenue * margin
        g = data.revenue_growth or 0.05                      # 5% growth default
        return [base * (1 + g) ** t for t in range(1, 6)]


# --------------------------------------------------------------------------- #
# Manual assumer
# --------------------------------------------------------------------------- #
class ManualAssumer:
    """Manual mode: use auto defaults as a base, then overlay every override.

    Practically ``ManualAssumer`` is a thin wrapper around :class:`AutoAssumer`
    that always applies the user's :class:`ManualOverrides`. Kept as a distinct
    class so the notebook UI (and the exported report) can label which mode was
    used without inspection.
    """

    def __init__(self, auto: AutoAssumer | None = None) -> None:
        self.auto = auto or AutoAssumer()

    def build(
        self, data: ExtractedFinancials, overrides: ManualOverrides
    ) -> AssumptionSet:
        """Return an assumption set with every user override applied."""
        return self.auto.build(data, overrides=overrides)
