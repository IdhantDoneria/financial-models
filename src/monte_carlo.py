"""Monte Carlo option pricing under geometric Brownian motion.

Prices European options by simulating terminal underlying prices and discounting
the average payoff, then benchmarks the estimate against the analytical
Black-Scholes value to demonstrate convergence.

Risk-neutral dynamics (Boyle, 1977)::

    S_T = S_0 * exp[(r - q - sigma^2 / 2) T + sigma * sqrt(T) * Z],   Z ~ N(0, 1)
    price = e^{-rT} * E[max(S_T - K, 0)]   (call)

Variance reduction via antithetic variates (pairing ``Z`` with ``-Z``) is
supported and enabled by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from scipy.stats import norm

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    import plotly.graph_objects as go

OptionType = Literal["call", "put"]


class MonteCarloOptionModel(BaseFinancialModel):
    """Price European options by Monte Carlo simulation of GBM terminal prices.

    Assumptions:
        * Risk-neutral geometric Brownian motion with constant ``sigma`` and
          continuous dividend yield ``q``.
        * The estimator is unbiased; its standard error shrinks as
          ``1/sqrt(n_sims)`` (or faster with antithetic variates).

    Example:
        >>> m = MonteCarloOptionModel(spot=42, strike=40, rate=0.10, sigma=0.20,
        ...                           maturity=0.5, n_sims=200_000, seed=7)
        >>> res = m.calculate()
        >>> abs(res["price"] - res["analytical_price"]) < 3 * res["std_error"]
        True
    """

    name = "Monte Carlo (GBM)"
    category = "Derivatives"
    references = [
        "Boyle, P. P. (1977). Options: A Monte Carlo Approach. Journal of "
        "Financial Economics, 4(3), 323-338.",
        "Glasserman, P. (2003). Monte Carlo Methods in Financial Engineering. "
        "Springer. (Antithetic variates, Ch. 4.)",
    ]

    def __init__(
        self,
        *,
        spot: float,
        strike: float,
        rate: float,
        sigma: float,
        maturity: float,
        option_type: OptionType = "call",
        dividend_yield: float = 0.0,
        n_sims: int = 100_000,
        antithetic: bool = True,
        seed: int | None = 42,
        logger: Any = None,
    ) -> None:
        """Initialise and validate simulation inputs.

        Args:
            spot: Current underlying price ``S > 0``.
            strike: Strike price ``K > 0``.
            rate: Continuously-compounded risk-free rate ``r``.
            sigma: Annualised volatility ``sigma > 0``.
            maturity: Time to expiry in years ``T > 0``.
            option_type: ``"call"`` or ``"put"``.
            dividend_yield: Continuous dividend yield ``q >= 0``.
            n_sims: Number of Monte Carlo paths (>= 1000 recommended).
            antithetic: If ``True``, use antithetic variates for variance reduction.
            seed: Seed for the NumPy random generator (``None`` = nondeterministic).

        Raises:
            ValidationError: If any input is outside its valid domain.
        """
        super().__init__(logger=logger)
        self.spot = self._require_positive(spot, "spot")
        self.strike = self._require_positive(strike, "strike")
        self.rate = self._as_finite_float(rate, "rate")
        self.sigma = self._require_positive(sigma, "sigma")
        self.maturity = self._require_positive(maturity, "maturity")
        self.dividend_yield = self._require_nonnegative(dividend_yield, "dividend_yield")
        if option_type not in ("call", "put"):
            raise ValidationError(f"'option_type' must be 'call'/'put', got {option_type!r}.")
        self.option_type: OptionType = option_type
        n = int(n_sims)
        if n < 100:
            raise ValidationError(f"'n_sims' must be >= 100, got {n_sims!r}.")
        self.n_sims = n
        self.antithetic = bool(antithetic)
        self.seed = seed

    # ------------------------------------------------------------------ #
    def _simulate_terminal(self, n: int) -> np.ndarray:
        """Return ``n`` risk-neutral terminal prices ``S_T`` (vectorised).

        Args:
            n: Number of draws (rounded to even when antithetic).

        Returns:
            1-D array of simulated terminal underlying prices.
        """
        rng = np.random.default_rng(self.seed)
        drift = (self.rate - self.dividend_yield - 0.5 * self.sigma**2) * self.maturity
        diffusion = self.sigma * np.sqrt(self.maturity)
        if self.antithetic:
            half = (n + 1) // 2
            z_half = rng.standard_normal(half)
            z = np.concatenate([z_half, -z_half])  # antithetic pairs
        else:
            z = rng.standard_normal(n)
        return self.spot * np.exp(drift + diffusion * z)

    def _discounted_payoff(self, terminal: np.ndarray) -> np.ndarray:
        """Return per-path discounted payoffs for the configured option type."""
        if self.option_type == "call":
            payoff = np.maximum(terminal - self.strike, 0.0)
        else:
            payoff = np.maximum(self.strike - terminal, 0.0)
        return np.exp(-self.rate * self.maturity) * payoff

    def analytical_price(self) -> float:
        """Return the closed-form Black-Scholes price (the convergence target)."""
        vst = self.sigma * np.sqrt(self.maturity)
        d1 = (np.log(self.spot / self.strike)
              + (self.rate - self.dividend_yield + 0.5 * self.sigma**2) * self.maturity) / vst
        d2 = d1 - vst
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)
        if self.option_type == "call":
            return float(self.spot * disc_q * norm.cdf(d1) - self.strike * disc * norm.cdf(d2))
        return float(self.strike * disc * norm.cdf(-d2) - self.spot * disc_q * norm.cdf(-d1))

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Run the simulation and return the price estimate with its error bounds.

        Returns:
            Dict with ``price`` (MC estimate), ``std_error``, a 95% ``ci`` tuple,
            the ``analytical_price`` target and the absolute ``abs_error``.
        """
        discounted = self._discounted_payoff(self._simulate_terminal(self.n_sims))
        price = float(discounted.mean())
        # Standard error of the mean: sample std / sqrt(n).
        std_error = float(discounted.std(ddof=1) / np.sqrt(discounted.size))
        analytical = self.analytical_price()
        result = {
            "price": price,
            "std_error": std_error,
            "ci": (price - 1.96 * std_error, price + 1.96 * std_error),
            "analytical_price": analytical,
            "abs_error": abs(price - analytical),
            "n_sims": self.n_sims,
        }
        self._logger.info(
            "MC price=%.6f (SE=%.6f) vs analytical=%.6f", price, std_error, analytical
        )
        return result

    def explain(self) -> str:
        """Return a Markdown explanation with the current worked example."""
        res = self.calculate()
        return (
            f"### Monte Carlo Pricing — {self.option_type.title()}\n\n"
            "Under the risk-neutral measure the underlying is log-normal:\n\n"
            r"$$S_T = S_0\,\exp\!\Big[(r-q-\tfrac12\sigma^2)T + \sigma\sqrt{T}\,Z\Big],"
            r"\quad Z\sim N(0,1)$$"
            "\n\nThe option value is the discounted expected payoff, estimated by the "
            "sample mean over simulated paths:\n\n"
            r"$$\hat{C} = e^{-rT}\frac{1}{N}\sum_{i=1}^{N}\max(S_T^{(i)}-K,0)$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- N = {self.n_sims:,} paths, antithetic = {self.antithetic}\n"
            f"- MC price = **{res['price']:.6f}** ± {1.96*res['std_error']:.6f} (95% CI)\n"
            f"- Analytical Black-Scholes = {res['analytical_price']:.6f} "
            f"(abs error {res['abs_error']:.6f})\n\n"
            "The estimator's standard error decays as $1/\\sqrt{N}$; antithetic "
            "variates cut variance by exploiting the symmetry of $Z$ and $-Z$."
        )

    def visualize(self, checkpoints: int = 40, **kwargs: Any) -> "go.Figure":
        """Plot the running MC estimate converging to the analytical price.

        Args:
            checkpoints: Number of logarithmically-spaced sample sizes to plot.

        Returns:
            A Plotly figure with the running estimate, a 95% confidence band and
            the analytical Black-Scholes reference line.
        """
        import plotly.graph_objects as go

        self._require_in_range(checkpoints, "checkpoints", 5, 200)
        discounted = self._discounted_payoff(self._simulate_terminal(self.n_sims))
        # Cumulative running mean and running standard error along the sample.
        idx = np.unique(np.logspace(2, np.log10(discounted.size), int(checkpoints)).astype(int))
        running_mean = np.cumsum(discounted)[idx - 1] / idx
        running_std = np.array([discounted[:k].std(ddof=1) for k in idx])
        se = running_std / np.sqrt(idx)
        analytical = self.analytical_price()

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=idx, y=running_mean + 1.96 * se, mode="lines",
                                 line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=idx, y=running_mean - 1.96 * se, mode="lines",
                                 line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(31,119,180,0.2)", name="95% CI"))
        fig.add_trace(go.Scatter(x=idx, y=running_mean, mode="lines",
                                 name="MC estimate", line=dict(width=2)))
        fig.add_hline(y=analytical, line_dash="dash",
                      annotation_text=f"Black-Scholes = {analytical:.4f}")
        fig.update_layout(
            title="Monte Carlo Convergence to Analytical Price",
            xaxis_title="Number of simulated paths", yaxis_title="Estimated price",
            xaxis_type="log", template="plotly_white", hovermode="x unified",
        )
        return fig

    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return convergence benchmarks against the analytical Black-Scholes price."""
        m = cls(spot=42, strike=40, rate=0.10, sigma=0.20, maturity=0.5,
                option_type="call", n_sims=400_000, seed=12345)
        res = m.calculate()
        # (a) Statistical convergence: |MC - BS| within 3 standard errors (fixed seed).
        within_3se = float(res["abs_error"] <= 3 * res["std_error"])
        # (b) The analytical target itself matches Hull's textbook value.
        return [
            Benchmark("MC within 3 SE of Black-Scholes (seed=12345)", within_3se, 1.0,
                      rel_tol=0.0, source="Boyle (1977); convergence of the estimator"),
            Benchmark("Analytical target = Hull Ex.15.6", res["analytical_price"], 4.759,
                      rel_tol=2e-4, source="Hull (2018), Example 15.6"),
        ]
