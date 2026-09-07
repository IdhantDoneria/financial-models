"""Reverse DCF — market-implied expectations solver.

Inverts the standard discounted-cash-flow relationship: instead of taking a
growth assumption and producing a price, this model takes the *current
market price* and numerically solves for the constant 5-year free-cash-flow
CAGR that the price already implies, holding the discount rate and terminal
growth fixed. It then translates that implied growth into an implied share
of a disclosed total addressable market (TAM) — a second, independent lens
on whether the market's expectation is plausible.

Formula (root-find on the standard DCF identity)::

    EV(price) = price * shares_outstanding + net_debt
    find g_cagr such that:
        EV(g_cagr) = sum_t base_fcf*(1+g_cagr)^t / (1+r)^t + PV(TV(g_cagr))
                    = EV(price)

``EV(g_cagr)`` is monotonically increasing in ``g_cagr`` for a positive base
FCF (more growth -> more cash flow -> more value), so the root is unique and
found with :func:`scipy.optimize.brentq` — the same bracketed, guaranteed-
convergent solver this codebase already uses for the Heston model's
volatility surface (see ``src/stochastic_volatility.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scipy.optimize import brentq

from .base_model import BaseFinancialModel, Benchmark, ValidationError
from .dcf import DiscountedCashFlowModel

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go

#: Search bracket for the implied CAGR — wide enough to cover a distressed
#: (-50%/yr) to a hyper-growth "story stock" (+200%/yr) expectation without
#: pretending a market can imply something outside any plausible range.
_CAGR_LO = -0.50
_CAGR_HI = 2.00


class ReverseDCFModel(BaseFinancialModel):
    """Solve for the FCF growth rate and TAM capture a market price implies.

    Unlike the forward :class:`~src.dcf.DiscountedCashFlowModel` (assumption
    in, price out), this model takes the price as given and asks: *what
    growth assumption would an analyst need to believe to justify paying
    this price?* It reuses ``DiscountedCashFlowModel`` internally for the
    enterprise-value formula itself — this file contributes only the
    root-finding and TAM-capture translation, not a second copy of the DCF
    identity.

    Example:
        >>> m = ReverseDCFModel(current_price=42.0, shares_outstanding=100,
        ...     net_debt=200, base_fcf=100, base_revenue=1000,
        ...     total_addressable_market=10000, years=5,
        ...     discount_rate=0.10, terminal_growth=0.03)
        >>> res = m.calculate()
        >>> res["implied_fcf_cagr"] is not None
        True
    """

    name = "Reverse DCF / Market-Implied Expectations"
    category = "Market-Implied"
    references = [
        "Rappaport, A. & Mauboussin, M. J. (2001). Expectations Investing: "
        "Reading Stock Prices for Better Returns. Boston: Harvard Business "
        "School Press — the reverse-DCF / expectations-investing method.",
        "Damodaran, A. (2012). Investment Valuation, 3rd ed. — DCF identity "
        "inverted here is the same enterprise-value formula as Chapter 12.",
        "Virtanen, P. et al. (2020). SciPy 1.0: Fundamental Algorithms for "
        "Scientific Computing in Python. Nature Methods 17, 261-272 "
        "(Brent's method, used here via scipy.optimize.brentq).",
    ]

    def __init__(
        self,
        *,
        current_price: float,
        shares_outstanding: float,
        net_debt: float,
        base_fcf: float,
        base_revenue: float,
        total_addressable_market: float,
        years: int = 5,
        discount_rate: float,
        terminal_growth: float,
        logger: Any = None,
    ) -> None:
        """Initialise and validate every input to the reverse solve.

        Args:
            current_price: The market price per share to explain, ``> 0``.
                Either typed in by hand or pulled live — this class does not
                care which; see the terminal UI for that distinction and its
                disclosure requirements.
            shares_outstanding: Share count ``> 0``.
            net_debt: Net debt (debt minus cash); any finite value.
            base_fcf: Trailing free cash flow to project forward, ``> 0``
                (the reverse solve is only well-posed for a currently
                cash-generative business — see the guard below).
            base_revenue: Trailing revenue, ``> 0``, used to translate the
                implied FCF growth into an implied revenue path.
            total_addressable_market: Disclosed/assumed TAM in revenue
                terms, ``> 0``, the denominator of the implied capture rate.
            years: Forecast horizon in whole years, ``>= 1``. Defaults to
                ``5`` (the brief's "5-year" horizon).
            discount_rate: WACC ``r > 0``.
            terminal_growth: Perpetual terminal growth rate ``g``; must
                satisfy ``discount_rate > terminal_growth``.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If any input is out of its valid domain, or if
                ``discount_rate <= terminal_growth`` (the underlying DCF
                terminal value would not converge).
        """
        super().__init__(logger=logger)
        self.current_price = self._require_positive(current_price, "current_price")
        self.shares_outstanding = self._require_positive(
            shares_outstanding, "shares_outstanding")
        self.net_debt = self._as_finite_float(net_debt, "net_debt")
        self.base_fcf = self._require_positive(base_fcf, "base_fcf")
        self.base_revenue = self._require_positive(base_revenue, "base_revenue")
        self.total_addressable_market = self._require_positive(
            total_addressable_market, "total_addressable_market")
        self.years = int(self._require_positive(years, "years"))
        self.discount_rate = self._require_positive(discount_rate, "discount_rate")
        self.terminal_growth = self._as_finite_float(terminal_growth, "terminal_growth")

        # Same convergence requirement as the forward DCF — checked here too
        # so the reverse solve fails fast with a clear message instead of
        # deep inside the solver's bracket evaluation.
        if self.discount_rate <= self.terminal_growth:
            raise ValidationError(
                f"'discount_rate' ({self.discount_rate!r}) must exceed "
                f"'terminal_growth' ({self.terminal_growth!r}) for the "
                "underlying DCF terminal value to converge."
            )
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def _implied_ev(self) -> float:
        """Enterprise value implied by the market price: ``P*shares + net_debt``."""
        return self.current_price * self.shares_outstanding + self.net_debt

    def _ev_at_growth(self, cagr: float) -> float:
        """Enterprise value the forward DCF produces at a given constant FCF CAGR.

        Delegates to :class:`DiscountedCashFlowModel` for the actual PV +
        terminal-value formula — this is the one place the two models share
        code, by composition rather than duplicating the identity.
        """
        fcfs = [self.base_fcf * (1.0 + cagr) ** t for t in range(1, self.years + 1)]
        forward = DiscountedCashFlowModel(
            free_cash_flows=fcfs, discount_rate=self.discount_rate,
            terminal_growth=self.terminal_growth, net_debt=0.0,
        )
        return forward.calculate()["enterprise_value"]

    def solve_implied_cagr(self) -> tuple[float | None, str]:
        """Root-find the FCF CAGR implied by the market price.

        Returns:
            ``(cagr, note)`` — ``cagr`` is ``None`` when the implied price is
            outside the ``[-50%, +200%]`` search bracket (the price cannot be
            justified even at hyper-growth, or is below what a shrinking-to-
            zero FCF path would support); ``note`` explains which bound was
            breached in that case, or is empty on a normal solve.
        """
        target = self._implied_ev()
        f_lo = self._ev_at_growth(_CAGR_LO) - target
        f_hi = self._ev_at_growth(_CAGR_HI) - target
        if f_lo > 0:
            return None, (
                f"Price implies a CAGR below {_CAGR_LO:.0%}/yr — even a "
                "collapsing FCF path over-justifies this price at the given "
                "discount rate; check for a data error or an unsustainably low r."
            )
        if f_hi < 0:
            return None, (
                f"Price implies a CAGR above {_CAGR_HI:.0%}/yr — no "
                "plausible growth rate within this model's search range "
                "justifies the price; the market may be pricing in an "
                "optionality this DCF framework does not capture."
            )
        cagr = brentq(lambda g: self._ev_at_growth(g) - target,
                       _CAGR_LO, _CAGR_HI, xtol=1e-12, rtol=1e-12)
        return cagr, ""

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Solve for the implied CAGR and translate it into implied TAM capture.

        Returns:
            Dictionary with keys ``implied_ev``, ``implied_fcf_cagr``
            (``None`` if unsolvable — see :meth:`solve_implied_cagr`),
            ``solver_note``, ``implied_revenue_year_n`` and
            ``implied_tam_capture`` (both ``None`` when the CAGR could not
            be solved).
        """
        implied_ev = self._implied_ev()
        cagr, note = self.solve_implied_cagr()

        implied_revenue_year_n = None
        implied_tam_capture = None
        if cagr is not None:
            # Simplifying assumption, stated plainly in explain(): revenue
            # grows at the same CAGR the solver found for FCF (a constant
            # FCF-margin assumption) so the two lenses share one growth path.
            implied_revenue_year_n = self.base_revenue * (1.0 + cagr) ** self.years
            implied_tam_capture = implied_revenue_year_n / self.total_addressable_market

        result: dict[str, Any] = {
            "implied_ev": implied_ev,
            "implied_fcf_cagr": cagr,
            "solver_note": note,
            "implied_revenue_year_n": implied_revenue_year_n,
            "implied_tam_capture": implied_tam_capture,
        }
        self._logger.info(
            "Implied EV=%.6f, implied FCF CAGR=%s, implied TAM capture=%s",
            implied_ev, cagr, implied_tam_capture,
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation plus a plain-language read of the implied growth."""
        res = self.calculate()
        if res["implied_fcf_cagr"] is None:
            solved = f"**Not solvable within [{_CAGR_LO:.0%}, {_CAGR_HI:.0%}]/yr** — {res['solver_note']}"
        else:
            solved = (
                f"**Implied {self.years}-year FCF CAGR = {res['implied_fcf_cagr']:.2%}/yr**, "
                f"implying year-{self.years} revenue of {res['implied_revenue_year_n']:.4f} "
                f"— **{res['implied_tam_capture']:.2%} of the assumed "
                f"{self.total_addressable_market:g} TAM**."
            )
        return (
            "### Reverse DCF — Market-Implied Expectations\n\n"
            "A forward DCF takes a growth assumption and produces a price. This "
            "inverts it: given today's price, what constant FCF growth rate would "
            "justify it?\n\n"
            r"$$EV(price) = P \cdot \text{shares} + \text{net debt} "
            r"\stackrel{!}{=} \sum_{t=1}^{N}\frac{FCF_0(1+g)^t}{(1+r)^t}"
            r"+\frac{1}{(1+r)^N}\cdot\frac{FCF_0(1+g)^N(1+g_\infty)}{r-g_\infty}$$"
            "\n\nSolved for the unique ``g`` (Brent's method — the relationship is "
            "monotonic, so the root is unique when it exists in-range).\n\n"
            "**Worked example (current inputs):**\n"
            f"- Price {self.current_price:g} × {self.shares_outstanding:g} shares + "
            f"net debt {self.net_debt:g} = implied EV **{res['implied_ev']:.4f}**\n"
            f"- {solved}\n\n"
            "**Why this differs from a forward DCF:** a forward DCF is only as good "
            "as the growth assumption fed into it; this flips the question to "
            "*what does the current price assume*, so you can judge that assumption "
            "against the company's actual TAM and history rather than assuming "
            "your own growth rate is right.\n"
        )

    def visualize(self, **kwargs: Any) -> "go.Figure":
        """Plot enterprise value vs. FCF CAGR, marking the price-implied solution."""
        import numpy as np
        import plotly.graph_objects as go

        res = self.calculate()
        grid = np.linspace(_CAGR_LO, _CAGR_HI, 121)
        ev = np.array([self._ev_at_growth(g) for g in grid])

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=grid, y=ev, mode="lines", name="EV(g)",
                                  line=dict(color="#3f6fd6")))
        fig.add_hline(y=res["implied_ev"], line=dict(color="#c0392b", dash="dash"),
                      annotation_text="Price-implied EV")
        if res["implied_fcf_cagr"] is not None:
            fig.add_trace(go.Scatter(
                x=[res["implied_fcf_cagr"]], y=[res["implied_ev"]], mode="markers",
                marker=dict(color="black", size=11, symbol="x"), name="Solved g"))
        fig.update_layout(
            title="Enterprise Value vs. FCF Growth — Where the Market Price Sits",
            xaxis_title="Constant FCF CAGR (g)", yaxis_title="Enterprise value",
            xaxis_tickformat=".0%", template="plotly_white",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return a round-trip solver identity plus a TAM-capture arithmetic identity."""
        # (a) Round-trip: price a DCF at a KNOWN cagr, feed that price back in,
        #     and confirm the solver recovers the same cagr to high precision.
        known_cagr, base_fcf, r, g, years = 0.12, 100.0, 0.10, 0.03, 5
        shares, net_debt = 100.0, 200.0
        fcfs = [base_fcf * (1.0 + known_cagr) ** t for t in range(1, years + 1)]
        priced_ev = DiscountedCashFlowModel(
            free_cash_flows=fcfs, discount_rate=r, terminal_growth=g, net_debt=0.0
        ).calculate()["enterprise_value"]
        implied_price = (priced_ev - net_debt) / shares
        solver_case = cls(
            current_price=implied_price, shares_outstanding=shares, net_debt=net_debt,
            base_fcf=base_fcf, base_revenue=1000.0, total_addressable_market=10000.0,
            years=years, discount_rate=r, terminal_growth=g,
        )
        recovered_cagr, _ = solver_case.solve_implied_cagr()

        # (b) TAM capture: pure arithmetic identity, independently re-derived.
        base_revenue, tam = 1000.0, 10000.0
        expected_capture = (base_revenue * (1.0 + known_cagr) ** years) / tam
        computed_capture = solver_case.calculate()["implied_tam_capture"]

        return [
            Benchmark("Round-trip solver recovers known CAGR", recovered_cagr, known_cagr,
                      rel_tol=1e-9, source="Internal consistency: price a DCF at a known "
                      "growth rate, solve the reverse DCF on that price, recover the rate."),
            Benchmark("Implied TAM capture arithmetic identity", computed_capture,
                      expected_capture, rel_tol=1e-12,
                      source="Direct re-derivation: base_revenue*(1+g)^years / TAM."),
        ]
