"""Capital Asset Pricing Model (CAPM).

Relates an asset's expected return to its systematic risk (``beta``) relative to
the market portfolio.

Formula (Sharpe, 1964; Lintner, 1965)::

    E[R_i] = r_f + beta_i * (E[R_m] - r_f)

where ``E[R_m] - r_f`` is the market risk premium. When ``beta`` is not supplied
it is estimated from realised returns by ordinary least squares::

    beta  = Cov(R_a, R_m) / Var(R_m)
    alpha = mean(R_a) - beta * mean(R_m)     (Jensen's alpha; excess returns if
                                              a periodic risk-free rate is given)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go


class CAPMModel(BaseFinancialModel):
    """Expected return from systematic risk, with optional beta estimation.

    Assumptions:
        * Investors hold mean-variance-efficient portfolios; only systematic
          (non-diversifiable) risk, measured by ``beta``, is priced.
        * A single-period, single-factor (market) world with a risk-free asset.

    Beta may be supplied directly, or estimated by OLS from equal-length
    ``asset_returns`` and ``market_returns`` (optionally in excess of a periodic
    risk-free rate).

    Example:
        >>> m = CAPMModel(risk_free_rate=0.03, expected_market_return=0.10, beta=1.2)
        >>> round(m.calculate()["expected_return"], 4)
        0.114
    """

    name = "Capital Asset Pricing Model"
    category = "Equity / Factor"
    references = [
        "Sharpe, W. F. (1964). Capital Asset Prices: A Theory of Market "
        "Equilibrium under Conditions of Risk. Journal of Finance, 19(3), 425-442.",
        "Lintner, J. (1965). The Valuation of Risk Assets and the Selection of "
        "Risky Investments in Stock Portfolios and Capital Budgets. Review of "
        "Economics and Statistics, 47(1), 13-37.",
    ]

    def __init__(
        self,
        *,
        risk_free_rate: float,
        expected_market_return: float,
        beta: float | None = None,
        asset_returns: Sequence[float] | np.ndarray | None = None,
        market_returns: Sequence[float] | np.ndarray | None = None,
        periodic_risk_free: float | Sequence[float] | np.ndarray | None = None,
        logger: Any = None,
    ) -> None:
        """Initialise and validate all inputs.

        Args:
            risk_free_rate: Risk-free rate ``r_f`` in the CAPM equation (finite).
            expected_market_return: Expected market return ``E[R_m]`` (finite).
            beta: Systematic risk ``beta`` (finite). If ``None``, it is estimated
                by OLS and ``asset_returns`` / ``market_returns`` are required.
            asset_returns: Realised asset returns (used only when estimating).
            market_returns: Realised market returns; must match the length of
                ``asset_returns``.
            periodic_risk_free: Optional per-period risk-free rate (scalar or an
                array matching the returns) used to form excess returns for
                estimation. When omitted, raw returns are used.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If inputs are invalid, if estimation is requested
                without both return series, if the series differ in length, or if
                the market return series has zero variance.
        """
        super().__init__(logger=logger)
        self.risk_free_rate = self._as_finite_float(risk_free_rate, "risk_free_rate")  # r_f
        self.expected_market_return = self._as_finite_float(
            expected_market_return, "expected_market_return"  # E[R_m]
        )
        self.asset_returns: np.ndarray | None = None
        self.market_returns: np.ndarray | None = None
        if beta is not None:
            # Beta supplied directly; no estimation, so alpha is undefined.
            self.beta = self._as_finite_float(beta, "beta")
            self.alpha: float | None = None
            self.estimated = False
        else:
            self.beta, self.alpha = self._estimate_beta_alpha(
                asset_returns, market_returns, periodic_risk_free
            )
            self.estimated = True
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Beta estimation
    # ------------------------------------------------------------------ #
    def _estimate_beta_alpha(
        self,
        asset_returns: Sequence[float] | np.ndarray | None,
        market_returns: Sequence[float] | np.ndarray | None,
        periodic_risk_free: float | Sequence[float] | np.ndarray | None,
    ) -> tuple[float, float]:
        """Estimate ``(beta, alpha)`` by OLS from realised returns.

        Args:
            asset_returns: Realised asset returns ``R_a``.
            market_returns: Realised market returns ``R_m`` (same length).
            periodic_risk_free: Optional periodic risk-free rate; when supplied,
                estimation uses excess returns ``R - r_f``.

        Returns:
            ``(beta, alpha)`` where ``beta = Cov(R_a, R_m)/Var(R_m)`` and
            ``alpha = mean(R_a) - beta*mean(R_m)`` (on excess returns if a
            periodic risk-free rate is given).

        Raises:
            ValidationError: If a series is missing, lengths differ, or the market
                return series has zero variance.
        """
        if asset_returns is None or market_returns is None:
            raise ValidationError(
                "When 'beta' is None, both 'asset_returns' and 'market_returns' "
                "must be supplied for OLS estimation."
            )
        r_a = self._as_float_array(asset_returns, "asset_returns")
        r_m = self._as_float_array(market_returns, "market_returns")
        if r_a.size != r_m.size:
            raise ValidationError(
                f"'asset_returns' (n={r_a.size}) and 'market_returns' "
                f"(n={r_m.size}) must have equal length."
            )
        if periodic_risk_free is not None:
            # Convert raw returns to excess returns over the periodic risk-free.
            rf = self._as_float_array(np.atleast_1d(periodic_risk_free), "periodic_risk_free")
            if rf.size not in (1, r_a.size):
                raise ValidationError(
                    "'periodic_risk_free' must be a scalar or match the number of "
                    f"return observations ({r_a.size}), got {rf.size}."
                )
            r_a = r_a - rf  # excess asset return
            r_m = r_m - rf  # excess market return
        self.asset_returns, self.market_returns = r_a, r_m
        a_mean, m_mean = r_a.mean(), r_m.mean()
        # Covariance / variance share ddof, so their ratio is ddof-independent.
        cov = float(np.mean((r_a - a_mean) * (r_m - m_mean)))  # Cov(R_a, R_m)
        var = float(np.mean((r_m - m_mean) ** 2))  # Var(R_m)
        if var == 0.0:
            raise ValidationError("'market_returns' has zero variance; beta is undefined.")
        beta = cov / var  # OLS slope
        alpha = float(a_mean - beta * m_mean)  # Jensen's alpha (intercept)
        return beta, alpha

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def expected_return(self) -> float:
        """Return the CAPM expected return ``E[R_i] = r_f + beta*(E[R_m]-r_f)``."""
        premium = self.expected_market_return - self.risk_free_rate  # market risk premium
        return self.risk_free_rate + self.beta * premium

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute beta, expected return, market risk premium and alpha.

        Returns:
            Dictionary with keys ``beta``, ``expected_return``,
            ``market_risk_premium`` and ``alpha`` (``None`` when ``beta`` was
            supplied rather than estimated).
        """
        premium = self.expected_market_return - self.risk_free_rate  # E[R_m] - r_f
        result: dict[str, Any] = {
            "beta": self.beta,
            "expected_return": self.expected_return(),  # r_f + beta*premium
            "market_risk_premium": premium,
            "alpha": self.alpha,
        }
        self._logger.info(
            "CAPM beta=%.6f -> expected return=%.6f (premium=%.6f)",
            self.beta, result["expected_return"], premium,
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        res = self.calculate()
        beta_line = (
            f"- Beta estimated by OLS from {self.asset_returns.size} observations "
            f"= {self.beta:.6f}, Jensen's alpha = {res['alpha']:.6f}\n"
            if self.estimated
            else f"- Beta supplied directly = {self.beta:.6f}\n"
        )
        return (
            "### Capital Asset Pricing Model\n\n"
            "In equilibrium only systematic risk is rewarded, so expected return "
            "is linear in beta along the Security Market Line:\n\n"
            r"$$E[R_i] = r_f + \beta_i\,(E[R_m] - r_f)$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- Inputs: r_f = {self.risk_free_rate}, E[R_m] = {self.expected_market_return}\n"
            f"{beta_line}"
            f"- Market risk premium = E[R_m] - r_f = {res['market_risk_premium']:.6f}\n"
            f"- **E[R] = {self.risk_free_rate} + {self.beta:.4f}·"
            f"{res['market_risk_premium']:.4f} = {res['expected_return']:.6f}**\n\n"
            "When estimating, beta is the slope of asset returns regressed on "
            "market returns; alpha is the intercept (out/under-performance)."
        )

    def visualize(self, *, beta_max: float = 2.0, **kwargs: Any) -> "go.Figure":
        """Plot the Security Market Line with the asset marked as a point.

        Args:
            beta_max: Upper limit of the beta axis (line spans ``0..beta_max``).

        Returns:
            A Plotly figure of the SML ``E[R] = r_f + beta*(E[R_m]-r_f)`` with the
            risk-free intercept at ``beta = 0`` and the asset plotted at its beta.
        """
        import plotly.graph_objects as go

        self._require_positive(beta_max, "beta_max")
        premium = self.expected_market_return - self.risk_free_rate
        betas = np.linspace(0.0, max(beta_max, self.beta * 1.2), 100)
        sml = self.risk_free_rate + betas * premium  # vectorised SML

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=betas, y=sml, name="Security Market Line",
                                 line=dict(width=3)))
        # Risk-free asset (beta = 0) and the market portfolio (beta = 1).
        fig.add_trace(go.Scatter(x=[0.0], y=[self.risk_free_rate], mode="markers",
                                 marker=dict(color="green", size=10),
                                 name="Risk-free (β=0)"))
        fig.add_trace(go.Scatter(x=[1.0], y=[self.expected_market_return], mode="markers",
                                 marker=dict(color="grey", size=10), name="Market (β=1)"))
        # The asset itself.
        fig.add_trace(go.Scatter(
            x=[self.beta], y=[self.expected_return()], mode="markers+text",
            marker=dict(color="red", size=13, symbol="star"),
            text=["asset"], textposition="top center", name="Asset"))
        fig.update_layout(
            title="Capital Asset Pricing Model — Security Market Line",
            xaxis_title="Beta (systematic risk)", yaxis_title="Expected return",
            template="plotly_white", hovermode="closest",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return the SML identity plus an exact beta-recovery cross-check."""
        # (a) Machine-precision SML value: 0.03 + 1.2*(0.10-0.03) = 0.114.
        direct = cls(risk_free_rate=0.03, expected_market_return=0.10, beta=1.2)
        er = direct.calculate()["expected_return"]
        # (b) Noise-free data: asset = 1.5 * market, so OLS beta must recover 1.5.
        market = np.array([0.01, -0.02, 0.03, 0.005, -0.015, 0.025, -0.01, 0.02])
        asset = 1.5 * market  # exactly linear, zero residual
        est = cls(risk_free_rate=0.03, expected_market_return=0.10,
                  asset_returns=asset, market_returns=market)
        return [
            Benchmark("SML value rf=0.03, beta=1.2, E[Rm]=0.10 -> 0.114", er, 0.114,
                      rel_tol=1e-12, source="Sharpe (1964)/Lintner (1965) SML identity"),
            Benchmark("OLS beta recovery (asset=1.5*market)", est.beta, 1.5,
                      rel_tol=1e-9, source="Exact OLS slope recovery (no noise)"),
        ]
