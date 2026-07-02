"""Black-Scholes-Merton European option pricing.

Reference model implementation that establishes the contract every model in this
package follows: a single class inheriting :class:`BaseFinancialModel` with
``calculate`` / ``explain`` / ``visualize`` plus literature-sourced benchmarks.

Formula (Black & Scholes, 1973; Merton, 1973), with continuous dividend yield q::

    d1 = [ln(S/K) + (r - q + sigma^2 / 2) * T] / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    Call = S e^{-qT} N(d1) - K e^{-rT} N(d2)
    Put  = K e^{-rT} N(-d2) - S e^{-qT} N(-d1)

where ``N`` is the standard-normal CDF.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from scipy.stats import norm

from .base_model import BaseFinancialModel, Benchmark

if TYPE_CHECKING:  # pragma: no cover
    import plotly.graph_objects as go

OptionType = Literal["call", "put"]


class BlackScholesModel(BaseFinancialModel):
    """Price European options and their Greeks with the Black-Scholes-Merton model.

    Assumptions:
        * The underlying follows geometric Brownian motion with constant
          volatility ``sigma`` and continuous dividend yield ``q``.
        * Constant risk-free rate ``r``; no transaction costs; European exercise.

    Example:
        >>> m = BlackScholesModel(spot=42, strike=40, rate=0.10, sigma=0.20,
        ...                       maturity=0.5, option_type="call")
        >>> round(m.calculate()["price"], 2)
        4.76
    """

    name = "Black-Scholes-Merton"
    category = "Derivatives"
    references = [
        "Black, F. & Scholes, M. (1973). The Pricing of Options and Corporate "
        "Liabilities. Journal of Political Economy, 81(3), 637-654.",
        "Merton, R. C. (1973). Theory of Rational Option Pricing. Bell Journal "
        "of Economics and Management Science, 4(1), 141-183.",
        "Hull, J. C. (2018). Options, Futures, and Other Derivatives, 10th ed., "
        "Example 15.6 (S=42, K=40, r=0.10, sigma=0.20, T=0.5 -> call=4.76).",
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
        logger: Any = None,
    ) -> None:
        """Initialise and validate all pricing inputs.

        Args:
            spot: Current price of the underlying, ``S > 0``.
            strike: Option strike price, ``K > 0``.
            rate: Continuously-compounded risk-free rate ``r`` (per annum).
            sigma: Annualised volatility ``sigma > 0``.
            maturity: Time to expiry in years, ``T > 0``.
            option_type: ``"call"`` or ``"put"``.
            dividend_yield: Continuous dividend yield ``q >= 0``.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If any input is out of its valid domain.
        """
        super().__init__(logger=logger)
        self.spot = self._require_positive(spot, "spot")
        self.strike = self._require_positive(strike, "strike")
        self.rate = self._as_finite_float(rate, "rate")
        self.sigma = self._require_positive(sigma, "sigma")
        self.maturity = self._require_positive(maturity, "maturity")
        self.dividend_yield = self._require_nonnegative(dividend_yield, "dividend_yield")
        if option_type not in ("call", "put"):
            from .base_model import ValidationError

            raise ValidationError(
                f"'option_type' must be 'call' or 'put', got {option_type!r}."
            )
        self.option_type: OptionType = option_type
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def _d1_d2(self) -> tuple[float, float]:
        """Return the ``(d1, d2)`` terms of the Black-Scholes formula."""
        vol_sqrt_t = self.sigma * np.sqrt(self.maturity)
        # (r - q + sigma^2/2) is the risk-neutral drift of ln(S).
        d1 = (
            np.log(self.spot / self.strike)
            + (self.rate - self.dividend_yield + 0.5 * self.sigma**2) * self.maturity
        ) / vol_sqrt_t
        d2 = d1 - vol_sqrt_t
        return float(d1), float(d2)

    def price(self) -> float:
        """Return the fair value of the option.

        Returns:
            The Black-Scholes price for the configured ``option_type``.
        """
        d1, d2 = self._d1_d2()
        disc = np.exp(-self.rate * self.maturity)          # e^{-rT}
        disc_q = np.exp(-self.dividend_yield * self.maturity)  # e^{-qT}
        if self.option_type == "call":
            value = self.spot * disc_q * norm.cdf(d1) - self.strike * disc * norm.cdf(d2)
        else:  # put
            value = self.strike * disc * norm.cdf(-d2) - self.spot * disc_q * norm.cdf(-d1)
        return float(value)

    def greeks(self) -> dict[str, float]:
        """Return the option Greeks (sensitivities of price to inputs).

        Returns:
            Dict with ``delta``, ``gamma``, ``vega`` (per 1.00 vol),
            ``theta`` (per year) and ``rho`` (per 1.00 rate).
        """
        d1, d2 = self._d1_d2()
        disc = np.exp(-self.rate * self.maturity)
        disc_q = np.exp(-self.dividend_yield * self.maturity)
        pdf_d1 = norm.pdf(d1)
        sqrt_t = np.sqrt(self.maturity)
        # Gamma and Vega are identical for calls and puts.
        gamma = disc_q * pdf_d1 / (self.spot * self.sigma * sqrt_t)
        vega = self.spot * disc_q * pdf_d1 * sqrt_t
        if self.option_type == "call":
            delta = disc_q * norm.cdf(d1)
            theta = (
                -self.spot * disc_q * pdf_d1 * self.sigma / (2 * sqrt_t)
                - self.rate * self.strike * disc * norm.cdf(d2)
                + self.dividend_yield * self.spot * disc_q * norm.cdf(d1)
            )
            rho = self.strike * self.maturity * disc * norm.cdf(d2)
        else:
            delta = -disc_q * norm.cdf(-d1)
            theta = (
                -self.spot * disc_q * pdf_d1 * self.sigma / (2 * sqrt_t)
                + self.rate * self.strike * disc * norm.cdf(-d2)
                - self.dividend_yield * self.spot * disc_q * norm.cdf(-d1)
            )
            rho = -self.strike * self.maturity * disc * norm.cdf(-d2)
        return {
            "delta": float(delta),
            "gamma": float(gamma),
            "vega": float(vega),
            "theta": float(theta),
            "rho": float(rho),
        }

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute price, Greeks and the intermediate ``d1``/``d2`` terms.

        Returns:
            Dictionary with keys ``price``, ``d1``, ``d2`` and one entry per Greek.
        """
        d1, d2 = self._d1_d2()
        result: dict[str, Any] = {
            "price": self.price(),
            "d1": d1,
            "d2": d2,
            "option_type": self.option_type,
        }
        result.update(self.greeks())
        self._logger.info("Priced %s option at %.6f", self.option_type, result["price"])
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        d1, d2 = self._d1_d2()
        res = self.calculate()
        return (
            f"### Black-Scholes-Merton — {self.option_type.title()}\n\n"
            "The price solves the PDE "
            r"$\partial_t V + \tfrac12\sigma^2 S^2 \partial_{SS}V + (r-q)S\partial_S V - rV = 0$ "
            "under geometric Brownian motion. The closed form is:\n\n"
            r"$$C = S e^{-qT} N(d_1) - K e^{-rT} N(d_2), \quad "
            r"d_{1,2} = \frac{\ln(S/K) + (r-q\pm\tfrac12\sigma^2)T}{\sigma\sqrt{T}}$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- Inputs: S={self.spot}, K={self.strike}, r={self.rate}, "
            f"σ={self.sigma}, T={self.maturity}, q={self.dividend_yield}\n"
            f"- d1 = {d1:.6f}, d2 = {d2:.6f}\n"
            f"- **Price = {res['price']:.6f}**, Δ={res['delta']:.4f}, "
            f"Γ={res['gamma']:.4f}, vega={res['vega']:.4f}\n\n"
            "Cross-check: put-call parity "
            r"$C - P = S e^{-qT} - K e^{-rT}$ holds to machine precision."
        )

    def visualize(self, spot_range: float = 0.6, **kwargs: Any) -> "go.Figure":
        """Plot option value and intrinsic payoff across a range of spot prices.

        Args:
            spot_range: Fractional half-width of the spot grid around the current
                spot (``0.6`` -> 40%..160% of spot).

        Returns:
            A Plotly figure with the option value curve and its payoff at expiry.
        """
        import plotly.graph_objects as go

        self._require_in_range(spot_range, "spot_range", 0.05, 0.95)
        spots = np.linspace(self.spot * (1 - spot_range), self.spot * (1 + spot_range), 120)
        # Vectorised repricing across the spot grid (no Python loop).
        vol_sqrt_t = self.sigma * np.sqrt(self.maturity)
        d1 = (np.log(spots / self.strike)
              + (self.rate - self.dividend_yield + 0.5 * self.sigma**2) * self.maturity) / vol_sqrt_t
        d2 = d1 - vol_sqrt_t
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)
        if self.option_type == "call":
            values = spots * disc_q * norm.cdf(d1) - self.strike * disc * norm.cdf(d2)
            payoff = np.maximum(spots - self.strike, 0.0)
        else:
            values = self.strike * disc * norm.cdf(-d2) - spots * disc_q * norm.cdf(-d1)
            payoff = np.maximum(self.strike - spots, 0.0)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=spots, y=values, name="Value now", line=dict(width=3)))
        fig.add_trace(go.Scatter(x=spots, y=payoff, name="Payoff at expiry",
                                 line=dict(dash="dash")))
        fig.add_vline(x=self.strike, line_dash="dot", annotation_text="Strike")
        fig.update_layout(
            title=f"Black-Scholes {self.option_type.title()} Value vs. Spot",
            xaxis_title="Underlying spot price", yaxis_title="Option value",
            template="plotly_white", hovermode="x unified",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return canonical benchmarks: Hull's textbook value + parity identity."""
        call = cls(spot=42, strike=40, rate=0.10, sigma=0.20, maturity=0.5,
                   option_type="call")
        put = cls(spot=42, strike=40, rate=0.10, sigma=0.20, maturity=0.5,
                  option_type="put")
        c, p = call.price(), put.price()
        parity_lhs = c - p
        parity_rhs = 42 - 40 * np.exp(-0.10 * 0.5)  # q = 0
        return [
            Benchmark("Hull Ex.15.6 call price", c, 4.759, rel_tol=2e-4,
                      source="Hull (2018), Example 15.6"),
            Benchmark("Hull Ex.15.6 put price", p, 0.808, rel_tol=1e-3,
                      source="Hull (2018), Example 15.6"),
            Benchmark("Put-call parity identity", parity_lhs, float(parity_rhs),
                      rel_tol=1e-12, source="No-arbitrage identity"),
        ]
