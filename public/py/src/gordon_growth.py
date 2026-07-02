"""Gordon Growth (constant-dividend-growth) equity valuation model.

Values a share as the present value of a dividend stream growing forever at a
constant rate ``g``, discounted at the investor's required return ``r``.

Formula (Gordon & Shapiro, 1956; Gordon, 1959)::

    D_1 = D_0 * (1 + g)          (next period's dividend, if D_0 is trailing)
    P_0 = D_1 / (r - g)          (Gordon growing perpetuity)

with the total required return decomposed as::

    r = D_1 / P_0  +  g          (dividend yield + capital-gains yield)

Convergence requires ``r > g``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go


class GordonGrowthModel(BaseFinancialModel):
    """Price a share via the constant-growth dividend discount model.

    Assumptions:
        * Dividends grow at a single constant rate ``g`` in perpetuity.
        * The required return ``r`` is constant and strictly exceeds ``g``.
        * Dividends are the only cash flow to equity holders.

    Example:
        >>> m = GordonGrowthModel(dividend=2.0, required_return=0.08, growth=0.03,
        ...                       dividend_is_forward=True)
        >>> m.calculate()["price"]
        40.0
    """

    name = "Gordon Growth Model"
    category = "Valuation"
    references = [
        "Gordon, M. J. (1959). Dividends, Earnings, and Stock Prices. The Review "
        "of Economics and Statistics, 41(2), 99-105.",
        "Gordon, M. J. & Shapiro, E. (1956). Capital Equipment Analysis: The "
        "Required Rate of Profit. Management Science, 3(1), 102-110.",
    ]

    def __init__(
        self,
        *,
        dividend: float,
        required_return: float,
        growth: float,
        dividend_is_forward: bool = False,
        logger: Any = None,
    ) -> None:
        """Initialise and validate all valuation inputs.

        Args:
            dividend: The dividend per share, ``> 0``. Interpreted as the trailing
                dividend ``D_0`` when ``dividend_is_forward`` is ``False`` (default)
                and as the forward dividend ``D_1`` when ``True``.
            required_return: Investor required return ``r > 0``.
            growth: Constant perpetual dividend growth rate ``g``; must satisfy
                ``required_return > growth``.
            dividend_is_forward: If ``True`` treat ``dividend`` as ``D_1`` directly;
                otherwise grow it one period, ``D_1 = D_0 * (1 + g)``.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If any input is out of its valid domain or if
                ``required_return <= growth`` (perpetuity diverges).
        """
        super().__init__(logger=logger)
        self.dividend = self._require_positive(dividend, "dividend")  # D_0 or D_1
        self.required_return = self._require_positive(required_return, "required_return")  # r
        self.growth = self._as_finite_float(growth, "growth")  # g
        self.dividend_is_forward = bool(dividend_is_forward)
        # Gordon perpetuity converges only when r > g.
        if self.required_return <= self.growth:
            raise ValidationError(
                f"'required_return' ({self.required_return!r}) must exceed 'growth' "
                f"({self.growth!r}) for the Gordon perpetuity to converge."
            )
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def forward_dividend(self) -> float:
        """Return next period's dividend ``D_1``.

        Returns:
            ``dividend`` itself when ``dividend_is_forward`` is set, else the
            trailing dividend grown one period, ``D_0 * (1 + g)``.
        """
        if self.dividend_is_forward:
            return self.dividend  # already D_1
        return self.dividend * (1.0 + self.growth)  # D_1 = D_0 (1+g)

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute the intrinsic price and its return decomposition.

        Returns:
            Dictionary with keys ``d1`` (forward dividend), ``price``
            (``D_1/(r-g)``), ``dividend_yield`` (``D_1/P_0``) and
            ``total_return`` (``= r = dividend_yield + g``).
        """
        d1 = self.forward_dividend()  # D_1
        price = d1 / (self.required_return - self.growth)  # P_0 = D_1/(r-g)
        dividend_yield = d1 / price  # equals r - g by construction
        result: dict[str, Any] = {
            "d1": d1,
            "price": price,
            "dividend_yield": dividend_yield,
            "capital_gains_yield": self.growth,  # price grows at g
            "total_return": self.required_return,  # r = dividend yield + g
        }
        self._logger.info(
            "Gordon price=%.6f (D1=%.6f, yield=%.4f)", price, d1, dividend_yield
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        res = self.calculate()
        d0_note = (
            f"D_1 supplied directly = {res['d1']:.6f}"
            if self.dividend_is_forward
            else f"D_1 = D_0(1+g) = {self.dividend}·(1+{self.growth}) = {res['d1']:.6f}"
        )
        return (
            "### Gordon Growth Model — Constant-Growth Dividend Discount\n\n"
            "Summing a perpetually growing dividend stream discounted at ``r`` "
            "collapses to a closed form:\n\n"
            r"$$P_0 = \sum_{t=1}^{\infty} \frac{D_0 (1+g)^t}{(1+r)^t} "
            r"= \frac{D_1}{r-g}, \qquad r > g$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- Inputs: dividend = {self.dividend}, r = {self.required_return}, "
            f"g = {self.growth} ({'forward D1' if self.dividend_is_forward else 'trailing D0'})\n"
            f"- {d0_note}\n"
            f"- **Price P_0 = D_1/(r-g) = {res['d1']:.6f}/"
            f"({self.required_return}-{self.growth}) = {res['price']:.6f}**\n"
            f"- Return decomposition: dividend yield = {res['dividend_yield']:.4f} + "
            f"capital-gains yield {self.growth:.4f} = total return "
            f"{res['total_return']:.4f}\n\n"
            "As $g \\to r$ the denominator $r-g \\to 0$ and the price diverges to "
            "infinity — the model's key sensitivity."
        )

    def visualize(
        self,
        *,
        growth_points: int = 200,
        margin: float = 0.005,
        **kwargs: Any,
    ) -> "go.Figure":
        """Plot price sensitivity as growth ``g`` varies (holding ``r`` fixed).

        Args:
            growth_points: Number of points on the growth grid.
            margin: Gap kept below ``r`` so the last point stays finite; the grid
                spans a symmetric range up to ``r - margin``.

        Returns:
            A Plotly figure of ``P_0(g)`` with a vertical marker at the current
            ``g`` and the ``g -> r`` asymptote drawn as a dotted line.
        """
        import plotly.graph_objects as go

        self._require_positive(margin, "margin")
        r = self.required_return
        # Sweep g from below the current value up to (but not touching) r.
        lo = min(self.growth, 0.0) - (r - self.growth)  # symmetric-ish lower bound
        g_grid = np.linspace(lo, r - margin, int(growth_points))
        d1_grid = self.dividend if self.dividend_is_forward else self.dividend * (1.0 + g_grid)
        prices = d1_grid / (r - g_grid)  # vectorised P_0(g) = D_1/(r-g)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=g_grid, y=prices, name="P₀(g)", line=dict(width=3)))
        # Current operating point.
        fig.add_trace(go.Scatter(
            x=[self.growth], y=[self.calculate()["price"]], mode="markers",
            marker=dict(color="red", size=11), name="Current g"))
        fig.add_vline(x=self.growth, line_dash="dash",
                      annotation_text=f"g = {self.growth}")
        # Asymptote at g -> r.
        fig.add_vline(x=r, line_dash="dot", line_color="grey",
                      annotation_text=f"asymptote g → r = {r}")
        fig.update_layout(
            title="Gordon Growth: Price Sensitivity to Growth Rate",
            xaxis_title="Perpetual growth rate g", yaxis_title="Intrinsic price P₀",
            template="plotly_white", hovermode="x unified",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return the canonical D1=2, r=0.08, g=0.03 -> price=40 identity."""
        m = cls(dividend=2.0, required_return=0.08, growth=0.03, dividend_is_forward=True)
        price = m.calculate()["price"]
        # From-D0 form: D_0 = 2 grown at g gives D_1 = 2.06, price = 2.06/0.05.
        m0 = cls(dividend=2.0, required_return=0.08, growth=0.03, dividend_is_forward=False)
        price0 = m0.calculate()["price"]
        return [
            Benchmark("Textbook D1=2, r=0.08, g=0.03 -> 40", price, 40.0,
                      rel_tol=1e-12, source="Gordon (1959) growing-perpetuity value"),
            Benchmark("From-D0 identity D1=D0(1+g)", price0, 2.0 * 1.03 / 0.05,
                      rel_tol=1e-12, source="Gordon (1959) constant-growth identity"),
        ]
