"""Modern Portfolio Theory (Markowitz mean-variance optimisation).

Implements the classic single-period mean-variance framework: the global
minimum-variance portfolio, the tangency (maximum-Sharpe) portfolio, and the
analytic efficient frontier obtained from the two-fund separation theorem.

Formulae (Markowitz, 1952; Merton, 1972), with ``mu`` the vector of expected
returns, ``Sigma`` the return covariance matrix, ``1`` the ones vector and
``rf`` the risk-free rate::

    w_mv = Sigma^{-1} 1 / (1^T Sigma^{-1} 1)          # global min-variance
    w_t  = Sigma^{-1}(mu - rf 1) / (1^T Sigma^{-1}(mu - rf 1))  # tangency

    ret(w)   = w^T mu
    var(w)   = w^T Sigma w
    vol(w)   = sqrt(var(w))
    sharpe(w)= (ret(w) - rf) / vol(w)

The efficient frontier uses the scalar constants::

    A = 1^T Sigma^{-1} 1,  B = 1^T Sigma^{-1} mu,  C = mu^T Sigma^{-1} mu,
    D = A C - B^2

so the minimum variance attainable for a target return ``m`` is
``sigma^2(m) = (A m^2 - 2 B m + C) / D``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go

#: Relative floor applied to the smallest eigenvalue when testing that the
#: covariance matrix is (strictly) positive-definite.
_PD_EIG_TOL = 1e-12


class ModernPortfolioTheoryModel(BaseFinancialModel):
    """Markowitz mean-variance portfolio optimisation.

    Assumptions:
        * A single investment period with known expected returns ``mu`` and a
          known, positive-definite covariance matrix ``Sigma``.
        * Investors care only about the mean and variance of portfolio return.
        * Unlimited borrowing/lending at ``risk_free_rate`` and unrestricted
          (possibly negative) weights that sum to one.

    Example:
        >>> mu = [0.10, 0.15]
        >>> cov = [[0.04, 0.006], [0.006, 0.09]]
        >>> m = ModernPortfolioTheoryModel(expected_returns=mu, covariance=cov,
        ...                                risk_free_rate=0.02)
        >>> round(float(m.calculate()["min_variance_weights"][0]), 6)
        0.711864
    """

    name = "Modern Portfolio Theory"
    category = "Portfolio"
    references = [
        "Markowitz, H. (1952). Portfolio Selection. Journal of Finance, 7(1), "
        "77-91.",
        "Merton, R. C. (1972). An Analytic Derivation of the Efficient Portfolio "
        "Frontier. Journal of Financial and Quantitative Analysis, 7(4), "
        "1851-1872.",
    ]

    def __init__(
        self,
        *,
        expected_returns: Any,
        covariance: Any,
        risk_free_rate: float = 0.0,
        logger: Any = None,
    ) -> None:
        """Initialise and validate the mean-variance inputs.

        Args:
            expected_returns: 1-D array-like of length ``n`` holding each asset's
                expected return ``mu_i``.
            covariance: ``n x n`` return covariance matrix ``Sigma``. Must be
                square, symmetric and positive-definite.
            risk_free_rate: Continuously-quoted risk-free rate ``rf`` (finite).
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If shapes disagree, or ``covariance`` is not square,
                symmetric or positive-definite, or any value is non-finite.
        """
        super().__init__(logger=logger)
        # mu: expected-return vector (length n).
        self.expected_returns = self._as_float_array(expected_returns, "expected_returns")
        self.n = int(self.expected_returns.size)
        # Sigma: covariance matrix, validated square / symmetric / positive-definite.
        self.covariance = self._validate_covariance(covariance, self.n)
        # rf: risk-free rate.
        self.risk_free_rate = self._as_finite_float(risk_free_rate, "risk_free_rate")

        # Pre-solve the two systems Sigma^{-1} 1 and Sigma^{-1} mu once (stable
        # solve rather than an explicit inverse), then the frontier constants.
        ones = np.ones(self.n)                                   # 1 vector
        self._sig_inv_one = np.linalg.solve(self.covariance, ones)   # Sigma^{-1} 1
        self._sig_inv_mu = np.linalg.solve(self.covariance, self.expected_returns)
        self.A = float(ones @ self._sig_inv_one)                 # 1^T Sigma^{-1} 1
        self.B = float(ones @ self._sig_inv_mu)                  # 1^T Sigma^{-1} mu
        self.C = float(self.expected_returns @ self._sig_inv_mu)  # mu^T Sigma^{-1} mu
        self.D = self.A * self.C - self.B**2                     # A C - B^2
        self._logger.debug("Initialised %r (n=%d)", self, self.n)

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_covariance(covariance: Any, n: int) -> np.ndarray:
        """Return ``covariance`` as a validated ``n x n`` positive-definite matrix.

        Args:
            covariance: Array-like candidate covariance matrix.
            n: Expected dimension (must equal ``len(expected_returns)``).

        Returns:
            The covariance as a ``float64`` ``n x n`` :class:`numpy.ndarray`.

        Raises:
            ValidationError: If not 2-D/square, mis-sized, non-finite, asymmetric
                or not strictly positive-definite.
        """
        try:
            cov = np.asarray(covariance, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"'covariance' must be a numeric matrix, got {covariance!r}."
            ) from exc
        if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
            raise ValidationError(
                f"'covariance' must be a square 2-D matrix, got shape {cov.shape}."
            )
        if cov.shape[0] != n:
            raise ValidationError(
                f"'covariance' shape {cov.shape} does not match "
                f"len(expected_returns)={n}."
            )
        if not np.all(np.isfinite(cov)):
            raise ValidationError("'covariance' must contain only finite values.")
        # Symmetry: Sigma == Sigma^T to a tight absolute tolerance.
        if not np.allclose(cov, cov.T, rtol=0.0, atol=1e-12):
            raise ValidationError("'covariance' must be symmetric.")
        # Positive-definite <=> all eigenvalues > 0 (eigvalsh for symmetric input).
        eigs = np.linalg.eigvalsh(cov)
        tol = _PD_EIG_TOL * max(float(np.abs(eigs).max()), 1.0)
        if float(eigs.min()) <= tol:
            raise ValidationError(
                "'covariance' must be positive-definite; smallest eigenvalue "
                f"{float(eigs.min()):.3e} <= {tol:.3e}."
            )
        return cov

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def min_variance_weights(self) -> np.ndarray:
        """Return the global minimum-variance weights ``w_mv``.

        Returns:
            ``w_mv = Sigma^{-1} 1 / (1^T Sigma^{-1} 1)``, a length-``n`` array
            summing to one.
        """
        return self._sig_inv_one / self.A  # normalise Sigma^{-1} 1 by A = 1^T Sigma^{-1} 1

    def tangency_weights(self) -> np.ndarray:
        """Return the tangency (maximum-Sharpe) weights ``w_t``.

        Returns:
            ``w_t = Sigma^{-1}(mu - rf 1) / (1^T Sigma^{-1}(mu - rf 1))``.

        Raises:
            ValidationError: If the normalising constant ``1^T Sigma^{-1}(mu-rf1)``
                is (numerically) zero, so the tangency portfolio is undefined.
        """
        excess = self.expected_returns - self.risk_free_rate  # mu - rf 1
        h = np.linalg.solve(self.covariance, excess)          # Sigma^{-1}(mu - rf 1)
        denom = float(np.ones(self.n) @ h)                    # 1^T Sigma^{-1}(mu-rf1)
        if abs(denom) < 1e-15:
            raise ValidationError(
                "Tangency portfolio is undefined: 1^T Sigma^{-1}(mu - rf*1) ~ 0."
            )
        return h / denom

    def portfolio_stats(self, weights: np.ndarray) -> dict[str, float]:
        """Return return, variance, volatility and Sharpe ratio of ``weights``.

        Args:
            weights: Portfolio weight vector (need not sum to one).

        Returns:
            Dict with ``return``, ``variance``, ``volatility`` and ``sharpe``.
        """
        w = np.asarray(weights, dtype=float)
        ret = float(w @ self.expected_returns)          # w^T mu
        var = float(w @ self.covariance @ w)            # w^T Sigma w
        vol = float(np.sqrt(var))
        sharpe = (ret - self.risk_free_rate) / vol if vol > 0 else float("nan")
        return {"return": ret, "variance": var, "volatility": vol, "sharpe": sharpe}

    def efficient_frontier(
        self, n_points: int = 50
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return the analytic efficient frontier over a grid of target returns.

        Uses the two-fund constants ``A, B, C, D`` so that the minimum variance
        for a target return ``m`` is ``sigma^2 = (A m^2 - 2 B m + C) / D`` and the
        corresponding volatility is its square root.

        Args:
            n_points: Number of target returns to evaluate (``>= 2``).

        Returns:
            A tuple ``(target_returns, min_volatilities)`` of equal-length arrays.

        Raises:
            ValidationError: If ``n_points`` is not an integer ``>= 2``.
        """
        if not (isinstance(n_points, (int, np.integer)) and int(n_points) >= 2):
            raise ValidationError(f"'n_points' must be an int >= 2, got {n_points!r}.")
        lo, hi = float(self.expected_returns.min()), float(self.expected_returns.max())
        if hi <= lo:  # degenerate: all assets share one expected return.
            hi = lo + 1.0
        targets = np.linspace(lo, hi, int(n_points))          # target returns m
        # Vectorised: variance(m) = (A m^2 - 2 B m + C) / D for every target m.
        variances = (self.A * targets**2 - 2.0 * self.B * targets + self.C) / self.D
        vols = np.sqrt(np.clip(variances, 0.0, None))         # guard tiny negatives
        return targets, vols

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute the min-variance and tangency portfolios.

        Returns:
            Dict with ``min_variance_weights``, ``min_variance_return``,
            ``min_variance_vol``, ``tangency_weights``, ``tangency_return``,
            ``tangency_vol`` and ``tangency_sharpe``.
        """
        w_mv = self.min_variance_weights()
        w_t = self.tangency_weights()
        mv = self.portfolio_stats(w_mv)
        tan = self.portfolio_stats(w_t)
        result: dict[str, Any] = {
            "min_variance_weights": w_mv,
            "min_variance_return": mv["return"],
            "min_variance_vol": mv["volatility"],
            "tangency_weights": w_t,
            "tangency_return": tan["return"],
            "tangency_vol": tan["volatility"],
            "tangency_sharpe": tan["sharpe"],
        }
        self._logger.info(
            "MV vol=%.6f, tangency Sharpe=%.6f", mv["volatility"], tan["sharpe"]
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        res = self.calculate()
        w_mv = np.array2string(res["min_variance_weights"], precision=4)
        w_t = np.array2string(res["tangency_weights"], precision=4)
        return (
            "### Modern Portfolio Theory (Markowitz mean-variance)\n\n"
            "Minimising variance subject to a budget constraint gives the global "
            "minimum-variance portfolio; adding a risk-free asset gives the "
            "tangency portfolio that maximises the Sharpe ratio:\n\n"
            r"$$w_{mv} = \frac{\Sigma^{-1}\mathbf{1}}{\mathbf{1}^{\top}\Sigma^{-1}"
            r"\mathbf{1}}, \qquad w_{t} = \frac{\Sigma^{-1}(\mu - r_f\mathbf{1})}"
            r"{\mathbf{1}^{\top}\Sigma^{-1}(\mu - r_f\mathbf{1})}$$"
            "\n\nThe efficient frontier follows from "
            r"$A=\mathbf{1}^{\top}\Sigma^{-1}\mathbf{1}$, "
            r"$B=\mathbf{1}^{\top}\Sigma^{-1}\mu$, "
            r"$C=\mu^{\top}\Sigma^{-1}\mu$, $D=AC-B^2$ via "
            r"$\sigma^2(m)=\frac{A m^2 - 2 B m + C}{D}$."
            "\n\n**Worked example (current inputs):**\n"
            f"- Assets: n={self.n}, rf={self.risk_free_rate}\n"
            f"- Constants: A={self.A:.4f}, B={self.B:.4f}, C={self.C:.4f}, "
            f"D={self.D:.6f}\n"
            f"- Min-variance weights = {w_mv}, vol = {res['min_variance_vol']:.6f}\n"
            f"- Tangency weights = {w_t}, return = {res['tangency_return']:.6f}, "
            f"vol = {res['tangency_vol']:.6f}, **Sharpe = "
            f"{res['tangency_sharpe']:.6f}**\n\n"
            "Cross-check: the tangency Sharpe equals "
            r"$\sqrt{(\mu-r_f\mathbf{1})^{\top}\Sigma^{-1}(\mu-r_f\mathbf{1})}$ "
            "to machine precision."
        )

    def visualize(self, n_points: int = 80, **kwargs: Any) -> "go.Figure":
        """Plot the efficient frontier, key portfolios and the capital market line.

        Args:
            n_points: Number of frontier points to trace.

        Returns:
            A Plotly figure with the frontier line, the minimum-variance and
            tangency markers, and the CML from ``(0, rf)`` through the tangency
            portfolio.
        """
        import plotly.graph_objects as go

        targets, vols = self.efficient_frontier(n_points=n_points)
        res = self.calculate()
        fig = go.Figure()
        # Efficient frontier: volatility on x, expected return on y.
        fig.add_trace(go.Scatter(x=vols, y=targets, name="Efficient frontier",
                                 mode="lines", line=dict(width=3)))
        # Global minimum-variance portfolio marker.
        fig.add_trace(go.Scatter(
            x=[res["min_variance_vol"]], y=[res["min_variance_return"]],
            name="Min-variance", mode="markers",
            marker=dict(size=11, symbol="diamond")))
        # Tangency (max-Sharpe) portfolio marker.
        fig.add_trace(go.Scatter(
            x=[res["tangency_vol"]], y=[res["tangency_return"]],
            name="Tangency", mode="markers", marker=dict(size=12, symbol="star")))
        # Capital market line: y = rf + Sharpe * x, from (0, rf) through tangency.
        x_max = float(max(vols.max(), res["tangency_vol"])) * 1.1
        cml_x = np.linspace(0.0, x_max, 50)
        cml_y = self.risk_free_rate + res["tangency_sharpe"] * cml_x
        fig.add_trace(go.Scatter(x=cml_x, y=cml_y, name="Capital market line",
                                 mode="lines", line=dict(dash="dash")))
        fig.update_layout(
            title="Efficient Frontier & Capital Market Line",
            xaxis_title="Volatility (std. dev. of return)",
            yaxis_title="Expected return",
            template="plotly_white", hovermode="closest",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return closed-form Markowitz identities used to grade accuracy."""
        import math

        # Two-asset min-variance closed form: weight on asset 1 is
        # (s2^2 - s12) / (s1^2 + s2^2 - 2 s12); expected returns are irrelevant.
        s1_2, s2_2, s12 = 0.04, 0.09, 0.006
        cov = [[s1_2, s12], [s12, s2_2]]
        m = cls(expected_returns=[0.10, 0.15], covariance=cov, risk_free_rate=0.02)
        w_mv = m.min_variance_weights()
        w1_expected = (s2_2 - s12) / (s1_2 + s2_2 - 2.0 * s12)

        # Independent max-Sharpe identity: tangency Sharpe = sign(denom) *
        # sqrt((mu - rf 1)^T Sigma^{-1} (mu - rf 1)).
        excess = m.expected_returns - m.risk_free_rate
        h = np.linalg.solve(m.covariance, excess)
        s_quad = float(excess @ h)                       # (mu-rf1)^T Sigma^{-1}(mu-rf1)
        denom = float(np.ones(m.n) @ h)                  # 1^T Sigma^{-1}(mu-rf1)
        sharpe_expected = math.copysign(math.sqrt(s_quad), denom)
        sharpe_computed = m.portfolio_stats(m.tangency_weights())["sharpe"]

        return [
            Benchmark("Two-asset min-variance weight (asset 1)",
                      float(w_mv[0]), float(w1_expected), rel_tol=1e-9,
                      source="Markowitz (1952), two-asset min-variance closed form"),
            Benchmark("Min-variance weights sum to one",
                      float(w_mv.sum()), 1.0, rel_tol=1e-12,
                      source="Budget constraint (fully-invested) identity"),
            Benchmark("Tangency maximum Sharpe identity",
                      float(sharpe_computed), float(sharpe_expected), rel_tol=1e-9,
                      source="Merton (1972), maximum-Sharpe closed form"),
        ]
