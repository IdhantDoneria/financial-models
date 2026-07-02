"""Discounted Cash Flow (DCF) intrinsic-valuation model.

Values a business as the present value of its projected free cash flows plus a
Gordon-growth terminal value beyond the explicit forecast horizon.

Formula (see Damodaran, *Investment Valuation*)::

    PV(FCF_t) = FCF_t / (1 + r)^t                for t = 1..N
    TV        = FCF_N * (1 + g) / (r - g)         (Gordon perpetuity at year N)
    PV(TV)    = TV / (1 + r)^N
    EV        = sum_t PV(FCF_t) + PV(TV)          (enterprise value)
    Equity    = EV - net_debt
    Price     = Equity / shares_outstanding

where ``r`` is the WACC (discount rate) and ``g`` the perpetual terminal growth
rate, which must satisfy ``r > g`` for the perpetuity to converge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go


class DiscountedCashFlowModel(BaseFinancialModel):
    """Value a firm from explicit free cash flows plus a Gordon terminal value.

    Assumptions:
        * Free cash flows are forecast for ``N`` explicit years, received at
          year-ends ``t = 1..N``.
        * Beyond year ``N`` cash flows grow forever at a constant rate ``g`` and
          are capitalised with the Gordon growing perpetuity.
        * A single, constant discount rate ``r`` (the WACC) applies to every
          period, and ``r > g`` (otherwise the perpetuity diverges).

    Example:
        >>> m = DiscountedCashFlowModel(free_cash_flows=[100, 110, 121],
        ...                             discount_rate=0.10, terminal_growth=0.03)
        >>> round(m.calculate()["enterprise_value"], 4)
        1610.3896
    """

    name = "Discounted Cash Flow"
    category = "Valuation"
    references = [
        "Damodaran, A. (2012). Investment Valuation: Tools and Techniques for "
        "Determining the Value of Any Asset, 3rd ed. Hoboken, NJ: Wiley.",
        "Gordon, M. J. (1962). The Investment, Financing, and Valuation of the "
        "Corporation. Homewood, IL: Richard D. Irwin (terminal-value perpetuity).",
    ]

    def __init__(
        self,
        *,
        free_cash_flows: Sequence[float] | np.ndarray,
        discount_rate: float,
        terminal_growth: float,
        net_debt: float = 0.0,
        shares_outstanding: float | None = None,
        logger: Any = None,
    ) -> None:
        """Initialise and validate all valuation inputs.

        Args:
            free_cash_flows: Projected free cash flows ``FCF_1..FCF_N`` (each
                finite), received at the end of years ``1..N``.
            discount_rate: Weighted average cost of capital ``r > 0``.
            terminal_growth: Perpetual terminal growth rate ``g``; must satisfy
                ``discount_rate > terminal_growth``.
            net_debt: Net debt (debt minus cash), any finite value; a negative
                value denotes a net-cash position. Defaults to ``0.0``.
            shares_outstanding: Optional share count ``> 0`` used to derive a
                per-share value. Defaults to ``None`` (per-share omitted).
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If any input is out of its valid domain or if
                ``discount_rate <= terminal_growth`` (perpetuity diverges).
        """
        super().__init__(logger=logger)
        # FCF vector: non-empty, all finite (validated by the base helper).
        self.free_cash_flows = self._as_float_array(free_cash_flows, "free_cash_flows")
        self.discount_rate = self._require_positive(discount_rate, "discount_rate")  # r
        self.terminal_growth = self._as_finite_float(terminal_growth, "terminal_growth")  # g
        self.net_debt = self._as_finite_float(net_debt, "net_debt")
        self.shares_outstanding = (
            None
            if shares_outstanding is None
            else self._require_positive(shares_outstanding, "shares_outstanding")
        )
        # Gordon perpetuity converges only when r > g.
        if self.discount_rate <= self.terminal_growth:
            raise ValidationError(
                f"'discount_rate' ({self.discount_rate!r}) must exceed "
                f"'terminal_growth' ({self.terminal_growth!r}) for the terminal "
                "value to converge."
            )
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def _present_values(self) -> tuple[np.ndarray, float, float]:
        """Return ``(pv_explicit, terminal_value, pv_terminal)``.

        Returns:
            A tuple of the per-period discounted explicit cash flows
            ``FCF_t/(1+r)^t``, the undiscounted Gordon terminal value ``TV`` at
            year ``N``, and its present value ``TV/(1+r)^N``.
        """
        n = self.free_cash_flows.size
        t = np.arange(1, n + 1)  # periods 1..N
        growth = (1.0 + self.discount_rate) ** t  # (1+r)^t
        pv_explicit = self.free_cash_flows / growth  # FCF_t / (1+r)^t
        fcf_n = float(self.free_cash_flows[-1])  # FCF_N (last explicit cash flow)
        # Gordon growing perpetuity valued at the end of year N.
        terminal_value = fcf_n * (1.0 + self.terminal_growth) / (
            self.discount_rate - self.terminal_growth
        )
        # Discount TV back N years to present: TV / (1+r)^N.
        pv_terminal = terminal_value / (1.0 + self.discount_rate) ** n
        return pv_explicit, float(terminal_value), float(pv_terminal)

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute enterprise, equity and (optionally) per-share value.

        Returns:
            Dictionary with keys ``pv_explicit`` (list of per-year present
            values), ``terminal_value``, ``pv_terminal``, ``enterprise_value``,
            ``equity_value`` and ``price_per_share`` (``None`` when no share
            count was supplied).
        """
        pv_explicit, terminal_value, pv_terminal = self._present_values()
        # EV = sum of discounted explicit FCFs + discounted terminal value.
        enterprise_value = float(pv_explicit.sum() + pv_terminal)
        equity_value = enterprise_value - self.net_debt  # bridge EV -> equity
        price_per_share = (
            None
            if self.shares_outstanding is None
            else equity_value / self.shares_outstanding
        )
        result: dict[str, Any] = {
            "pv_explicit": pv_explicit.tolist(),
            "terminal_value": terminal_value,
            "pv_terminal": pv_terminal,
            "enterprise_value": enterprise_value,
            "equity_value": equity_value,
            "price_per_share": price_per_share,
        }
        self._logger.info(
            "DCF enterprise value=%.6f, equity value=%.6f", enterprise_value, equity_value
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation with the configured worked example."""
        res = self.calculate()
        n = self.free_cash_flows.size
        pv_terms = ", ".join(f"{pv:.4f}" for pv in res["pv_explicit"])
        share_line = (
            f"- **Price per share = {res['price_per_share']:.6f}** "
            f"(equity / {self.shares_outstanding:g} shares)\n"
            if res["price_per_share"] is not None
            else "- Price per share: not computed (no share count supplied)\n"
        )
        return (
            "### Discounted Cash Flow — Intrinsic Valuation\n\n"
            "Enterprise value is the present value of explicit free cash flows "
            "plus the discounted Gordon terminal value:\n\n"
            r"$$EV = \sum_{t=1}^{N} \frac{FCF_t}{(1+r)^t} "
            r"+ \frac{1}{(1+r)^N}\cdot\frac{FCF_N (1+g)}{r-g}$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- FCFs = {self.free_cash_flows.tolist()}, r = {self.discount_rate}, "
            f"g = {self.terminal_growth}, net debt = {self.net_debt}\n"
            f"- PV of explicit FCFs (t=1..{n}): [{pv_terms}]\n"
            f"- Terminal value at year {n} = {res['terminal_value']:.6f}, "
            f"PV(TV) = {res['pv_terminal']:.6f}\n"
            f"- **Enterprise value = {res['enterprise_value']:.6f}**, "
            f"equity value = {res['equity_value']:.6f}\n"
            f"{share_line}"
            "\nRequires $r > g$; here the terminal value contributes "
            f"{100 * res['pv_terminal'] / res['enterprise_value']:.1f}% of EV."
        )

    def visualize(
        self,
        *,
        rate_span: float = 0.03,
        growth_span: float = 0.02,
        grid_size: int = 41,
        metric: str | None = None,
        **kwargs: Any,
    ) -> "go.Figure":
        """Plot a sensitivity heatmap of value across ``r`` x ``g``.

        Args:
            rate_span: Half-width of the discount-rate axis around the base ``r``.
            growth_span: Half-width of the terminal-growth axis around base ``g``.
            grid_size: Number of grid points per axis.
            metric: ``"enterprise_value"``, ``"equity_value"`` or ``"per_share"``.
                Defaults to ``"per_share"`` when a share count is available, else
                ``"enterprise_value"``.

        Returns:
            A Plotly figure with a :class:`~plotly.graph_objects.Heatmap` of the
            chosen value metric; cells where ``r <= g`` are left blank (NaN).
        """
        import plotly.graph_objects as go

        self._require_positive(rate_span, "rate_span")
        self._require_positive(growth_span, "growth_span")
        if metric is None:
            metric = "per_share" if self.shares_outstanding is not None else "enterprise_value"
        # Axis grids centred on the base inputs (discount rate kept strictly > 0).
        r_grid = np.linspace(max(1e-4, self.discount_rate - rate_span),
                             self.discount_rate + rate_span, int(grid_size))
        g_grid = np.linspace(self.terminal_growth - growth_span,
                             self.terminal_growth + growth_span, int(grid_size))
        ev_grid = self._enterprise_value_grid(r_grid, g_grid)  # shape (len g, len r)
        if metric == "enterprise_value":
            z, label = ev_grid, "Enterprise value"
        elif metric == "equity_value":
            z, label = ev_grid - self.net_debt, "Equity value"
        elif metric == "per_share":
            if self.shares_outstanding is None:
                raise ValidationError("'per_share' metric requires 'shares_outstanding'.")
            z, label = (ev_grid - self.net_debt) / self.shares_outstanding, "Value per share"
        else:
            raise ValidationError(f"Unknown metric {metric!r}.")

        fig = go.Figure(
            data=go.Heatmap(x=r_grid, y=g_grid, z=z, colorscale="RdBu",
                            colorbar=dict(title=label), hoverongaps=False)
        )
        # Mark the base-case cell.
        fig.add_trace(go.Scatter(
            x=[self.discount_rate], y=[self.terminal_growth], mode="markers",
            marker=dict(color="black", size=11, symbol="x"), name="Base case"))
        fig.update_layout(
            title=f"DCF Sensitivity: {label} vs. Discount Rate x Terminal Growth",
            xaxis_title="Discount rate (WACC) r", yaxis_title="Terminal growth g",
            template="plotly_white",
        )
        return fig

    def _enterprise_value_grid(self, r_grid: np.ndarray, g_grid: np.ndarray) -> np.ndarray:
        """Vectorised enterprise value over a ``(g, r)`` grid.

        Args:
            r_grid: 1-D array of discount rates (heatmap x-axis / columns).
            g_grid: 1-D array of terminal growth rates (heatmap y-axis / rows).

        Returns:
            2-D array of enterprise values with shape ``(len(g_grid), len(r_grid))``;
            entries where ``r <= g`` are ``NaN``.
        """
        n = self.free_cash_flows.size
        t = np.arange(1, n + 1)[:, None]  # column vector of periods 1..N
        # Sum of discounted explicit FCFs depends only on r: S(r) per column.
        s_explicit = (self.free_cash_flows[:, None] / (1.0 + r_grid[None, :]) ** t).sum(axis=0)
        r_mesh, g_mesh = np.meshgrid(r_grid, g_grid)  # shape (len g, len r)
        fcf_n = float(self.free_cash_flows[-1])
        with np.errstate(divide="ignore", invalid="ignore"):
            terminal = fcf_n * (1.0 + g_mesh) / (r_mesh - g_mesh)  # TV(r,g)
            pv_terminal = terminal / (1.0 + r_mesh) ** n  # discount TV by (1+r)^N
            ev = s_explicit[None, :] + pv_terminal  # broadcast S(r) across rows
        ev[r_mesh <= g_mesh] = np.nan  # perpetuity diverges when r <= g
        return ev

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return a machine-precision identity plus a worked-example cross-check."""
        # (a) One-period horizon: EV must equal the closed-form perpetuity sum.
        fcf1, r, g = 100.0, 0.10, 0.03
        one = cls(free_cash_flows=[fcf1], discount_rate=r, terminal_growth=g)
        ev_one = one.calculate()["enterprise_value"]
        expected_one = fcf1 / (1 + r) + (fcf1 * (1 + g) / (r - g)) / (1 + r)
        # (b) Three-year worked example; expected EV = 15004000/9317 computed by
        #     the explicit formula (100/1.1 + 110/1.21 + 121/1.331 + PV of TV).
        three = cls(free_cash_flows=[100, 110, 121], discount_rate=0.10,
                    terminal_growth=0.03, net_debt=0.0)
        ev_three = three.calculate()["enterprise_value"]
        return [
            Benchmark("Single-period Gordon identity", ev_one, float(expected_one),
                      rel_tol=1e-12, source="Closed-form growing-perpetuity identity"),
            Benchmark("3-year worked example EV", ev_three, 1610.3896103896104,
                      rel_tol=1e-9, source="Damodaran (2012), explicit DCF formula"),
        ]
