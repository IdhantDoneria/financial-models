"""Value-at-Risk (VaR) and Conditional VaR (Expected Shortfall) estimation.

Provides three interchangeable estimators of downside risk, expressed as
positive loss amounts scaled by the portfolio value ``V``. Given a confidence
level ``c`` (e.g. ``0.95``), the lower tail probability is ``alpha = 1 - c``.

Formulae (losses are reported as positive numbers). Under the i.i.d. returns
assumption, the ``h``-day mean scales as ``mu * h`` while the ``h``-day std
scales as ``sigma * sqrt(h)`` (square-root-of-time on the *dispersion* only,
never on the drift):

* **Parametric (Gaussian)** with per-period mean ``mu``, std ``sigma`` and
  horizon ``h`` (with ``z = N^{-1}(alpha)`` and ``phi`` the standard-normal
  pdf)::

      VaR  = -(mu * h + z * sigma * sqrt(h)) * V
      CVaR = -(mu * h - sigma * sqrt(h) * phi(z) / alpha) * V

* **Historical** on an empirical per-period return sample ``r``: the
  alpha-quantile is de-meaned, scaled by ``sqrt(h)``, then re-centred on the
  ``h``-period mean (``mean(r) * h``) -- the same drift/dispersion split as
  the parametric method, so the two agree on Gaussian data::

      mean = mean(r)
      q    = quantile(r, alpha)
      VaR  = -(mean * h + (q - mean) * sqrt(h)) * V
      tail_mean = mean(r[r <= q])
      CVaR = -(mean * h + (tail_mean - mean) * sqrt(h)) * V

* **Monte Carlo**: draw ``n_sims`` i.i.d. samples from ``Normal(mu, sigma)``
  with a seeded generator, then apply the historical formulae above
  (including the drift/dispersion horizon scaling) to the draws.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from scipy.stats import norm

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go

VaRMethod = Literal["historical", "parametric", "monte_carlo"]
_METHODS: tuple[str, ...] = ("historical", "parametric", "monte_carlo")


class ValueAtRiskModel(BaseFinancialModel):
    """Estimate Value-at-Risk and Conditional VaR (Expected Shortfall).

    The model accepts *either* an empirical ``returns`` sample *or* a
    ``mean``/``std`` pair (parametric / Monte-Carlo only). Losses are quoted as
    positive numbers scaled by ``portfolio_value``.

    Assumptions:
        * Returns are stationary over the estimation window.
        * All three methods scale multi-day horizons with drift scaling as
          ``h`` and dispersion scaling as ``sqrt(h)`` (i.i.d. returns); the
          parametric estimator additionally assumes normally-distributed
          returns.

    Example:
        >>> m = ValueAtRiskModel(mean=0.0, std=1.0, confidence_level=0.99,
        ...                      method="parametric")
        >>> round(m.calculate()["var"], 6)
        2.326348
    """

    name = "Value at Risk / CVaR"
    category = "Risk"
    references = [
        "Jorion, P. (2007). Value at Risk: The New Benchmark for Managing "
        "Financial Risk, 3rd ed. McGraw-Hill.",
        "Artzner, P., Delbaen, F., Eber, J.-M. & Heath, D. (1999). Coherent "
        "Measures of Risk. Mathematical Finance, 9(3), 203-228.",
    ]

    def __init__(
        self,
        *,
        returns: Any = None,
        mean: float | None = None,
        std: float | None = None,
        confidence_level: float = 0.95,
        horizon_days: int = 1,
        portfolio_value: float = 1.0,
        method: VaRMethod = "historical",
        seed: int = 42,
        n_sims: int = 100_000,
        logger: Any = None,
    ) -> None:
        """Initialise and validate the risk-model inputs.

        Args:
            returns: Optional 1-D array-like of historical returns (>= 2 points).
            mean: Distribution mean ``mu`` (required when ``returns`` is omitted).
            std: Distribution std ``sigma`` > 0 (required when ``returns`` omitted).
            confidence_level: Confidence ``c`` strictly in ``(0, 1)``.
            horizon_days: Positive integer holding period ``h``; all three
                methods scale drift by ``h`` and dispersion by ``sqrt(h)``.
            portfolio_value: Positive portfolio value ``V`` scaling the loss.
            method: One of ``"historical"``, ``"parametric"`` or ``"monte_carlo"``.
            seed: Seed for the Monte-Carlo random generator.
            n_sims: Number of Monte-Carlo draws (positive integer).
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If inputs are missing, out of domain, or the chosen
                ``method`` is incompatible with the supplied data.
        """
        super().__init__(logger=logger)
        if method not in _METHODS:
            raise ValidationError(
                f"'method' must be one of {_METHODS}, got {method!r}."
            )
        self.method: VaRMethod = method
        # c in (0, 1) strictly => alpha = 1 - c is a valid tail probability.
        self.confidence_level = self._require_open_unit(confidence_level, "confidence_level")
        self.horizon_days = self._require_positive_int(horizon_days, "horizon_days")
        self.portfolio_value = self._require_positive(portfolio_value, "portfolio_value")
        self.n_sims = self._require_positive_int(n_sims, "n_sims")
        self.seed = self._require_int(seed, "seed")

        # Resolve the return sample and/or the (mu, sigma) distribution params.
        if returns is not None:
            self.returns: np.ndarray | None = self._as_float_array(returns, "returns")
            if self.returns.size < 2:
                raise ValidationError("'returns' must contain at least 2 observations.")
            self._mu = float(np.mean(self.returns))            # sample mean mu
            self._sigma = float(np.std(self.returns, ddof=1))  # sample std sigma
        else:
            self.returns = None
            if method == "historical":
                raise ValidationError(
                    "The 'historical' method requires a 'returns' sample."
                )
            if mean is None or std is None:
                raise ValidationError(
                    "Provide 'returns', or both 'mean' and 'std' for "
                    "parametric / monte_carlo methods."
                )
            self._mu = self._as_finite_float(mean, "mean")     # supplied mu
            self._sigma = self._require_positive(std, "std")   # supplied sigma
        self._logger.debug("Initialised %r (mu=%.6g, sigma=%.6g)", self, self._mu, self._sigma)

    # ------------------------------------------------------------------ #
    # Extra validators
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_open_unit(value: float, name: str) -> float:
        """Validate that ``value`` lies strictly in the open interval ``(0, 1)``."""
        v = BaseFinancialModel._as_finite_float(value, name)
        if not (0.0 < v < 1.0):
            raise ValidationError(f"{name!r} must be in (0, 1), got {v!r}.")
        return v

    @staticmethod
    def _require_int(value: int, name: str) -> int:
        """Validate that ``value`` is an integer (accepts integral floats)."""
        if isinstance(value, bool) or not isinstance(value, (int, np.integer, float)):
            raise ValidationError(f"{name!r} must be an integer, got {value!r}.")
        if isinstance(value, float) and not value.is_integer():
            raise ValidationError(f"{name!r} must be an integer, got {value!r}.")
        return int(value)

    @classmethod
    def _require_positive_int(cls, value: int, name: str) -> int:
        """Validate that ``value`` is a strictly positive integer."""
        v = cls._require_int(value, name)
        if v <= 0:
            raise ValidationError(f"{name!r} must be a positive integer, got {v!r}.")
        return v

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def _parametric(self, mu: float, sigma: float) -> tuple[float, float]:
        """Return ``(VaR, CVaR)`` under the Gaussian parametric model.

        The ``h``-day mean scales as ``mu * h`` (drift accumulates linearly)
        while the ``h``-day std scales as ``sigma * sqrt(h)`` (dispersion
        under i.i.d. returns) -- these are NOT the same power of ``h``, so
        the drift and the dispersion term are scaled separately.

        Args:
            mu: Per-period return mean.
            sigma: Per-period return standard deviation.

        Returns:
            Tuple ``(var, cvar)`` as positive loss amounts.
        """
        alpha = 1.0 - self.confidence_level          # lower-tail probability
        z = norm.ppf(alpha)                          # z = N^{-1}(alpha) < 0
        h = self.horizon_days
        sqrt_h = np.sqrt(h)                           # sqrt(h) scales sigma only
        var = -(mu * h + z * sigma * sqrt_h) * self.portfolio_value
        # CVaR uses the truncated-normal mean: phi(z)/alpha, scaled like sigma.
        cvar = -(mu * h - sigma * sqrt_h * norm.pdf(z) / alpha) * self.portfolio_value
        return float(var), float(cvar)

    def _empirical(self, sample: np.ndarray) -> tuple[float, float]:
        """Return ``(VaR, CVaR)`` from an empirical/simulated return sample.

        ``sample`` holds per-period (1-day) returns. As in ``_parametric``,
        the horizon scaling is split between drift and dispersion: the
        sample mean scales as ``mean * h`` while the de-meaned quantile (and
        de-meaned tail mean) scale as ``sqrt(h)``, so historical and
        parametric VaR/CVaR agree on Gaussian data.

        Args:
            sample: 1-D array of per-period returns.

        Returns:
            Tuple ``(var, cvar)`` as positive loss amounts.
        """
        alpha = 1.0 - self.confidence_level          # lower-tail probability
        mean = float(np.mean(sample))                 # per-period sample mean
        q = float(np.quantile(sample, alpha))          # alpha-quantile of returns
        tail = sample[sample <= q]                     # losses at or beyond the quantile
        tail_mean = float(np.mean(tail))
        h = self.horizon_days
        sqrt_h = np.sqrt(h)                            # sqrt(h) scales the de-meaned
                                                         # quantile/tail-mean only
        var = -(mean * h + (q - mean) * sqrt_h) * self.portfolio_value
        cvar = -(mean * h + (tail_mean - mean) * sqrt_h) * self.portfolio_value
        return float(var), float(cvar)

    def _simulate(self, seed: int, n_sims: int) -> np.ndarray:
        """Draw ``n_sims`` Gaussian returns with a seeded generator."""
        rng = np.random.default_rng(seed)
        return rng.normal(self._mu, self._sigma, size=n_sims)  # Normal(mu, sigma)

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute VaR and CVaR using the configured ``method``.

        Args:
            **kwargs: Optional ``seed`` / ``n_sims`` overrides for Monte-Carlo.

        Returns:
            Dict with ``var``, ``cvar``, ``confidence_level``, ``method``,
            ``mean`` and ``std``.
        """
        if self.method == "parametric":
            var, cvar = self._parametric(self._mu, self._sigma)
        elif self.method == "historical":
            assert self.returns is not None  # guaranteed by __init__ validation
            var, cvar = self._empirical(self.returns)
        else:  # monte_carlo
            seed = self._require_int(kwargs.get("seed", self.seed), "seed")
            n_sims = self._require_positive_int(kwargs.get("n_sims", self.n_sims), "n_sims")
            var, cvar = self._empirical(self._simulate(seed, n_sims))

        result: dict[str, Any] = {
            "var": var,
            "cvar": cvar,
            "confidence_level": self.confidence_level,
            "method": self.method,
            "mean": self._mu,
            "std": self._sigma,
        }
        self._logger.info(
            "%s VaR=%.6f, CVaR=%.6f at %.0f%% confidence",
            self.method, var, cvar, 100.0 * self.confidence_level,
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        res = self.calculate()
        return (
            f"### Value at Risk & CVaR — {self.method.replace('_', ' ').title()}\n\n"
            "With confidence $c$ and tail probability $\\alpha = 1 - c$, VaR is the "
            "loss quantile and CVaR (expected shortfall) is the mean loss beyond it. "
            "Drift scales linearly with the horizon while dispersion scales with "
            "$\\sqrt{h}$ (i.i.d. returns), so the Gaussian closed form (with "
            "$z = N^{-1}(\\alpha)$ and pdf $\\phi$) is:\n\n"
            r"$$\mathrm{VaR} = -(\mu h + z\,\sigma\sqrt{h})\,V, \qquad "
            r"\mathrm{CVaR} = -\left(\mu h - \sigma\sqrt{h}\,\frac{\phi(z)}{\alpha}\right)"
            r"V$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- Method: {self.method}, confidence c={self.confidence_level}, "
            f"alpha={1 - self.confidence_level:.4f}\n"
            f"- mu={res['mean']:.6g}, sigma={res['std']:.6g}, "
            f"horizon={self.horizon_days}d, V={self.portfolio_value}\n"
            f"- **VaR = {res['var']:.6f}**, **CVaR = {res['cvar']:.6f}** "
            "(positive = loss)\n\n"
            "Cross-check: CVaR >= VaR always, since expected shortfall averages "
            "losses in the tail beyond the VaR threshold."
        )

    def visualize(self, bins: int = 60, **kwargs: Any) -> "go.Figure":
        """Plot the return/P&L distribution with VaR and CVaR cutoffs.

        Args:
            bins: Number of histogram bins.

        Returns:
            A Plotly figure: a histogram of the (observed or simulated) returns
            with vertical lines at the ``-VaR`` and ``-CVaR`` return thresholds.
        """
        import plotly.graph_objects as go

        res = self.calculate()
        # Build the sample to display for each method.
        if self.returns is not None:
            sample = self.returns
        else:  # parametric with only (mu, sigma) -> simulate for display
            sample = self._simulate(self.seed, self.n_sims)
        # Cutoffs live in return space: threshold = -loss / V.
        var_cut = -res["var"] / self.portfolio_value
        cvar_cut = -res["cvar"] / self.portfolio_value

        fig = go.Figure()
        fig.add_trace(go.Histogram(x=sample, nbinsx=int(bins), name="Returns",
                                   opacity=0.75))
        fig.add_vline(x=var_cut, line_dash="dash", line_color="orange",
                      annotation_text=f"-VaR ({res['var']:.4f})")
        fig.add_vline(x=cvar_cut, line_dash="dot", line_color="red",
                      annotation_text=f"-CVaR ({res['cvar']:.4f})")
        fig.update_layout(
            title=f"{self.method.replace('_', ' ').title()} return distribution "
                  f"({100.0 * self.confidence_level:.0f}% VaR/CVaR)",
            xaxis_title="Return", yaxis_title="Frequency",
            template="plotly_white", bargap=0.02,
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return standard-normal parametric identities used to grade accuracy."""
        # Standard normal, 99% confidence, unit exposure, 1-day horizon.
        m = cls(mean=0.0, std=1.0, confidence_level=0.99, horizon_days=1,
                portfolio_value=1.0, method="parametric")
        res = m.calculate()
        alpha = 0.01
        # VaR = -N^{-1}(0.01) = 2.326347...  (independent scipy computation).
        var_expected = float(-norm.ppf(alpha))
        # CVaR = phi(N^{-1}(0.01)) / 0.01  for the standard normal.
        cvar_expected = float(norm.pdf(norm.ppf(alpha)) / alpha)
        return [
            Benchmark("Standard-normal 99% parametric VaR",
                      float(res["var"]), var_expected, rel_tol=1e-12,
                      source="Jorion (2007); VaR = -N^{-1}(1-c) for N(0,1)"),
            Benchmark("Standard-normal 99% parametric CVaR",
                      float(res["cvar"]), cvar_expected, rel_tol=1e-12,
                      source="Jorion (2007); CVaR = phi(N^{-1}(alpha))/alpha for N(0,1)"),
        ]
