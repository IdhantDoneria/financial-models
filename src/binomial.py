"""Cox-Ross-Rubinstein binomial tree option pricing.

Prices European and American options on a recombining binomial lattice and shows
convergence to the Black-Scholes price as the number of steps grows.

Cox, Ross & Rubinstein (1979) parameterisation, with step ``dt = T / N``::

    u = exp(sigma * sqrt(dt)),   d = 1 / u
    p = (exp((r - q) * dt) - d) / (u - d)     (risk-neutral up-probability)

The value is obtained by backward induction; American options additionally take
``max(continuation, intrinsic)`` at every node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from scipy.stats import norm

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    import plotly.graph_objects as go

OptionType = Literal["call", "put"]
Exercise = Literal["european", "american"]


class BinomialTreeModel(BaseFinancialModel):
    """Price options on a Cox-Ross-Rubinstein recombining binomial tree.

    Assumptions:
        * The underlying moves up by ``u`` or down by ``d = 1/u`` each step under
          the risk-neutral probability ``p``.
        * As ``N -> infinity`` the European price converges to Black-Scholes.

    Example:
        >>> m = BinomialTreeModel(spot=42, strike=40, rate=0.10, sigma=0.20,
        ...                       maturity=0.5, n_steps=1000, option_type="call")
        >>> abs(m.calculate()["price"] - 4.759) < 0.01
        True
    """

    name = "Binomial Tree (CRR)"
    category = "Derivatives"
    references = [
        "Cox, J. C., Ross, S. A. & Rubinstein, M. (1979). Option Pricing: A "
        "Simplified Approach. Journal of Financial Economics, 7(3), 229-263.",
        "Hull, J. C. (2018). Options, Futures, and Other Derivatives, 10th ed., "
        "Ch. 13-21.",
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
        exercise: Exercise = "european",
        dividend_yield: float = 0.0,
        n_steps: int = 500,
        logger: Any = None,
    ) -> None:
        """Initialise and validate tree inputs.

        Args:
            spot: Current underlying price ``S > 0``.
            strike: Strike price ``K > 0``.
            rate: Continuously-compounded risk-free rate ``r``.
            sigma: Annualised volatility ``sigma > 0``.
            maturity: Time to expiry in years ``T > 0``.
            option_type: ``"call"`` or ``"put"``.
            exercise: ``"european"`` or ``"american"``.
            dividend_yield: Continuous dividend yield ``q >= 0``.
            n_steps: Number of time steps in the tree (>= 1).

        Raises:
            ValidationError: If any input is invalid or the tree violates
                no-arbitrage (``p`` outside ``[0, 1]``).
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
        if exercise not in ("european", "american"):
            raise ValidationError(f"'exercise' must be 'european'/'american', got {exercise!r}.")
        self.option_type: OptionType = option_type
        self.exercise: Exercise = exercise
        n = int(n_steps)
        if n < 1:
            raise ValidationError(f"'n_steps' must be >= 1, got {n_steps!r}.")
        self.n_steps = n

    # ------------------------------------------------------------------ #
    def _tree_params(self, n_steps: int) -> tuple[float, float, float, float]:
        """Return ``(dt, u, d, p)`` for an ``n_steps`` CRR tree.

        Raises:
            ValidationError: If the risk-neutral probability ``p`` leaves
                ``[0, 1]`` (an arbitrage-violating parameterisation).
        """
        dt = self.maturity / n_steps
        u = float(np.exp(self.sigma * np.sqrt(dt)))     # up factor
        d = 1.0 / u                                     # down factor
        p = (np.exp((self.rate - self.dividend_yield) * dt) - d) / (u - d)
        if not (0.0 <= p <= 1.0):
            raise ValidationError(
                f"Risk-neutral probability p={p:.4f} outside [0,1]; increase n_steps "
                "or check inputs (no-arbitrage violated)."
            )
        return dt, u, d, float(p)

    def _price_tree(self, n_steps: int) -> float:
        """Price the option on an ``n_steps`` tree via backward induction.

        Each backward step is fully vectorised over the nodes in that layer; the
        only Python loop is the unavoidable sweep over the ``n_steps`` layers.
        """
        dt, u, d, p = self._tree_params(n_steps)
        disc = np.exp(-self.rate * dt)
        j = np.arange(n_steps + 1)
        # Terminal underlying prices S_T = S0 * u^j * d^(N-j).
        prices = self.spot * u**j * d**(n_steps - j)
        if self.option_type == "call":
            values = np.maximum(prices - self.strike, 0.0)
        else:
            values = np.maximum(self.strike - prices, 0.0)
        # Roll back the lattice one layer at a time.
        for i in range(n_steps - 1, -1, -1):
            values = disc * (p * values[1:] + (1 - p) * values[:-1])
            if self.exercise == "american":
                prices = prices[: i + 1] / d  # underlying prices at layer i
                intrinsic = (np.maximum(prices - self.strike, 0.0)
                             if self.option_type == "call"
                             else np.maximum(self.strike - prices, 0.0))
                values = np.maximum(values, intrinsic)
        return float(values[0])

    def analytical_price(self) -> float:
        """Return the Black-Scholes price (the European convergence target)."""
        vst = self.sigma * np.sqrt(self.maturity)
        d1 = (np.log(self.spot / self.strike)
              + (self.rate - self.dividend_yield + 0.5 * self.sigma**2) * self.maturity) / vst
        d2 = d1 - vst
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)
        if self.option_type == "call":
            return float(self.spot * disc_q * norm.cdf(d1) - self.strike * disc * norm.cdf(d2))
        return float(self.strike * disc * norm.cdf(-d2) - self.spot * disc_q * norm.cdf(-d1))

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Price the option and (for European) report the Black-Scholes gap.

        Returns:
            Dict with ``price``, tree parameters ``u``/``d``/``p``, and — for
            European exercise — ``analytical_price`` and ``abs_error``.
        """
        _, u, d, p = self._tree_params(self.n_steps)
        price = self._price_tree(self.n_steps)
        result: dict[str, Any] = {
            "price": price, "u": u, "d": d, "p": p,
            "n_steps": self.n_steps, "exercise": self.exercise,
        }
        if self.exercise == "european":
            analytical = self.analytical_price()
            result["analytical_price"] = analytical
            result["abs_error"] = abs(price - analytical)
        self._logger.info("Binomial %s %s price=%.6f (N=%d)",
                          self.exercise, self.option_type, price, self.n_steps)
        return result

    def explain(self) -> str:
        """Return a Markdown explanation with the current worked example."""
        res = self.calculate()
        target = res.get("analytical_price")
        target_line = (f"- Black-Scholes target = {target:.6f} "
                       f"(abs error {res['abs_error']:.6f})\n" if target is not None
                       else "- American exercise: no closed form; tree is the reference.\n")
        return (
            f"### Binomial Tree (CRR) — {self.exercise.title()} {self.option_type.title()}\n\n"
            "Each step the price moves up by $u$ or down by $d=1/u$ with risk-neutral "
            "probability $p$:\n\n"
            r"$$u=e^{\sigma\sqrt{\Delta t}},\quad d=\tfrac1u,\quad "
            r"p=\frac{e^{(r-q)\Delta t}-d}{u-d}$$"
            "\n\nValues propagate backward: "
            r"$V = e^{-r\Delta t}\big[pV_{up}+(1-p)V_{down}\big]$, taking "
            r"$\max(V,\text{intrinsic})$ at each node for American options."
            "\n\n**Worked example (current inputs):**\n"
            f"- N = {self.n_steps} steps, u = {res['u']:.4f}, d = {res['d']:.4f}, p = {res['p']:.4f}\n"
            f"- **Price = {res['price']:.6f}**\n" + target_line
        )

    def visualize(self, max_steps: int = 120, **kwargs: Any) -> "go.Figure":
        """Plot European price convergence to Black-Scholes as ``N`` grows.

        Args:
            max_steps: Largest step count to evaluate on the convergence curve.

        Returns:
            A Plotly figure of tree price vs. ``N`` with the Black-Scholes line.
            (For American options the tree price is shown without a BS target.)
        """
        import plotly.graph_objects as go

        self._require_in_range(max_steps, "max_steps", 10, 2000)
        steps = np.unique(np.linspace(2, int(max_steps), 60).astype(int))
        prices = np.array([self._price_tree(int(n)) for n in steps])

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=steps, y=prices, mode="lines+markers",
                                 name="Binomial price"))
        if self.exercise == "european":
            analytical = self.analytical_price()
            fig.add_hline(y=analytical, line_dash="dash",
                          annotation_text=f"Black-Scholes = {analytical:.4f}")
        fig.update_layout(
            title=f"Binomial Convergence — {self.exercise.title()} {self.option_type.title()}",
            xaxis_title="Number of steps (N)", yaxis_title="Option price",
            template="plotly_white", hovermode="x unified",
        )
        return fig

    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return convergence and American-vs-European identity benchmarks."""
        euro = cls(spot=42, strike=40, rate=0.10, sigma=0.20, maturity=0.5,
                   option_type="call", exercise="european", n_steps=2000)
        euro_price = euro._price_tree(2000)
        # American call on a non-dividend-paying stock equals the European call.
        amer = cls(spot=42, strike=40, rate=0.10, sigma=0.20, maturity=0.5,
                   option_type="call", exercise="american", n_steps=2000)
        amer_price = amer._price_tree(2000)
        return [
            Benchmark("CRR European call -> Black-Scholes (N=2000)", euro_price,
                      euro.analytical_price(), rel_tol=1e-3,
                      source="Cox-Ross-Rubinstein (1979) convergence"),
            Benchmark("American call == European call (no dividends)", amer_price,
                      euro_price, rel_tol=1e-9,
                      source="Merton (1973): early exercise never optimal"),
            Benchmark("European call ~ Hull Ex.15.6", euro_price, 4.759,
                      rel_tol=1e-3, source="Hull (2018), Example 15.6"),
        ]
