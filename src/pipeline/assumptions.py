"""Assumption engines for the analyser: Auto (IB heuristic) and Manual (overrides).

Each model in :mod:`src` requires a specific set of numeric inputs. Some come
straight from the extracted financials; others (WACC, terminal growth,
volatility, correlations, …) must be *assumed* — either by an experienced
practitioner or by the software.

* :class:`AutoAssumer` implements the practitioner heuristic: it fills every
  missing input with an IB / hedge-fund-manager-style default (Damodaran-style
  WACC via CAPM, terminal g ≈ risk-free rate, sector-median betas, etc.).
* :class:`ManualAssumer` reads through :class:`ManualOverrides` — a plain
  dataclass populated by the ipywidgets sliders and text boxes in the notebook
  — and lets the user override any auto-derived value.

Both produce an :class:`AssumptionSet`: a single dict of ``{model_name:
{kwargs}}`` that :mod:`src.pipeline.runner` feeds into each model's constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pdf_extractor import ExtractedFinancials


# --------------------------------------------------------------------------- #
# Assumption container
# --------------------------------------------------------------------------- #
@dataclass
class AssumptionSet:
    """Per-model constructor kwargs, plus the market context they were built in.

    Attributes:
        kwargs_by_model: ``{"Discounted Cash Flow": {"free_cash_flows": [...],
            "discount_rate": 0.09, ...}, ...}``.
        market_context: The risk-free rate, market return and horizon actually
            used, kept for audit trails in the exported report.
        rationale: One-line human-readable justification per assumption, keyed
            by ``(model, param)``.
        unavailable: ``{model_name: reason}`` for models the auto-assumer
            knows cannot produce a trustworthy result from what this filing
            actually disclosed — e.g. Reverse DCF requires the market's own
            current price and share count as inputs (that's the whole point:
            it inverts today's real price into an implied growth rate), so
            fabricating a placeholder price/share-count would produce a
            number that looks like a real answer but describes nothing. A
            model listed here still has an entry in ``kwargs_by_model`` (every
            caller iterating :data:`AVAILABLE_MODELS` still finds a key), but
            callers should check this dict first and skip execution — see
            :meth:`src.pipeline.runner.AnalysisRunner.run`.
        partial: ``{model_name: reason}`` for models that DO run but on
            inputs defaulted in a way that could be mistaken for a genuine
            finding — e.g. the Ind AS 116 Hidden-Debt Normalizer showing a
            $0 adjustment because it found no lease/contingent-liability
            disclosures to work with reads identically to a $0 adjustment
            because it genuinely found nothing to adjust; only the second
            is actually informative. Unlike ``unavailable``, a model listed
            here still produces real results — this only flags that the
            headline number needs a caveat, not that it should be skipped.
    """

    kwargs_by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    market_context: dict[str, float] = field(default_factory=dict)
    rationale: dict[tuple[str, str], str] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    partial: dict[str, str] = field(default_factory=dict)


@dataclass
class ManualOverrides:
    """User-supplied overrides from the notebook widgets.

    Every field is optional; ``None`` means "keep the auto value". Populated by
    ``ManualAssumer.from_widgets`` in the notebook.
    """

    risk_free_rate: float | None = None
    expected_market_return: float | None = None
    beta: float | None = None
    discount_rate: float | None = None           # WACC (DCF)
    terminal_growth: float | None = None
    dividend_growth: float | None = None         # Gordon
    volatility: float | None = None              # Options
    option_maturity: float | None = None
    strike_ratio: float | None = None            # strike / spot
    var_confidence: float | None = None
    var_horizon_days: int | None = None
    monte_carlo_paths: int | None = None
    heston_kappa: float | None = None
    heston_theta: float | None = None
    heston_xi: float | None = None
    heston_rho: float | None = None
    # Ind AS 116 hidden-debt normalizer — the footnote-only figures the
    # auto-assumer can't reliably extract (see AutoAssumer.build).
    annual_lease_payment: float | None = None
    lease_term_years: int | None = None
    reverse_factoring_exposure: float | None = None
    cl1_amount: float | None = None
    cl1_probability: float | None = None
    cl2_amount: float | None = None
    cl2_probability: float | None = None
    # Reverse DCF — total addressable market, almost never a labelled figure.
    total_addressable_market: float | None = None


# --------------------------------------------------------------------------- #
# Auto assumer
# --------------------------------------------------------------------------- #
#: Modern Portfolio Theory's broad-market volatility and company/market
#: correlation — both fixed constants, not derived from anything. Unlike
#: `vol` (the company's own volatility, real when the filing's stock-comp
#: footnote discloses one — see AutoAssumer.build), there's no equivalent
#: cheap real substitute here: this app fetches a single current-day quote
#: (api/quotes.js), never a historical price series for either the market
#: index or the company, so no realized volatility or correlation is
#: actually computable from data the app has access to today. A live
#: historical-range feed could replace _MPT_MARKET_VOL with a real trailing
#: figure; _MPT_CORRELATION has no cheap real substitute even then (it would
#: need the company's OWN historical series too). Named and disclosed in
#: `rationale` rather than left as bare literals in the covariance matrix,
#: so a user can see these are assumed, not derived.
_MPT_MARKET_VOL = 0.18
_MPT_CORRELATION = 0.6


class AutoAssumer:
    """Fill every missing model input with a practitioner-style default.

    Defaults are configurable — the defaults picked here mirror what Damodaran's
    Investment Valuation and typical sell-side desks use as base cases:

    * Risk-free rate: 4.25% (10Y US Treasury, editable).
    * Equity risk premium: 5% → expected market return = rf + ERP.
    * Sector-neutral beta: 1.0 when none is scraped.
    * WACC: CAPM cost of equity (equity 80% / debt 20% blend with 25% tax).
    * Terminal growth: min(rf, 2.5%) — never exceeds the risk-free rate.
    * Volatility: 25% annualised when nothing is scraped.
    * Heston params: literature "typical equity index" values (κ=1.5, θ=0.04,
      ξ=0.3, ρ=−0.6) tied to the annualised volatility guess.
    """

    def __init__(
        self,
        *,
        risk_free_rate: float = 0.0425,
        equity_risk_premium: float = 0.05,
        tax_rate: float = 0.25,
        target_equity_weight: float = 0.80,
        default_beta: float = 1.0,
        default_volatility: float = 0.25,
    ) -> None:
        self.rf = risk_free_rate
        self.erp = equity_risk_premium
        self.tax = tax_rate
        self.we = target_equity_weight
        self.wd = 1 - target_equity_weight
        self.default_beta = default_beta
        self.default_vol = default_volatility

    # ------------------------------------------------------------------ #
    def _wacc(
        self, beta: float, tax: float | None = None,
        we: float | None = None, cost_of_debt: float | None = None,
    ) -> float:
        """Weighted average cost of capital: E/V * ke + D/V * kd * (1-t).

        Args:
            we: Real equity weight (market cap / (market cap + total debt)),
                when derivable from the filing — see :meth:`build`. Falls
                back to the constructor's fixed ``target_equity_weight``
                otherwise.
            cost_of_debt: Real cost of debt (interest expense / total debt),
                when derivable AND plausible — see :meth:`build`. Falls back
                to the flat rf+150bp credit-spread assumption otherwise.
        """
        cost_of_equity = self.rf + beta * self.erp
        kd = cost_of_debt if cost_of_debt is not None else self.rf + 0.015
        equity_weight = we if we is not None else self.we
        debt_weight = 1 - equity_weight
        t = tax if tax is not None else self.tax
        return equity_weight * cost_of_equity + debt_weight * kd * (1 - t)

    def build(
        self, data: ExtractedFinancials, overrides: ManualOverrides | None = None
    ) -> AssumptionSet:
        """Produce a full :class:`AssumptionSet` from extracted data + overrides.

        Args:
            data: Financials scraped from the PDF (some fields may be ``None``).
            overrides: Optional per-parameter overrides from the manual UI.

        Returns:
            A populated :class:`AssumptionSet` covering all twelve models.
        """
        o = overrides or ManualOverrides()
        rf = o.risk_free_rate if o.risk_free_rate is not None else self.rf
        beta = o.beta if o.beta is not None else (
            data.beta if data.beta is not None else self.default_beta)
        erm = o.expected_market_return if o.expected_market_return is not None \
            else rf + self.erp
        # The filing's own effective tax rate (when confidently scraped) is a
        # real, company-specific number sitting right there in the extracted
        # data — previously scraped and then silently discarded in favour of
        # the constructor's generic 25% default even when a genuine value
        # was available (e.g. Tesla's 10-K scrapes tax_rate=0.27 cleanly).
        tax = data.tax_rate if data.tax_rate is not None else self.tax
        # Real capital-structure weight — market cap / (market cap + total
        # debt) — instead of the fixed 80/20 constructor default, when both
        # halves are confidently known. Bounded to [0, 1] by construction
        # (both inputs are positive by the time they get here), so no extra
        # plausibility guard is needed the way cost-of-debt below requires.
        market_cap = (
            data.current_price * data.shares_outstanding
            if data.current_price is not None and data.shares_outstanding is not None
            else None
        )
        we_real = None
        if market_cap is not None and data.total_debt is not None and (market_cap + data.total_debt) > 0:
            we_real = market_cap / (market_cap + data.total_debt)
        # Real cost of debt — interest expense / total debt — instead of a
        # flat rf+150bp spread, when both are known AND the resulting rate
        # is actually plausible. PDFExtractor._scrape_total_debt sums a
        # confirmed Current+Long-Term column breakdown rather than reading
        # just the first number (a real Tesla 10-K's debt-schedule table —
        # "Total debt 1,569 6,584 $8,177 $6,429" — previously understated
        # total debt ~5x this way, producing a nonsensical ~21.5% derived
        # cost of debt), but this guard stays as defence in depth against
        # any other extraction shape that still produces an implausible
        # ratio. The lower bound is intentionally loose (rf-2%, not a
        # strict rf floor): a real borrower's actual cost of debt can sit
        # a little below this model's own generic/live risk-free
        # assumption without that being a sign of a bad extraction —
        # Tesla's own real figures (interest expense / real total debt)
        # land at ~4.15%, marginally under a 4.25% rf default, and a
        # strict "must be >= rf" bound would wrongly discard that genuine
        # value. The upper bound (rf+15%) still rules out anything from
        # investment-grade through deep junk but rejects a clearly wrong
        # ratio like the pre-fix 21.5%.
        cost_of_debt_real = None
        if data.interest_expense is not None and data.total_debt:
            candidate = data.interest_expense / data.total_debt
            if (rf - 0.02) <= candidate <= (rf + 0.15):
                cost_of_debt_real = candidate
        wacc = o.discount_rate if o.discount_rate is not None else self._wacc(
            beta, tax, we=we_real, cost_of_debt=cost_of_debt_real)
        # Terminal growth cannot exceed the risk-free rate (Gordon constraint).
        g_terminal = o.terminal_growth if o.terminal_growth is not None else min(rf, 0.025)
        # A real, filing-disclosed volatility (the "expected [share price]
        # volatility" a 10-K's stock-comp footnote states as an ASC 718
        # Black-Scholes input for valuing employee option grants) is a
        # genuine, company- and period-specific number — prefer it over
        # the flat 25% default the same way the tax_rate fix elsewhere
        # prefers a scraped rate over a generic one. It's not necessarily
        # identical to a market-implied volatility for an arbitrary traded
        # option (it's management's own accounting estimate), which is
        # worth a rationale caveat, but it's real data, not a guess.
        vol = (o.volatility if o.volatility is not None
               else data.disclosed_volatility if data.disclosed_volatility is not None
               else self.default_vol)
        # Fabricated FCF trajectory: revenue × margin × (1+g)^t when actual FCFs missing.
        fcfs = data.free_cash_flows or self._synth_fcfs(data, wacc)
        spot = data.current_price or 100.0  # normalised units when unknown
        strike = o.strike_ratio * spot if o.strike_ratio else spot
        # Gordon Growth requires dividend > 0 — unlike DCF (which happily
        # reports enterprise/equity value with price_per_share left None
        # when share count is unknown), there's no honest partial output
        # here: the model can't represent "this company pays no dividend"
        # at all, only a specific positive number. A fabricated "2% of
        # price" doesn't just approximate an unknown real dividend — for a
        # real company that pays no dividend at all (large classes of
        # growth/tech filers, confirmed on the Tesla fixture: zero
        # dividends for essentially its entire public life), it invents a
        # number that CONTRADICTS a known fact about the company, and even
        # for a genuine payer whose DPS just wasn't extracted cleanly,
        # real yields vary far too widely (roughly 0.5%-6%+ across real
        # dividend payers) for "2% of price" to be a trustworthy stand-in
        # either way. Left None when not disclosed; gated as `unavailable`
        # below rather than run on a fabricated number in either case.
        dividend = data.dividend_per_share
        g_div = o.dividend_growth if o.dividend_growth is not None else 0.03
        shares = data.shares_outstanding or 1_000_000.0
        net_debt = data.net_debt if data.net_debt is not None else 0.0
        # Same margin fallback _synth_fcfs uses, so a synthesised base_revenue
        # is internally consistent with a synthesised FCF path (base_fcf =
        # base_revenue * margin) instead of picking an unrelated placeholder.
        margin = data.operating_margin or 0.15
        base_revenue = data.revenue if data.revenue is not None else fcfs[0] / margin
        reported_equity_value = spot * shares
        lease_discount_rate = rf + 0.015   # same cost-of-debt spread as _wacc

        kwargs: dict[str, dict[str, Any]] = {
            "Discounted Cash Flow": {
                "free_cash_flows": fcfs,
                "discount_rate": wacc,
                "terminal_growth": g_terminal,
                "net_debt": data.net_debt or 0.0,
                "shares_outstanding": data.shares_outstanding or None,
            },
            "Gordon Growth Model": {
                "dividend": dividend,
                "required_return": max(wacc, g_div + 0.005),
                "growth": g_div,
                "dividend_is_forward": False,
            },
            "Modern Portfolio Theory": {
                # Two-asset proxy: the target company + broad market benchmark.
                # `vol` (the company's own volatility — real when disclosed,
                # see the volatility rationale above) drives the first
                # diagonal entry; MKT_VOL and CORRELATION below are NOT
                # derived from anything — see the MPT rationale entry.
                "expected_returns": [rf + beta * self.erp, rf + self.erp],
                "covariance": [[vol**2, _MPT_CORRELATION * vol * _MPT_MARKET_VOL],
                               [_MPT_CORRELATION * vol * _MPT_MARKET_VOL, _MPT_MARKET_VOL**2]],
                "risk_free_rate": rf,
            },
            "Value at Risk / CVaR": {
                "mean": (rf + beta * self.erp) / 252,
                "std": vol / (252**0.5),
                "confidence_level": o.var_confidence if o.var_confidence is not None else 0.95,
                "horizon_days": o.var_horizon_days if o.var_horizon_days is not None else 10,
                # A real dollar VaR needs a real market cap — the old
                # (price or 100.0)*(shares or 1,000,000) fallback quietly
                # substituted a $100M notional for any filing missing
                # either figure (the common case), so the reported "var"
                # was a specific, wrong dollar amount, not a vague
                # approximation — on a real Tesla-scale filing that's off
                # from the true market cap by four orders of magnitude.
                # Unlike Reverse DCF, though, VaR doesn't need a real
                # dollar anchor to be meaningful at all: the model's own
                # default portfolio_value is 1.0 (see ValueAtRiskModel's
                # constructor) and a %-of-portfolio loss is still a real,
                # useful answer with no market-cap dependency — so this
                # falls back to that unit notional instead of a fabricated
                # dollar figure, and the report layer (AnalysisReport.
                # _headline) shows it as a % rather than a "$" amount
                # whenever `partial` below is set.
                "portfolio_value": (
                    data.current_price * data.shares_outstanding
                    if data.current_price is not None and data.shares_outstanding is not None
                    else 1.0
                ),
                "method": "parametric",
            },
            "Capital Asset Pricing Model": {
                "risk_free_rate": rf,
                "expected_market_return": erm,
                "beta": beta,
            },
            "Fama-French 3-Factor": {"_needs_factor_data": True},   # runner handles this
            "Black-Scholes-Merton": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call",
            },
            "Binomial Tree (CRR)": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call", "exercise": "american", "n_steps": 500,
            },
            "Monte Carlo (GBM)": {
                "spot": spot, "strike": strike, "rate": rf, "sigma": vol,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "option_type": "call",
                "n_sims": o.monte_carlo_paths if o.monte_carlo_paths is not None else 100_000,
                "seed": 42,
            },
            "Heston Stochastic Volatility": {
                "spot": spot, "strike": strike, "rate": rf,
                "maturity": o.option_maturity if o.option_maturity is not None else 1.0,
                "v0": vol**2,
                "kappa": o.heston_kappa if o.heston_kappa is not None else 1.5,
                "theta": o.heston_theta if o.heston_theta is not None else vol**2,
                "xi": o.heston_xi if o.heston_xi is not None else 0.3,
                "rho": o.heston_rho if o.heston_rho is not None else -0.6,
                "option_type": "call",
            },
            "Ind AS 116 Hidden-Debt Normalizer": {
                "net_income": data.net_income or 0.0,
                "reported_net_debt": net_debt,
                "reported_equity_value": reported_equity_value,
                "shares_outstanding": shares,
                # Lease payment, reverse-factoring exposure and contingent
                # liabilities are almost never stated as a single clean
                # figure a regex can trust (lease footnotes disclose a
                # multi-year maturity schedule, not one "annual payment";
                # reverse-factoring and contingent-liability amounts are
                # prose, not tabulated) — default to $0 / 0% rather than
                # guess, so an unadjusted company reports truthfully as
                # unadjusted. MANUAL mode overrides these from the filing.
                "annual_lease_payment": o.annual_lease_payment if o.annual_lease_payment is not None else 0.0,
                "lease_term_years": o.lease_term_years if o.lease_term_years is not None else 5,
                "lease_discount_rate": lease_discount_rate,
                "reverse_factoring_exposure": (
                    o.reverse_factoring_exposure if o.reverse_factoring_exposure is not None else 0.0),
                "cl1_amount": o.cl1_amount if o.cl1_amount is not None else 0.0,
                "cl1_probability": o.cl1_probability if o.cl1_probability is not None else 0.0,
                "cl2_amount": o.cl2_amount if o.cl2_amount is not None else 0.0,
                "cl2_probability": o.cl2_probability if o.cl2_probability is not None else 0.0,
                "depreciation_amortization": data.depreciation_amortization or 0.0,
                "rd_capitalized_amortization": 0.0,
                "rd_cash_spend": data.rd_expense or 0.0,
                "maintenance_capex": data.capital_expenditures or 0.0,
            },
            "Reverse DCF / Market-Implied Expectations": {
                "current_price": spot,
                "shares_outstanding": shares,
                "net_debt": net_debt,
                "base_fcf": fcfs[0],
                "base_revenue": base_revenue,
                # No formula-based proxy for TAM is defensible (see the
                # rationale below) — left None rather than a fabricated
                # placeholder when not manually supplied. This model is
                # gated off in `unavailable` below whenever that's the
                # case, so this None is never actually fed into the model;
                # kept None rather than some placeholder anyway as a
                # fail-loud backstop if a future caller ever runs this
                # model's kwargs without checking `unavailable` first.
                "total_addressable_market": o.total_addressable_market,
                "years": 5,
                "discount_rate": wacc,
                "terminal_growth": g_terminal,
            },
        }

        rationale: dict[tuple[str, str], str] = {}
        we_shown = we_real if we_real is not None else self.we
        kd_shown = cost_of_debt_real if cost_of_debt_real is not None else self.rf + 0.015
        rationale[("DCF", "discount_rate")] = (
            f"WACC via CAPM: {we_shown:.0%} equity @ (rf {rf:.2%} + β {beta:.2f}·ERP "
            f"{self.erp:.2%}) + {1 - we_shown:.0%} debt @ {kd_shown:.2%}"
            f"{' (interest expense/total debt)' if cost_of_debt_real is not None else ' (rf+150bp default)'}"
            f"·(1-{tax:.0%} tax{' · scraped from filing' if data.tax_rate is not None else ' · default'})."
            f" Equity weight {'= market cap/(market cap+debt), scraped' if we_real is not None else '= 80/20 default'}."
        )
        rationale[("DCF", "terminal_growth")] = (
            f"Capped at min(rf={rf:.2%}, 2.5%) — Gordon constraint g < r."
        )
        rationale[("CAPM", "beta")] = (
            "Scraped from PDF." if data.beta else f"Sector-neutral default = {beta}."
        )
        rationale[("Options/MPT/VaR", "volatility")] = (
            f"Scraped from the filing's stock-comp footnote ('expected "
            f"volatility' — {vol:.0%}); this is management's own ASC 718 "
            f"Black-Scholes input for valuing employee option grants, real "
            f"and period-specific but not necessarily identical to a "
            f"market-implied volatility for an arbitrary traded option."
            if data.disclosed_volatility is not None and o.volatility is None
            else f"Default = {vol:.0%} (no disclosed volatility found)."
        )
        rationale[("MPT", "market volatility / correlation")] = (
            f"Market volatility ({_MPT_MARKET_VOL:.0%}) and company-market "
            f"correlation ({_MPT_CORRELATION:.2f}) are fixed assumptions, "
            f"not derived from live or filing data — this app only fetches "
            f"a single current-day quote (api/quotes.js), never a "
            f"historical price series for either the market index or the "
            f"company, so no real realized volatility or correlation is "
            f"actually computable today. The company's OWN volatility "
            f"above ({vol:.0%}) IS real when the filing discloses one."
        )
        rationale[("HDEBT", "annual_lease_payment / reverse_factoring / contingent liabilities")] = (
            "No filing reliably states these as one clean, tabulated figure a "
            "regex can trust — defaulted to $0 / 0% (an unadjusted company "
            "reports truthfully as unadjusted) rather than guess a number. "
            "Set the real figures from the filing's lease and contingency "
            "footnotes in MANUAL mode."
        )
        rationale[("HDEBT", "depreciation_amortization / rd_cash_spend / maintenance_capex")] = (
            "Scraped from PDF." if (data.depreciation_amortization and data.rd_expense
                                     and data.capital_expenditures)
            else "Partially or fully defaulted to $0 where the filing's D&A, R&D "
                 "expense or capex line wasn't confidently found."
        )
        rationale[("RDCF", "total_addressable_market")] = (
            "No filing states its own TAM in a form a regex can trust (when "
            "disclosed at all, it's prose in the MD&A, not a labelled "
            "figure), and unlike WACC or terminal growth there's no "
            "formula-based proxy that's meaningfully better than a guess — "
            "real TAM estimates vary 5-50x by segment definition and aren't "
            "derivable from a filing's own numbers. Rather than compute a "
            "10x-revenue placeholder that looks precise but isn't, Reverse "
            "DCF's implied-market-share output requires the company's real "
            "addressable market as a MANUAL input."
        )

        partial: dict[str, str] = {}
        # A $0 hidden-debt adjustment reads identically whether the model
        # found genuinely nothing to adjust, or was simply never given any
        # lease/reverse-factoring/contingent-liability figures to look at —
        # those two situations are not the same claim, and only the first
        # one is actually informative. Flag it here so the report layer can
        # tell "confirmed clean" apart from "not actually assessed" instead
        # of just showing "$0.00" either way.
        if (o.annual_lease_payment is None and o.reverse_factoring_exposure is None
                and o.cl1_amount is None and o.cl2_amount is None):
            partial["Ind AS 116 Hidden-Debt Normalizer"] = (
                "No lease, reverse-factoring or contingent-liability figures "
                "were found or manually supplied — this is not a confirmed "
                "zero-adjustment finding, it's an unassessed one. Set the "
                "real figures from the filing's footnotes in MANUAL mode to "
                "get an actual hidden-debt read."
            )
        if data.current_price is None or data.shares_outstanding is None:
            partial["Value at Risk / CVaR"] = (
                "No share price or share count disclosed — reporting risk as "
                "a % of portfolio value on a $1 unit notional instead of a "
                "fabricated dollar figure. Correct the share price/count "
                "fields for a real dollar-denominated VaR/CVaR."
            )
        if data.current_price is None:
            # Lower stakes than DCF/RDCF/VaR's dollar figures deliberately
            # get a softer note here: these four models are presented in
            # the UI as pricing-MECHANICS demonstrations (their own
            # category/description frames them that way, and spot/strike
            # are exposed as directly user-adjustable sliders defaulting to
            # 100, not as a scraped "real" company fact) — a "$9.59 option
            # price" built on a $100 normalised spot doesn't claim to be a
            # company-specific prediction the way a DCF headline does.
            # Still worth flagging for the same consistency HDEBT/VaR get:
            # a user comparing models in the IB Desk report shouldn't see
            # "OK" for all four with no indication the spot was invented.
            for opt_model in ("Black-Scholes-Merton", "Binomial Tree (CRR)",
                              "Monte Carlo (GBM)", "Heston Stochastic Volatility"):
                partial[opt_model] = (
                    "No share price disclosed — spot defaulted to $100 "
                    "(normalised units). This model illustrates option-"
                    "pricing mechanics rather than pricing a real option on "
                    "this specific company; set the real share price for a "
                    "company-specific figure."
                )
        # Fama-French is flagged unconditionally, not just when a filing is
        # missing data — the app has no historical price series for the
        # company at all (only point-in-time filing extraction), so
        # AnalysisRunner._build_ff_kwargs synthesises an "asset" return
        # series as RF + beta_hint*Mkt-RF + noise, then that exact series
        # gets regressed against Mkt-RF/SMB/HML inside the model. That's
        # circular, not approximate: running the regression against 10
        # years of real Fama-French factor data at beta_hint 1.0, 0.5 and
        # 2.0 recovers β_mkt ≈ the input almost exactly (R² 0.95-0.997)
        # every time, regardless of what the real company's actual factor
        # exposure is — the R² a user sees looks like a rigorous empirical
        # fit but is mechanically guaranteed by construction. No fix exists
        # short of a real historical-returns feed this app doesn't have;
        # flagging it is the honest option available now.
        partial["Fama-French 3-Factor"] = (
            "This model's 'asset' return series is synthesised from the "
            "same beta assumption it's then regressed against — the market-"
            "factor loading and R² will always look strong regardless of "
            "the real company's actual factor exposure, since there's no "
            "real historical price series for this company to regress "
            "against instead. Treat this as an illustration of the "
            "methodology, not an empirical fit to this company."
        )

        unavailable: dict[str, str] = {}
        # Reverse DCF's entire premise is inverting *today's real market
        # price* into an implied growth rate (needs a genuine share price +
        # share count — unlike DCF, which happily reports enterprise/equity
        # value with price_per_share left as None when no share count is
        # known, there's no partial, honest result here if either half of
        # "market cap" is fabricated) and then expressing that implied
        # growth as a share of a real addressable market (needs a genuine
        # TAM — see the rationale above for why no formula-based proxy is
        # defensible here). Any of the three missing is enough to block it:
        # a filing that never states a share price/count, or simply has no
        # trustworthy TAM at all (the common case — virtually none do),
        # isn't a gap the auto-assumer should paper over with fabricated
        # placeholders that produce a confident-looking number describing
        # nothing real. Correct the missing field(s) via MANUAL mode (or the
        # IB desk's per-field override) to run this model.
        rdcf_missing = [n for n, v in (
            ("share price", data.current_price),
            ("share count", data.shares_outstanding),
            ("addressable market (TAM)", o.total_addressable_market),
        ) if v is None]
        if rdcf_missing:
            missing = " or ".join(rdcf_missing)
            unavailable["Reverse DCF / Market-Implied Expectations"] = (
                f"This filing doesn't state a real {missing} — Reverse DCF requires "
                "these as real, market-sourced inputs (that's what it inverts "
                "and expresses a capture of), so it can't produce a "
                "trustworthy result from a fabricated placeholder. Enter the "
                "real figure(s) manually to run this model."
            )
            rationale[("RDCF", "current_price / shares_outstanding / total_addressable_market")] = \
                unavailable["Reverse DCF / Market-Implied Expectations"]

        if data.dividend_per_share is None:
            unavailable["Gordon Growth Model"] = (
                "No dividend per share disclosed. Gordon Growth can't "
                "represent a $0 dividend, and a fabricated placeholder would "
                "either contradict a real fact about a non-dividend-paying "
                "company or guess at a real payer's actual yield — enter "
                "the real dividend per share manually to run this model."
            )
            rationale[("Gordon Growth", "dividend")] = unavailable["Gordon Growth Model"]

        return AssumptionSet(
            kwargs_by_model=kwargs,
            market_context={"risk_free_rate": rf, "expected_market_return": erm,
                            "beta": beta, "volatility": vol, "wacc": wacc,
                            "terminal_growth": g_terminal},
            rationale=rationale,
            unavailable=unavailable,
            partial=partial,
        )

    def _synth_fcfs(self, data: ExtractedFinancials, wacc: float) -> list[float]:
        """Fabricate a 5-year FCF projection when the PDF has none."""
        if data.revenue is None:
            base = 100.0                                     # placeholder units
        else:
            margin = data.operating_margin or 0.15           # 15% FCF margin default
            base = data.revenue * margin
        g = data.revenue_growth or 0.05                      # 5% growth default
        return [base * (1 + g) ** t for t in range(1, 6)]


# --------------------------------------------------------------------------- #
# Manual assumer
# --------------------------------------------------------------------------- #
class ManualAssumer:
    """Manual mode: use auto defaults as a base, then overlay every override.

    Practically ``ManualAssumer`` is a thin wrapper around :class:`AutoAssumer`
    that always applies the user's :class:`ManualOverrides`. Kept as a distinct
    class so the notebook UI (and the exported report) can label which mode was
    used without inspection.
    """

    def __init__(self, auto: AutoAssumer | None = None) -> None:
        self.auto = auto or AutoAssumer()

    def build(
        self, data: ExtractedFinancials, overrides: ManualOverrides
    ) -> AssumptionSet:
        """Return an assumption set with every user override applied."""
        return self.auto.build(data, overrides=overrides)
