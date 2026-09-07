"""Ind AS 116 hidden-debt and forensic cash-flow normalizer.

Bridges a company's *reported* net debt and equity value to an *adjusted*
figure that recognises three categories of economically-real leverage that
standard ratio analysis routinely misses, plus a Buffett-style owner-earnings
recomputation that strips out the smoothing effect of capitalised R&D.

Formula (capitalised operating lease liability, Ind AS 116 / IFRS 16)::

    L = C * (1 - (1+r)^-n) / r        (present value of an ordinary annuity)

where ``C`` is the annual lease payment, ``r`` the incremental borrowing
rate and ``n`` the remaining lease term in years. The lease liability,
disclosed reverse-factoring/supply-chain-finance exposure, and the
probability-weighted expected value of disclosed contingent liabilities are
all forms of leverage that sit in footnotes rather than the balance sheet;
this model moves them onto it::

    hidden_debt   = L + reverse_factoring + Σ(contingent_amount_i * prob_i)
    adjusted_debt = reported_net_debt + hidden_debt
    adjusted_equity_value = reported_equity_value - hidden_debt

Owner earnings (Buffett, 1986) reverse the accounting smoothing of
capitalised R&D: the amortisation already embedded in reported net income is
added back, and this period's actual cash R&D spend is expensed in full
instead, alongside the standard D&A add-back and maintenance-capex
deduction::

    owner_earnings = net_income + D&A + rd_amortization - rd_cash_spend - maint_capex
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base_model import BaseFinancialModel, Benchmark, ValidationError

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go


class IndASHiddenDebtModel(BaseFinancialModel):
    """Adjust reported net debt / equity value for off-balance-sheet leverage.

    Recognises three footnote-disclosed items most ratio analysis ignores:

    1. **Capitalised operating leases** (Ind AS 116 / IFRS 16) — the present
       value of remaining lease payments, discounted at the incremental
       borrowing rate, as a debt-equivalent liability.
    2. **Reverse factoring / supply-chain finance** — a disclosed exposure
       amount added directly to net debt (economically borrowing, reported
       as trade payables).
    3. **Contingent liabilities** — probability-weighted rather than ignored
       outright: ``expected value = disclosed amount * probability``.

    It also recomputes Buffett-style *owner earnings*, reversing the effect
    of capitalised R&D so earnings quality is comparable to a company that
    expenses R&D as incurred.

    Example:
        >>> m = IndASHiddenDebtModel(
        ...     net_income=100, reported_net_debt=500, reported_equity_value=2000,
        ...     shares_outstanding=100, annual_lease_payment=50, lease_term_years=3,
        ...     lease_discount_rate=0.08, reverse_factoring_exposure=75,
        ...     cl1_amount=200, cl1_probability=0.25, cl2_amount=100, cl2_probability=0.10)
        >>> round(m.calculate()["adjusted_net_debt"], 2)
        763.85
    """

    name = "Ind AS 116 Hidden-Debt Normalizer"
    category = "Forensic Accounting"
    references = [
        "Institute of Chartered Accountants of India / IASB. Ind AS 116 / "
        "IFRS 16, Leases (2016) — lessee capitalises the present value of "
        "remaining lease payments as a right-of-use liability.",
        "IASB. Amendments to IAS 7 and IFRS 7: Supply Chain Financing "
        "Arrangements (2023) — disclosure of reverse-factoring exposure.",
        "Buffett, W. E. (1986). Berkshire Hathaway Shareholder Letter — "
        "definition of owner earnings (net income + D&A − maintenance capex, "
        "adjusted for non-cash smoothing items).",
        "Damodaran, A. (2012). Investment Valuation, 3rd ed. — treatment of "
        "operating leases and R&D as debt-like / capital-like items.",
    ]

    def __init__(
        self,
        *,
        net_income: float,
        reported_net_debt: float,
        reported_equity_value: float,
        shares_outstanding: float,
        annual_lease_payment: float,
        lease_term_years: int,
        lease_discount_rate: float,
        reverse_factoring_exposure: float = 0.0,
        cl1_amount: float = 0.0,
        cl1_probability: float = 0.0,
        cl2_amount: float = 0.0,
        cl2_probability: float = 0.0,
        depreciation_amortization: float = 0.0,
        rd_capitalized_amortization: float = 0.0,
        rd_cash_spend: float = 0.0,
        maintenance_capex: float = 0.0,
        logger: Any = None,
    ) -> None:
        """Initialise and validate every reported figure and footnote input.

        Args:
            net_income: Reported net income (may be negative — a loss).
            reported_net_debt: Total debt minus cash, as reported (negative
                denotes a net-cash position).
            reported_equity_value: The equity value being tested — market
                cap, or another model's equity output, before adjustment.
            shares_outstanding: Share count ``> 0``, for per-share values.
            annual_lease_payment: Disclosed annual minimum lease payment
                under Ind AS 116/IFRS 16 footnotes, ``>= 0``.
            lease_term_years: Remaining lease term in whole years, ``>= 1``.
            lease_discount_rate: Incremental borrowing rate ``r > 0`` used to
                discount the lease payments.
            reverse_factoring_exposure: Disclosed reverse-factoring / supply
                -chain-financing exposure, ``>= 0``. Defaults to ``0.0``.
            cl1_amount: Disclosed amount of the first contingent liability,
                ``>= 0``. Defaults to ``0.0``.
            cl1_probability: Probability of ``cl1_amount`` crystallising,
                in ``[0, 1]``. Defaults to ``0.0``.
            cl2_amount: Disclosed amount of a second contingent liability,
                ``>= 0``. Defaults to ``0.0``.
            cl2_probability: Probability of ``cl2_amount`` crystallising,
                in ``[0, 1]``. Defaults to ``0.0``.
            depreciation_amortization: Reported D&A add-back, ``>= 0``.
                Defaults to ``0.0``.
            rd_capitalized_amortization: Amortisation of previously
                capitalised R&D already embedded in ``net_income``, added
                back, ``>= 0``. Defaults to ``0.0``.
            rd_cash_spend: This period's actual cash R&D spend, expensed in
                full for owner earnings, ``>= 0``. Defaults to ``0.0``.
            maintenance_capex: Capex required to sustain (not grow) the
                business, ``>= 0``. Defaults to ``0.0``.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If any input is out of its valid domain, or if
                the reported baseline itself is not economically meaningful
                (see the enterprise-value guard below).
        """
        super().__init__(logger=logger)
        self.net_income = self._as_finite_float(net_income, "net_income")
        self.reported_net_debt = self._as_finite_float(reported_net_debt, "reported_net_debt")
        self.reported_equity_value = self._as_finite_float(
            reported_equity_value, "reported_equity_value")
        self.shares_outstanding = self._require_positive(
            shares_outstanding, "shares_outstanding")
        self.annual_lease_payment = self._require_nonnegative(
            annual_lease_payment, "annual_lease_payment")
        self.lease_term_years = int(self._require_positive(
            lease_term_years, "lease_term_years"))
        self.lease_discount_rate = self._require_positive(
            lease_discount_rate, "lease_discount_rate")  # r — annuity denominator
        self.reverse_factoring_exposure = self._require_nonnegative(
            reverse_factoring_exposure, "reverse_factoring_exposure")
        self.cl1_amount = self._require_nonnegative(cl1_amount, "cl1_amount")
        self.cl1_probability = self._require_probability(cl1_probability, "cl1_probability")
        self.cl2_amount = self._require_nonnegative(cl2_amount, "cl2_amount")
        self.cl2_probability = self._require_probability(cl2_probability, "cl2_probability")
        self.depreciation_amortization = self._require_nonnegative(
            depreciation_amortization, "depreciation_amortization")
        self.rd_capitalized_amortization = self._require_nonnegative(
            rd_capitalized_amortization, "rd_capitalized_amortization")
        self.rd_cash_spend = self._require_nonnegative(rd_cash_spend, "rd_cash_spend")
        self.maintenance_capex = self._require_nonnegative(
            maintenance_capex, "maintenance_capex")

        # The reported baseline (equity + net debt = implied reported EV)
        # must itself be economically meaningful before we adjust it further
        # — mirrors DCF's r > g convergence guard: garbage in, garbage out.
        implied_reported_ev = self.reported_equity_value + self.reported_net_debt
        if implied_reported_ev <= 0:
            raise ValidationError(
                f"Implied reported enterprise value (equity "
                f"{self.reported_equity_value!r} + net debt "
                f"{self.reported_net_debt!r} = {implied_reported_ev!r}) must be > 0 "
                "— the reported baseline must be meaningful before it can be adjusted."
            )
        self._logger.debug("Initialised %r", self)

    # ------------------------------------------------------------------ #
    # Core maths
    # ------------------------------------------------------------------ #
    def _lease_liability(self) -> float:
        """PV of remaining lease payments: ``C * (1-(1+r)^-n) / r``."""
        c, r, n = self.annual_lease_payment, self.lease_discount_rate, self.lease_term_years
        return c * (1.0 - (1.0 + r) ** (-n)) / r

    def _weighted_contingent_liabilities(self) -> float:
        """Probability-weighted expected value of the two disclosed contingencies."""
        return (self.cl1_amount * self.cl1_probability
                + self.cl2_amount * self.cl2_probability)

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Compute the hidden-debt bridge and owner-earnings recomputation.

        Returns:
            Dictionary with keys ``lease_liability``, ``reverse_factoring_exposure``,
            ``weighted_contingent_liabilities``, ``hidden_debt_addback``,
            ``adjusted_net_debt``, ``adjusted_equity_value``,
            ``reported_price_per_share``, ``adjusted_price_per_share``,
            ``pct_equity_erosion``, ``owner_earnings`` and
            ``earnings_quality_delta`` (``owner_earnings - net_income``).
        """
        lease_liability = self._lease_liability()
        weighted_cl = self._weighted_contingent_liabilities()
        hidden_debt_addback = (
            lease_liability + self.reverse_factoring_exposure + weighted_cl
        )
        adjusted_net_debt = self.reported_net_debt + hidden_debt_addback
        adjusted_equity_value = self.reported_equity_value - hidden_debt_addback

        owner_earnings = (
            self.net_income + self.depreciation_amortization
            + self.rd_capitalized_amortization - self.rd_cash_spend
            - self.maintenance_capex
        )

        pct_equity_erosion = (
            hidden_debt_addback / self.reported_equity_value
            if self.reported_equity_value != 0 else None
        )

        result: dict[str, Any] = {
            "lease_liability": lease_liability,
            "reverse_factoring_exposure": self.reverse_factoring_exposure,
            "weighted_contingent_liabilities": weighted_cl,
            "hidden_debt_addback": hidden_debt_addback,
            "adjusted_net_debt": adjusted_net_debt,
            "adjusted_equity_value": adjusted_equity_value,
            "reported_price_per_share": self.reported_equity_value / self.shares_outstanding,
            "adjusted_price_per_share": adjusted_equity_value / self.shares_outstanding,
            "pct_equity_erosion": pct_equity_erosion,
            "owner_earnings": owner_earnings,
            "earnings_quality_delta": owner_earnings - self.net_income,
        }
        self._logger.info(
            "Hidden debt add-back=%.6f, adjusted equity=%.6f, owner earnings=%.6f",
            hidden_debt_addback, adjusted_equity_value, owner_earnings,
        )
        return result

    # ------------------------------------------------------------------ #
    # Explanation & visualisation
    # ------------------------------------------------------------------ #
    def explain(self) -> str:
        """Return a Markdown derivation plus a plain-language delta explanation."""
        res = self.calculate()
        erosion = (f"{res['pct_equity_erosion'] * 100:.1f}%"
                   if res["pct_equity_erosion"] is not None else "n/a")
        return (
            "### Ind AS 116 Hidden-Debt & Forensic Cash-Flow Normalizer\n\n"
            "Capitalised operating lease liability (present value of an ordinary annuity):\n\n"
            r"$$L = C \cdot \frac{1-(1+r)^{-n}}{r}$$"
            "\n\nHidden debt add-back combines the capitalised lease, disclosed "
            "reverse-factoring exposure, and probability-weighted contingent liabilities:\n\n"
            r"$$\text{Hidden debt} = L + F + \sum_i A_i \cdot p_i$$"
            "\n\n**Worked example (current inputs):**\n"
            f"- Capitalised lease liability = {res['lease_liability']:.4f} "
            f"({self.annual_lease_payment:g}/yr × {self.lease_term_years}y @ "
            f"{self.lease_discount_rate:.2%})\n"
            f"- Reverse-factoring exposure = {res['reverse_factoring_exposure']:.4f}\n"
            f"- Probability-weighted contingent liabilities = "
            f"{res['weighted_contingent_liabilities']:.4f} "
            f"({self.cl1_amount:g}×{self.cl1_probability:.0%} + "
            f"{self.cl2_amount:g}×{self.cl2_probability:.0%})\n"
            f"- **Total hidden debt add-back = {res['hidden_debt_addback']:.4f}**\n\n"
            "**Why this differs from the reported number:** moving the hidden debt "
            f"onto the balance sheet takes reported equity value "
            f"{self.reported_equity_value:.4f} down to adjusted equity value "
            f"**{res['adjusted_equity_value']:.4f}** — a {erosion} erosion — while "
            f"reported net debt {self.reported_net_debt:.4f} becomes "
            f"**{res['adjusted_net_debt']:.4f}**. Per share: "
            f"{res['reported_price_per_share']:.4f} → **{res['adjusted_price_per_share']:.4f}**.\n\n"
            "Separately, owner earnings reverse the smoothing effect of capitalised R&D — "
            "add back the amortisation already embedded in net income, then expense this "
            "period's actual cash R&D spend and maintenance capex in full:\n\n"
            r"$$OE = NI + D\&A + RD_{amort} - RD_{cash} - Capex_{maint}$$"
            f"\n\n- Reported net income = {self.net_income:.4f}\n"
            f"- **Owner earnings = {res['owner_earnings']:.4f}** "
            f"(Δ = {res['earnings_quality_delta']:+.4f} vs. reported net income)\n"
        )

    def visualize(self, **kwargs: Any) -> "go.Figure":
        """Return a waterfall bridging reported equity value to adjusted equity value."""
        import plotly.graph_objects as go

        res = self.calculate()
        fig = go.Figure(go.Waterfall(
            name="Hidden-debt bridge",
            orientation="v",
            measure=["absolute", "relative", "relative", "relative", "total"],
            x=["Reported equity", "− Capitalised leases", "− Reverse factoring",
               "− Contingent liabilities", "Adjusted equity"],
            y=[self.reported_equity_value, -res["lease_liability"],
               -res["reverse_factoring_exposure"], -res["weighted_contingent_liabilities"], 0],
            connector={"line": {"color": "rgb(140,140,140)"}},
            decreasing={"marker": {"color": "#c0392b"}},
            totals={"marker": {"color": "#2e7d32"}},
        ))
        fig.update_layout(
            title="Reported Equity → Adjusted Equity (Ind AS 116 Hidden-Debt Bridge)",
            yaxis_title="Equity value",
            template="plotly_white",
        )
        return fig

    # ------------------------------------------------------------------ #
    # Benchmarks (consumed by the scoring engine & tests)
    # ------------------------------------------------------------------ #
    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return two closed-form arithmetic identities for the scoring engine."""
        # (a) Lease PV annuity: closed-form ordinary-annuity identity.
        lease_case = cls(
            net_income=0, reported_net_debt=0, reported_equity_value=1000,
            shares_outstanding=100, annual_lease_payment=100, lease_term_years=5,
            lease_discount_rate=0.10,
        )
        lease_pv = lease_case.calculate()["lease_liability"]
        expected_lease_pv = 100.0 * (1.0 - 1.10 ** -5) / 0.10

        # (b) Full hidden-debt sum: independently re-derived arithmetic identity.
        c, r, n = 50.0, 0.08, 3
        expected_lease = c * (1.0 - (1.0 + r) ** -n) / r
        full_case = cls(
            net_income=0, reported_net_debt=500, reported_equity_value=2000,
            shares_outstanding=100, annual_lease_payment=c, lease_term_years=n,
            lease_discount_rate=r, reverse_factoring_exposure=75,
            cl1_amount=200, cl1_probability=0.25, cl2_amount=100, cl2_probability=0.10,
        )
        adjusted_net_debt = full_case.calculate()["adjusted_net_debt"]
        expected_adjusted_net_debt = 500.0 + expected_lease + 75.0 + (200 * 0.25 + 100 * 0.10)

        return [
            Benchmark("Lease PV ordinary-annuity identity", lease_pv, expected_lease_pv,
                      rel_tol=1e-12, source="Ind AS 116 / IFRS 16 §26 — closed-form "
                      "present value of an ordinary annuity."),
            Benchmark("Adjusted net debt arithmetic identity", adjusted_net_debt,
                      expected_adjusted_net_debt, rel_tol=1e-12,
                      source="Direct re-derivation: reported net debt + capitalised "
                      "lease + reverse-factoring exposure + probability-weighted "
                      "contingent liabilities."),
        ]
