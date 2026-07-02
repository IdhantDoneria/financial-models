"""Heston (1993) stochastic-volatility European option pricing.

Prices European options under the Heston model via its semi-analytical
characteristic-function representation, using the numerically stable
"Little Heston Trap" formulation (Albrecher et al., 2007) to avoid the branch-cut
discontinuities of the original 1993 integrand. A full-truncation Euler Monte
Carlo simulator provides an independent cross-check and the volatility paths for
visualisation.

Dynamics under the risk-neutral measure::

    dS_t = (r - q) S_t dt + sqrt(v_t) S_t dW_t^S
    dv_t = kappa (theta - v_t) dt + xi sqrt(v_t) dW_t^v,   d<W^S, W^v>_t = rho dt

The European call is ``C = S e^{-qT} P1 - K e^{-rT} P2`` where ``P1``, ``P2`` are
probabilities recovered by Fourier inversion of the characteristic function.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from scipy.integrate import quad
from scipy.optimize import brentq
from scipy.stats import norm

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    import plotly.graph_objects as go

OptionType = Literal["call", "put"]


class HestonModel(BaseFinancialModel):
    """Price European options under Heston stochastic volatility.

    Assumptions:
        * Variance follows a mean-reverting CIR process; the Feller condition
          ``2 kappa theta >= xi^2`` keeps variance strictly positive (a warning is
          logged if violated).
        * As ``xi -> 0`` with ``v0 = theta`` the model collapses to Black-Scholes
          with ``sigma = sqrt(theta)`` — used as an accuracy benchmark.

    Example:
        >>> m = HestonModel(spot=100, strike=100, rate=0.02, maturity=1.0,
        ...                 v0=0.04, kappa=1.5, theta=0.04, xi=0.3, rho=-0.6)
        >>> m.calculate()["price"] > 0
        True
    """

    name = "Heston Stochastic Volatility"
    category = "Derivatives"
    references = [
        "Heston, S. L. (1993). A Closed-Form Solution for Options with Stochastic "
        "Volatility. Review of Financial Studies, 6(2), 327-343.",
        "Albrecher, H., Mayer, P., Schoutens, W. & Tistaert, J. (2007). The Little "
        "Heston Trap. Wilmott Magazine, 83-92.",
    ]

    def __init__(
        self,
        *,
        spot: float,
        strike: float,
        rate: float,
        maturity: float,
        v0: float,
        kappa: float,
        theta: float,
        xi: float,
        rho: float,
        option_type: OptionType = "call",
        dividend_yield: float = 0.0,
        logger: Any = None,
    ) -> None:
        """Initialise and validate Heston parameters.

        Args:
            spot: Current underlying price ``S > 0``.
            strike: Strike price ``K > 0``.
            rate: Continuously-compounded risk-free rate ``r``.
            maturity: Time to expiry in years ``T > 0``.
            v0: Initial instantaneous variance ``v0 > 0`` (note: variance, not vol).
            kappa: Mean-reversion speed ``kappa > 0``.
            theta: Long-run variance ``theta > 0``.
            xi: Volatility of variance ("vol of vol") ``xi > 0``.
            rho: Correlation between the two Brownian motions, ``-1 <= rho <= 1``.
            option_type: ``"call"`` or ``"put"``.
            dividend_yield: Continuous dividend yield ``q >= 0``.

        Raises:
            ValidationError: If any input is outside its valid domain.
        """
        super().__init__(logger=logger)
        self.spot = self._require_positive(spot, "spot")
        self.strike = self._require_positive(strike, "strike")
        self.rate = self._as_finite_float(rate, "rate")
        self.maturity = self._require_positive(maturity, "maturity")
        self.v0 = self._require_positive(v0, "v0")
        self.kappa = self._require_positive(kappa, "kappa")
        self.theta = self._require_positive(theta, "theta")
        self.xi = self._require_positive(xi, "xi")
        self.rho = self._require_in_range(rho, "rho", -1.0, 1.0)
        self.dividend_yield = self._require_nonnegative(dividend_yield, "dividend_yield")
        if option_type not in ("call", "put"):
            raise ValidationError(f"'option_type' must be 'call'/'put', got {option_type!r}.")
        self.option_type: OptionType = option_type
        if 2 * self.kappa * self.theta < self.xi**2:
            self._logger.warning(
                "Feller condition violated (2*kappa*theta=%.4f < xi^2=%.4f); "
                "variance may hit zero.", 2 * self.kappa * self.theta, self.xi**2
            )

    # ------------------------------------------------------------------ #
    def _char_func(self, phi: complex, j: int) -> complex:
        """Heston characteristic function ``f_j(phi)`` (Little-Trap formulation).

        Args:
            phi: Fourier frequency.
            j: 1 for the ``P1`` probability, 2 for ``P2``.

        Returns:
            The complex value of the characteristic function at ``phi``.
        """
        i = 1j
        # u and b differ between the two probabilities (Heston, 1993, Eq. 12).
        u = 0.5 if j == 1 else -0.5
        b = self.kappa - self.rho * self.xi if j == 1 else self.kappa
        a = self.kappa * self.theta
        rho_xi_i_phi = self.rho * self.xi * i * phi
        # Discriminant d and the trap-stable ratio g = 1 / g_original.
        d = np.sqrt((rho_xi_i_phi - b) ** 2 - self.xi**2 * (2 * u * i * phi - phi**2))
        g = (b - rho_xi_i_phi - d) / (b - rho_xi_i_phi + d)
        exp_dt = np.exp(-d * self.maturity)
        drift = (self.rate - self.dividend_yield) * i * phi * self.maturity
        C = drift + (a / self.xi**2) * (
            (b - rho_xi_i_phi - d) * self.maturity
            - 2 * np.log((1 - g * exp_dt) / (1 - g))
        )
        D = ((b - rho_xi_i_phi - d) / self.xi**2) * ((1 - exp_dt) / (1 - g * exp_dt))
        return np.exp(C + D * self.v0 + i * phi * np.log(self.spot))

    def _prob(self, j: int) -> float:
        """Recover probability ``P_j`` by Fourier inversion of ``f_j``."""
        i = 1j
        log_k = np.log(self.strike)

        def integrand(phi: float) -> float:
            return float(np.real(np.exp(-i * phi * log_k) * self._char_func(phi, j) / (i * phi)))

        # Integrate from just above 0 (integrand ~ finite limit) to infinity.
        value, _ = quad(integrand, 1e-8, np.inf, limit=200)
        return 0.5 + value / np.pi

    def price(self) -> float:
        """Return the semi-analytical Heston price for the configured option."""
        p1, p2 = self._prob(1), self._prob(2)
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)
        call = self.spot * disc_q * p1 - self.strike * disc * p2
        if self.option_type == "call":
            return float(call)
        # Put via put-call parity: P = C - S e^{-qT} + K e^{-rT}.
        return float(call - self.spot * disc_q + self.strike * disc)

    def simulate_paths(
        self, n_paths: int = 20000, n_steps: int = 100, seed: int | None = 42
    ) -> tuple[np.ndarray, np.ndarray]:
        """Simulate price and variance paths with a full-truncation Euler scheme.

        Args:
            n_paths: Number of Monte Carlo paths.
            n_steps: Time steps per path.
            seed: RNG seed for reproducibility.

        Returns:
            Tuple ``(prices, variances)`` each of shape ``(n_paths, n_steps + 1)``.
        """
        rng = np.random.default_rng(seed)
        dt = self.maturity / n_steps
        s = np.full((n_paths, n_steps + 1), self.spot, dtype=float)
        v = np.full((n_paths, n_steps + 1), self.v0, dtype=float)
        chol = np.array([[1.0, 0.0], [self.rho, np.sqrt(1 - self.rho**2)]])
        for t in range(n_steps):
            z = rng.standard_normal((n_paths, 2)) @ chol.T
            v_pos = np.maximum(v[:, t], 0.0)  # full truncation keeps variance >= 0
            sqrt_v = np.sqrt(v_pos)
            s[:, t + 1] = s[:, t] * np.exp(
                (self.rate - self.dividend_yield - 0.5 * v_pos) * dt
                + sqrt_v * np.sqrt(dt) * z[:, 0]
            )
            v[:, t + 1] = v[:, t] + self.kappa * (self.theta - v_pos) * dt + self.xi * sqrt_v * np.sqrt(dt) * z[:, 1]
        return s, v

    def _mc_price(self, n_paths: int = 200000, seed: int | None = 2024) -> tuple[float, float]:
        """Return an independent Monte Carlo ``(price, std_error)`` cross-check."""
        s, _ = self.simulate_paths(n_paths=n_paths, n_steps=200, seed=seed)
        terminal = s[:, -1]
        payoff = (np.maximum(terminal - self.strike, 0.0) if self.option_type == "call"
                  else np.maximum(self.strike - terminal, 0.0))
        disc_payoff = np.exp(-self.rate * self.maturity) * payoff
        return float(disc_payoff.mean()), float(disc_payoff.std(ddof=1) / np.sqrt(disc_payoff.size))

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute the Heston price and its inversion probabilities.

        Returns:
            Dict with ``price``, ``P1``, ``P2`` and the Feller-condition flag.
        """
        p1, p2 = self._prob(1), self._prob(2)
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)
        call = self.spot * disc_q * p1 - self.strike * disc * p2
        price = call if self.option_type == "call" else call - self.spot * disc_q + self.strike * disc
        result = {
            "price": float(price), "P1": p1, "P2": p2,
            "feller_satisfied": bool(2 * self.kappa * self.theta >= self.xi**2),
        }
        self._logger.info("Heston %s price=%.6f (P1=%.4f, P2=%.4f)",
                          self.option_type, price, p1, p2)
        return result

    def _bs_implied_vol(self, price: float, strike: float) -> float:
        """Invert Black-Scholes to the implied volatility for a given price."""
        disc, disc_q = np.exp(-self.rate * self.maturity), np.exp(-self.dividend_yield * self.maturity)

        def bs_call(vol: float) -> float:
            vst = vol * np.sqrt(self.maturity)
            d1 = (np.log(self.spot / strike)
                  + (self.rate - self.dividend_yield + 0.5 * vol**2) * self.maturity) / vst
            d2 = d1 - vst
            return self.spot * disc_q * norm.cdf(d1) - strike * disc * norm.cdf(d2)

        intrinsic = max(self.spot * disc_q - strike * disc, 0.0)
        if price <= intrinsic + 1e-10:
            return float("nan")
        return float(brentq(lambda v: bs_call(v) - price, 1e-4, 5.0, maxiter=200))

    def explain(self) -> str:
        """Return a Markdown explanation with the current worked example."""
        res = self.calculate()
        mc, se = self._mc_price(n_paths=100000)
        return (
            f"### Heston Stochastic Volatility — {self.option_type.title()}\n\n"
            "Volatility is itself random, mean-reverting to $\\theta$:\n\n"
            r"$$dS_t=(r-q)S_t\,dt+\sqrt{v_t}\,S_t\,dW^S_t,\quad "
            r"dv_t=\kappa(\theta-v_t)\,dt+\xi\sqrt{v_t}\,dW^v_t$$"
            "\n\nThe price is recovered by Fourier inversion of the characteristic "
            "function: "
            r"$C=Se^{-qT}P_1-Ke^{-rT}P_2$."
            "\n\n**Worked example (current inputs):**\n"
            f"- v0={self.v0}, κ={self.kappa}, θ={self.theta}, ξ={self.xi}, ρ={self.rho}\n"
            f"- P1={res['P1']:.4f}, P2={res['P2']:.4f}\n"
            f"- **Semi-analytic price = {res['price']:.6f}**\n"
            f"- Independent Monte Carlo = {mc:.4f} ± {1.96*se:.4f} (95% CI) — agreement "
            "validates the closed form.\n\n"
            f"- Feller condition (2κθ≥ξ²): {'satisfied' if res['feller_satisfied'] else 'VIOLATED'}."
        )

    def visualize(self, strike_range: float = 0.35, n_strikes: int = 25, **kwargs: Any) -> "go.Figure":
        """Plot the Black-Scholes implied-volatility smile produced by Heston.

        Args:
            strike_range: Fractional half-width of the strike grid around spot.
            n_strikes: Number of strikes to price and invert.

        Returns:
            A Plotly figure of implied volatility vs. strike (the volatility smile).
        """
        import plotly.graph_objects as go

        self._require_in_range(strike_range, "strike_range", 0.05, 0.9)
        strikes = np.linspace(self.spot * (1 - strike_range), self.spot * (1 + strike_range),
                              int(n_strikes))
        implied = []
        for k in strikes:
            model = HestonModel(
                spot=self.spot, strike=float(k), rate=self.rate, maturity=self.maturity,
                v0=self.v0, kappa=self.kappa, theta=self.theta, xi=self.xi, rho=self.rho,
                option_type="call", dividend_yield=self.dividend_yield,
            )
            implied.append(model._bs_implied_vol(model.price(), float(k)) * 100)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=strikes, y=implied, mode="lines+markers",
                                 name="Heston implied vol"))
        fig.add_hline(y=np.sqrt(self.theta) * 100, line_dash="dash",
                      annotation_text=f"√θ = {np.sqrt(self.theta)*100:.1f}%")
        fig.add_vline(x=self.spot, line_dash="dot", annotation_text="Spot")
        fig.update_layout(
            title="Heston-Implied Volatility Smile",
            xaxis_title="Strike", yaxis_title="Black-Scholes implied volatility (%)",
            template="plotly_white", hovermode="x unified",
        )
        return fig

    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return the BS-reduction identity and an independent MC cross-check."""
        # (a) As xi -> 0 with v0 = theta, Heston collapses to Black-Scholes(sqrt(theta)).
        v = 0.04
        reduced = cls(spot=100, strike=100, rate=0.02, maturity=1.0, v0=v, kappa=2.0,
                      theta=v, xi=1e-4, rho=0.0, option_type="call")
        vst = np.sqrt(v) * np.sqrt(1.0)
        d1 = (np.log(100 / 100) + (0.02 + 0.5 * v) * 1.0) / vst
        d2 = d1 - vst
        bs = 100 * norm.cdf(d1) - 100 * np.exp(-0.02) * norm.cdf(d2)
        # (b) Full stochastic-vol case: semi-analytic price vs. independent Monte Carlo.
        full = cls(spot=100, strike=100, rate=0.02, maturity=1.0, v0=0.04, kappa=1.5,
                   theta=0.04, xi=0.3, rho=-0.6, option_type="call")
        analytic = full.price()
        mc, se = full._mc_price(n_paths=300000, seed=7)
        within_3se = float(abs(analytic - mc) <= 3 * se)
        return [
            Benchmark("Heston -> Black-Scholes as xi->0", reduced.price(), float(bs),
                      rel_tol=1e-3, source="Heston (1993) degenerate case"),
            Benchmark("Semi-analytic within 3 SE of Monte Carlo", within_3se, 1.0,
                      rel_tol=0.0, source="Full-truncation Euler MC cross-check"),
        ]
