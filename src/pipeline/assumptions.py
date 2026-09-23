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


#: Per-industry beta / operating-margin / revenue-growth baselines, keyed by
#: the same category names :meth:`src.pipeline.pdf_extractor.PDFExtractor.
#: _classify_sector` recognises. Sourced from Aswath Damodaran's public NYU
#: Stern industry datasets (levered beta + WACC, operating/net margins, and
#: 5-year historical revenue CAGR — all dated January 2026):
#:   https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/wacc.html
#:   https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/margin.html
#:   https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histgr.html
#:
#: ``beta`` is the LEVERED figure the source publishes — used as-is, since
#: :meth:`AutoAssumer._wacc` already wants a levered equity beta (``cost_of_
#: equity = rf + beta * erp``), not an unlevered one requiring re-levering.
#:
#: ``operating_margin`` is Damodaran's pre-tax operating (EBIT) margin. In
#: THIS codebase it is used as the FCF-margin proxy when a filing discloses
#: no cash-flow statement (:meth:`AutoAssumer._synth_fcfs`: ``base = revenue
#: * margin``) — an approximation the flat 15% default already made; a real
#: sector's EBIT margin is a strictly better proxy for that same purpose,
#: not a different one.
#:
#: ``revenue_growth`` is the 5-YEAR HISTORICAL CAGR, used as a forward
#: near-term growth assumption. This mirrors an existing, deliberate choice
#: elsewhere in this pipeline: PDFExtractor._derive_yoy_metrics already
#: prefers a company's own real historical YoY growth over the flat 5%
#: default for the identical reason (a real number beats a generic one) —
#: this is that same substitution at sector granularity, for filings where
#: no company-specific growth could be read. Some sectors' 5-year figures
#: are volatile (Software (Internet) 29%, Retail (Grocery and Food) -2.6%)
#: because the underlying businesses genuinely are; using the real published
#: figure rather than clamping it to "look normal" is the same posture this
#: pipeline already takes with real negative FCF (see the DCF/RDCF gating
#: in :meth:`AutoAssumer.build`) — an unusual real number is disclosed as
#: real, not silently smoothed into something more comfortable.
#:
#: One sector's raw figure is capped rather than trusted at face value —
#: see :data:`_MAX_SECTOR_GROWTH` just below — because it is not actually a
#: case of "the sector genuinely behaves this way" the way the two above
#: are. Air Transport's 47.79% is a trailing 5-year CAGR measured off a
#: pandemic-crashed base year: a real number, but a measurement artifact of
#: *when* the window starts, not a forward-looking rate any airline is
#: expected to sustain. Damodaran's own methodology explicitly warns
#: against using a raw historical CAGR as a forward growth driver without
#: adjusting for exactly this kind of base-year distortion. Bounding a
#: proxy value for this reason is not the same thing as smoothing real
#: company-disclosed data (which this pipeline still never touches) — it
#: is model hygiene applied to an already-approximate fallback layer.
#:
#: This is a fallback layer only: it fills a gap the code would otherwise
#: fill with the flat generic default (beta=1.0, margin=15%, growth=5%),
#: and only when :meth:`PDFExtractor._classify_sector` confidently
#: identified the filing's industry. It never overrides a real value the
#: filing itself discloses or a value the pipeline already derived from it.
#:
#: Not refreshed at runtime — this pipeline runs client-side in Pyodide/
#: WebAssembly, where a live fetch to a third-party site on every analysis
#: would be a real reliability and staleness-detection risk for no benefit
#: over a periodically-updated constant; the existing risk-free-rate and
#: equity-risk-premium defaults in this same class take the identical
#: compiled-constant posture already.
#:
#: Applied ONLY to a value sourced from this table (never to a company's
#: own real disclosed/derived growth, which is used exactly as stated no
#: matter how extreme) — see the Air Transport note above. Set just above
#: Software (Internet)'s real, legitimately-volatile 29.18%, so it clips
#: nothing but the one confirmed base-year artifact currently in this
#: table; a future addition landing above it deserves the same scrutiny
#: Air Transport got, not a silent pass-through.
_MAX_SECTOR_GROWTH = 0.30

#: Explicit forecast horizon. Growth fades in a straight line from the
#: company's current rate (year 1) to terminal growth (year N), instead of
#: holding a one-year growth rate flat for five years and then dropping it
#: to terminal growth overnight — the cliff that made a company growing 15%
#: look like it would slow to 2.5% in a single year.
_FORECAST_YEARS = 10

#: Trailing years averaged into the FCF base, so one unusual year (a
#: one-off tax deposit or settlement) doesn't set the level of every
#: projected year. Fewer are used when the history is shorter.
_BASE_YEARS = 3

#: Blume (1971) adjustment toward the market beta of 1.0 — the convention
#: behind Bloomberg's default "adjusted beta". Raw regression betas are
#: noisy and mean-revert; applied only to betas this app REGRESSES, never to
#: a disclosed, sector-table or manual one.
_BLUME_WEIGHT = 0.67

#: Minimum gap between the cost of equity and dividend growth before the
#: Gordon model will run. As r approaches g the value 1/(r-g) explodes; the
#: old max(wacc, g+0.5%) floor silently valued a stock at ~200x its dividend.
_GORDON_MIN_SPREAD = 0.01

#: Fewest overlapping months a real Fama-French regression is run on: four
#: parameters need a real sample, and 24 is the same floor the ticker path's
#: beta regression uses (MIN_BETA_OBSERVATIONS in api/fundamentals.js).
FF_MIN_MONTHS = 24

#: Ken French's factors are built from US stocks, so only a US-benchmarked
#: listing's returns can be meaningfully regressed on them.
_FF_US_BENCHMARKS = {"^GSPC"}


def ff_real_returns(data: ExtractedFinancials) -> dict[int, float] | None:
    """The company's real monthly total returns keyed ``YYYYMM``, when a
    Fama-French regression on them is meaningful (a US listing with at least
    :data:`FF_MIN_MONTHS` months); otherwise ``None``. Single source of the
    eligibility rule for both the assumer's disclosure and the runner."""
    if data.return_benchmark not in _FF_US_BENCHMARKS:
        return None
    months = {int(m): float(r) for m, r in (data.monthly_returns or [])}
    return months if len(months) >= FF_MIN_MONTHS else None
SECTOR_BASELINES: dict[str, dict[str, float]] = {
    "Drugs (Pharmaceutical)":            {"beta": 0.98, "operating_margin": 0.3124, "revenue_growth": 0.1845},
    "Healthcare Products":               {"beta": 0.91, "operating_margin": 0.1740, "revenue_growth": 0.1841},
    "Software (System & Application)":   {"beta": 1.28, "operating_margin": 0.4081, "revenue_growth": 0.1956},
    "Software (Internet)":               {"beta": 1.69, "operating_margin": 0.1855, "revenue_growth": 0.2918},
    "Computer Services":                 {"beta": 1.09, "operating_margin": 0.0741, "revenue_growth": 0.2710},
    "Business & Consumer Services":      {"beta": 0.89, "operating_margin": 0.1227, "revenue_growth": 0.0580},
    "Bank (Money Center)":               {"beta": 0.76, "operating_margin": 0.0230, "revenue_growth": 0.0906},
    "Insurance (General)":               {"beta": 0.67, "operating_margin": 0.2307, "revenue_growth": 0.1183},
    "Retail (General)":                  {"beta": 0.81, "operating_margin": 0.0815, "revenue_growth": 0.0992},
    "Retail (Grocery and Food)":         {"beta": 1.12, "operating_margin": 0.0255, "revenue_growth": -0.0263},
    "Auto & Truck":                      {"beta": 1.46, "operating_margin": 0.0316, "revenue_growth": 0.0864},
    "Steel":                             {"beta": 1.06, "operating_margin": 0.0450, "revenue_growth": 0.1137},
    "Metals & Mining":                   {"beta": 1.04, "operating_margin": 0.2385, "revenue_growth": 0.0867},
    "Real Estate (General/Diversified)": {"beta": 0.81, "operating_margin": 0.2361, "revenue_growth": 0.0960},
    "Telecom Services":                  {"beta": 0.63, "operating_margin": 0.2105, "revenue_growth": 0.1357},
    "Telecom (Wireless)":                {"beta": 0.54, "operating_margin": 0.2198, "revenue_growth": 0.0370},
    "Power":                             {"beta": 0.48, "operating_margin": 0.2190, "revenue_growth": 0.0674},
    "Oil/Gas (Integrated)":              {"beta": 0.30, "operating_margin": 0.1156, "revenue_growth": 0.0462},
    "Oil/Gas Production and Exploration":{"beta": 0.72, "operating_margin": 0.2632, "revenue_growth": 0.1761},
    "Chemical (Specialty)":              {"beta": 0.97, "operating_margin": 0.1285, "revenue_growth": 0.0804},
    "Food Processing":                   {"beta": 0.61, "operating_margin": 0.1100, "revenue_growth": 0.0716},
    "Building Materials":                {"beta": 1.11, "operating_margin": 0.1328, "revenue_growth": 0.0414},
    "Engineering/Construction":          {"beta": 1.21, "operating_margin": 0.0704, "revenue_growth": 0.1170},
    "Machinery":                         {"beta": 0.96, "operating_margin": 0.1678, "revenue_growth": 0.1037},
    "Semiconductor":                     {"beta": 1.52, "operating_margin": 0.4037, "revenue_growth": 0.1118},
    "Apparel":                           {"beta": 0.94, "operating_margin": 0.0911, "revenue_growth": 0.0810},
    "Hotel/Gaming":                      {"beta": 1.08, "operating_margin": 0.1939, "revenue_growth": 0.2182},
    "Air Transport":                     {"beta": 1.19, "operating_margin": 0.0532, "revenue_growth": 0.4779},
    "Transportation":                    {"beta": 0.86, "operating_margin": 0.0757, "revenue_growth": 0.0912},
    "Publishing & Newspapers":           {"beta": 0.56, "operating_margin": 0.0998, "revenue_growth": 0.0004},
    "Shipbuilding & Marine":             {"beta": 0.75, "operating_margin": 0.1260, "revenue_growth": -0.0012},
}


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
        terminal_growth_cap: float = 0.025,
    ) -> None:
        """``terminal_growth_cap`` is the market's long-run nominal growth
        ceiling (the country selector supplies it — see ``COUNTRIES`` in
        terminal.js). Terminal growth is min(risk-free rate, this cap)."""
        self.terminal_growth_cap = terminal_growth_cap
        self.rf = risk_free_rate
        self.erp = equity_risk_premium
        self.tax = tax_rate
        self.we = target_equity_weight
        self.wd = 1 - target_equity_weight
        self.default_beta = default_beta
        self.default_vol = default_volatility

    # ------------------------------------------------------------------ #
    @staticmethod
    def _sector_baseline(data: ExtractedFinancials) -> dict[str, float] | None:
        """SECTOR_BASELINES entry for ``data.sector``, or ``None``.

        The single lookup point — this used to be repeated inline at three
        separate call sites (build, _project_fcfs, _synth_fcfs), which meant
        any future change to how a sector maps to its baseline (or a guard
        added around it) had to be kept in sync by hand across all three.
        """
        return SECTOR_BASELINES.get(data.sector) if data.sector else None

    @staticmethod
    def _beta_source(data: ExtractedFinancials) -> str:
        """How to describe a beta that arrived with the extraction.

        Beta needs its own wording because it is the one input that is NOT a
        disclosure. There is no XBRL concept for it — searching a filer's
        entire us-gaap taxonomy for "beta" returns nothing — because beta
        describes how a stock's returns co-move with its market, which no
        company reports about itself. The ticker path therefore COMPUTES it by
        regression from price history; only the PDF path can read one off a
        document (filings occasionally quote a beta in a valuation note).

        Describing a regressed beta as "from the company's SEC XBRL filing
        data" would be exactly the authoritative-sounding false citation
        :meth:`_disclosed_source` exists to prevent, just aimed at a different
        field — so the general helper deliberately is not reused here.
        """
        backends = getattr(data, "backends_used", None) or []
        joined = " ".join(str(b).lower() for b in backends)
        if "sec-edgar" in joined or "market-data" in joined:
            return ("Computed by OLS regression of five years of monthly returns "
                    "against the listing's market index — beta is a market "
                    "statistic, not a figure any company discloses.")
        return "Scraped from PDF."

    @staticmethod
    def _disclosed_source(data: ExtractedFinancials) -> str:
        """How to describe a figure that came from the company itself.

        The IB desk has two intake paths: an uploaded PDF, and a ticker load
        that pulls SEC XBRL company facts (``api/fundamentals.js``). A
        rationale reading "Scraped from PDF." next to an XBRL fact is a false
        citation — the same class of bug as the manual-override
        misattribution guarded against in :meth:`build`, and the reason that
        guard exists: a specific, authoritative-sounding *wrong* provenance is
        worse than no provenance at all, because a reader acts on it.

        Keyed off ``backends_used``, which each intake path stamps on the
        extraction it produces.
        """
        backends = getattr(data, "backends_used", None) or []
        if any("sec-edgar" in str(b).lower() for b in backends):
            return "From the company's SEC XBRL filing data."
        return "Scraped from PDF."

    @staticmethod
    def _sector_growth(sector_baseline: dict[str, float] | None) -> float | None:
        """The sector's revenue-growth baseline, capped — see
        :data:`_MAX_SECTOR_GROWTH` for why this is the one field in
        SECTOR_BASELINES that gets bounded rather than used verbatim.
        """
        if sector_baseline is None:
            return None
        return min(sector_baseline["revenue_growth"], _MAX_SECTOR_GROWTH)

    def _wacc(
        self, beta: float, tax: float | None = None,
        we: float | None = None, cost_of_debt: float | None = None,
        rf: float | None = None, erp: float | None = None,
    ) -> float:
        """Weighted average cost of capital: E/V * ke + D/V * kd * (1-t).

        Args:
            rf, erp: The risk-free rate and equity risk premium actually in
                force for this build — a MANUAL override of either must reach
                the discount rate. Reading ``self.rf``/``self.erp`` here
                instead meant an overridden 7% risk-free rate left WACC
                unchanged while the rationale printed "rf 7.00%" beside it.
                Default to the constructor's values for direct callers.
            we: Real equity weight (market cap / (market cap + total debt)),
                when derivable from the filing — see :meth:`build`. Falls
                back to the constructor's fixed ``target_equity_weight``
                otherwise.
            cost_of_debt: Real cost of debt (interest expense / total debt),
                when derivable AND plausible — see :meth:`build`. Falls back
                to the flat rf+150bp credit-spread assumption otherwise.
        """
        rf = self.rf if rf is None else rf
        erp = self.erp if erp is None else erp
        cost_of_equity = rf + beta * erp
        kd = cost_of_debt if cost_of_debt is not None else rf + 0.015
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
        sector_baseline = self._sector_baseline(data)
        # A real, filing-disclosed beta always wins; failing that, a real
        # sector-median beta (Damodaran, see SECTOR_BASELINES) beats the flat
        # 1.0 "sector-neutral" default this class's docstring already
        # described as an aspiration — betas genuinely spread from 0.30
        # (Oil/Gas Integrated) to 1.69 (Software Internet), so 1.0 was never
        # a neutral choice for most real companies, just an uninformed one.
        beta = o.beta if o.beta is not None else (
            data.beta if data.beta is not None else (
                sector_baseline["beta"] if sector_baseline else self.default_beta))
        # A beta the ticker path REGRESSED is Blume-adjusted; a disclosed,
        # sector-table or manual one is used exactly as given.
        beta_raw = beta
        beta_regressed = (o.beta is None and data.beta is not None
                          and "regression" in self._beta_source(data).lower())
        if beta_regressed:
            beta = _BLUME_WEIGHT * beta_raw + (1 - _BLUME_WEIGHT)
        erm = o.expected_market_return if o.expected_market_return is not None \
            else rf + self.erp
        # The premium every cost-of-equity figure below uses. An expected-
        # market-return override implies its own premium (erm - rf); CAPM
        # already priced off erm while WACC kept self.erp, so the same build
        # quoted two different costs of equity for one company.
        erp = erm - rf
        cost_of_equity = rf + beta * erp
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
        # Lease liabilities are debt: a lessee owes those payments as surely
        # as it owes a bond coupon. The ticker path only reports a lease
        # field when the debt tag it used does NOT already include it.
        lease_debt = ((data.finance_lease_liabilities or 0.0)
                      + (data.operating_lease_liabilities or 0.0))
        we_real = None
        if market_cap is not None and data.total_debt is not None \
                and (market_cap + data.total_debt + lease_debt) > 0:
            we_real = market_cap / (market_cap + data.total_debt + lease_debt)
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
            beta, tax, we=we_real, cost_of_debt=cost_of_debt_real, rf=rf, erp=erp)
        # Terminal growth cannot exceed the risk-free rate (Gordon constraint).
        # Terminal growth cannot exceed the risk-free rate of the currency
        # (Gordon constraint), nor the market's long-run nominal growth. The
        # cap used to be a flat 2.5% everywhere, which for a rupee valuation
        # (rf 6.9%) sat below Indian inflation.
        g_terminal = (o.terminal_growth if o.terminal_growth is not None
                      else min(rf, self.terminal_growth_cap))
        # A real, filing-disclosed volatility (the "expected [share price]
        # volatility" a 10-K's stock-comp footnote states as an ASC 718
        # Black-Scholes input for valuing employee option grants) is a
        # genuine, company- and period-specific number — prefer it over
        # the flat 25% default the same way the tax_rate fix elsewhere
        # prefers a scraped rate over a generic one. It's not necessarily
        # identical to a market-implied volatility for an arbitrary traded
        # option (it's management's own accounting estimate), which is
        # worth a rationale caveat, but it's real data, not a guess.
        # After a disclosed figure comes the stock's realised volatility,
        # measured on the ticker path from the same five years of monthly
        # returns beta is regressed on. The flat 25% it replaces was off by
        # 2x in both directions on real names (TSLA 58%, KO 16%).
        vol = (o.volatility if o.volatility is not None
               else data.disclosed_volatility if data.disclosed_volatility is not None
               else data.realized_volatility if data.realized_volatility is not None
               else self.default_vol)
        # MPT's market leg: measured against the same benchmark when the
        # ticker path supplied it, the fixed constants otherwise.
        mkt_vol = (data.market_volatility if data.market_volatility is not None
                   else _MPT_MARKET_VOL)
        corr = (data.market_correlation if data.market_correlation is not None
                else _MPT_CORRELATION)
        # Fabricated FCF trajectory: revenue × margin × (1+g)^t when actual FCFs missing.
        # A filing that discloses ONE free-cash-flow figure (a quarterly
        # results announcement states the period's FCF in prose, not a
        # multi-year row) gives a base, not a trajectory — projecting from it
        # is what that number supports. Handing the DCF a one-element list
        # instead would silently reduce it to a single explicit year plus
        # terminal value, which is a different and much cruder model than the
        # five-year path every other input here assumes.
        #
        # A multi-year series is REPORTED history, and history is not a
        # forecast. It used to be handed to the DCF as FCF_1..FCF_N, so a
        # ticker load discounted FY2020-FY2025 as if they were the next six
        # years and closed the perpetuity on a past year: AAPL came out at
        # $84 against a $337 price, MSFT $121 against $497. The most recent
        # year is the base every projection grows from, exactly as a
        # single-figure filing already was.
        lease_rate = rf + 0.015       # same credit spread as _wacc's default kd
        base_fcf, base_note = self._normalised_base(data, tax, lease_rate)
        fcfs = self._project_fcfs(base_fcf, data, g_terminal)
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
        # Same margin fallback _synth_fcfs uses (including its sector-baseline
        # preference — see SECTOR_BASELINES), so a synthesised base_revenue is
        # internally consistent with a synthesised FCF path (base_fcf =
        # base_revenue * margin) instead of picking an unrelated placeholder.
        # `or`, not `is not None`, matching _synth_fcfs's own check — kept
        # identical rather than tightened here as an unrelated side effect.
        margin = data.operating_margin or (
            sector_baseline["operating_margin"] if sector_baseline else None) or 0.15
        # abs() on the fallback: a cash-burning filing's real FCF is negative,
        # and revenue derived from it would come out negative too — a company
        # with negative sales, which is not a thing. The models this feeds are
        # gated off for that case below; this only keeps the value sane for
        # the ones that still run.
        base_revenue = (data.revenue if data.revenue is not None
                        else abs(base_fcf) / margin)
        reported_equity_value = spot * shares
        lease_discount_rate = rf + 0.015   # same cost-of-debt spread as _wacc

        kwargs: dict[str, dict[str, Any]] = {
            "Discounted Cash Flow": {
                "free_cash_flows": fcfs,
                "discount_rate": wacc,
                "terminal_growth": g_terminal,
                "net_debt": (data.net_debt or 0.0) + lease_debt,
                "shares_outstanding": data.shares_outstanding or None,
            },
            "Gordon Growth Model": {
                "dividend": dividend,
                # Dividends belong to shareholders, so they are discounted
                # at the cost of equity — not WACC, which blends in the
                # cheaper, tax-shielded cost of debt.
                "required_return": cost_of_equity,
                "growth": g_div,
                "dividend_is_forward": False,
            },
            "Modern Portfolio Theory": {
                # Two-asset proxy: the target company + broad market benchmark.
                # `vol` (the company's own volatility — real when disclosed,
                # see the volatility rationale above) drives the first
                # diagonal entry; MKT_VOL and CORRELATION below are NOT
                # derived from anything — see the MPT rationale entry.
                "expected_returns": [rf + beta * erp, rf + erp],
                "covariance": [[vol**2, corr * vol * mkt_vol],
                               [corr * vol * mkt_vol, mkt_vol**2]],
                "risk_free_rate": rf,
            },
            "Value at Risk / CVaR": {
                "mean": (rf + beta * erp) / 252,
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
                "net_debt": net_debt + lease_debt,
                # The TRAILING figure the model grows at its solved rate —
                # not fcfs[0], which is already one year of assumed growth
                # (and, before the history fix above, was the OLDEST year).
                "base_fcf": base_fcf,
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
                # The forward DCF's own horizon and growth shape: the
                # reverse solve then answers "what year-1 growth, fading
                # like the DCF's, does today's price require?" — the same
                # question the forward DCF answers in reverse.
                "years": _FORECAST_YEARS,
                "growth_profile": "fade",
                "discount_rate": wacc,
                "terminal_growth": g_terminal,
            },
        }

        rationale: dict[tuple[str, str], str] = {}
        we_shown = we_real if we_real is not None else self.we
        kd_shown = cost_of_debt_real if cost_of_debt_real is not None else rf + 0.015
        rationale[("DCF", "discount_rate")] = (
            f"WACC via CAPM: {we_shown:.0%} equity @ (rf {rf:.2%} + β {beta:.2f}·ERP "
            f"{erp:.2%}) + {1 - we_shown:.0%} debt @ {kd_shown:.2%}"
            f"{' (interest expense/total debt)' if cost_of_debt_real is not None else ' (rf+150bp default)'}"
            f"·(1-{tax:.0%} tax{' · scraped from filing' if data.tax_rate is not None else ' · default'})."
            f" Equity weight {'= market cap/(market cap+debt), scraped' if we_real is not None else '= 80/20 default'}."
        )
        rationale[("DCF", "terminal_growth")] = (
            f"Manually overridden = {g_terminal:.2%}." if o.terminal_growth is not None
            else f"min(risk-free {rf:.2%}, market long-run nominal growth "
                 f"{self.terminal_growth_cap:.2%}) = {g_terminal:.2%} — a perpetuity "
                 f"can't outgrow the currency's risk-free rate or its economy."
        )
        if lease_debt:
            rationale[("DCF", "net_debt")] = (
                (f"Reported net debt plus {lease_debt:,.0f} of IFRS 16 lease "
                 f"liabilities. IFRS 16 puts every lease on the balance sheet and "
                 f"reports the principal repayments under FINANCING cash flows, so "
                 f"FCF (operating cash flow − capex) never deducted them — counting "
                 f"the liability as debt is the matching treatment, not a double count."
                 if data.accounting_standard == "ifrs-full" else
                 f"Reported net debt plus {lease_debt:,.0f} of lease liabilities "
                 f"(finance {data.finance_lease_liabilities or 0:,.0f}, operating "
                 f"{data.operating_lease_liabilities or 0:,.0f}) — lease payments are "
                 f"owed like debt; the interest inside operating-lease payments is "
                 f"added back to FCF so it isn't counted twice.")
            )
        capped_note = (" (capped — see SECTOR_BASELINES)"
                       if sector_baseline and sector_baseline["revenue_growth"] > _MAX_SECTOR_GROWTH
                       else "")
        growth_src = ("filing" if data.revenue_growth else
                      f"{data.sector} sector{capped_note}" if sector_baseline else "generic 5%")
        g_used = data.revenue_growth or self._sector_growth(sector_baseline) or 0.05
        fade = (f"projected {_FORECAST_YEARS} years, growth fading linearly from "
                f"{g_used:.2%} ({growth_src}) to the {g_terminal:.2%} terminal rate")
        if data.free_cash_flows:
            src = self._disclosed_source(data)
            rationale[("DCF", "free_cash_flows")] = (
                f"Base free cash flow to the firm {base_fcf:,.0f} — {base_note} "
                f"({src[:1].lower()}{src[1:].rstrip('.')}; reported history is the "
                f"base, never used as the forecast itself); {fade}."
            )
        elif data.revenue is None:
            rationale[("DCF", "free_cash_flows")] = (
                "No FCF or revenue disclosed — synthesised from a placeholder "
                f"base; {fade}."
            )
        else:
            margin_src = ("filing" if data.operating_margin else
                          f"{data.sector} sector" if sector_baseline else "generic 15%")
            rationale[("DCF", "free_cash_flows")] = (
                f"No FCF disclosed — synthesised as revenue × operating margin "
                f"({margin_src} default); {fade}."
            )
        # Checks the TRUE source beta was actually resolved from, in the same
        # priority order build() itself used (o.beta > data.beta >
        # sector_baseline > default) — not just data.beta and sector_baseline,
        # which the earlier version of this line did. That version, given a
        # manual override on a filing that happened to classify into a
        # sector, printed "Drugs (Pharmaceutical) sector median (Damodaran,
        # Jan 2026) = 1.5" for a beta the USER typed in — a specific,
        # authoritative-sounding false citation for their own input.
        if o.beta is not None:
            rationale[("CAPM", "beta")] = f"Manually overridden = {beta}."
        elif data.beta is not None and beta_regressed:
            rationale[("CAPM", "beta")] = (
                f"{self._beta_source(data)} Raw {beta_raw:.2f}, Blume-adjusted "
                f"{_BLUME_WEIGHT}×{beta_raw:.2f} + {1 - _BLUME_WEIGHT:.2f} = {beta:.2f} "
                f"(Bloomberg's adjusted-beta convention: regression betas mean-revert "
                f"toward 1)."
            )
        elif data.beta is not None:
            rationale[("CAPM", "beta")] = self._beta_source(data)
        elif sector_baseline:
            rationale[("CAPM", "beta")] = (
                f"{data.sector} sector median (Damodaran, Jan 2026) = {beta}."
            )
        else:
            rationale[("CAPM", "beta")] = f"Sector-neutral default = {beta}."
        rationale[("Options/MPT/VaR", "volatility")] = (
            f"Scraped from the filing's stock-comp footnote ('expected "
            f"volatility' — {vol:.0%}); this is management's own ASC 718 "
            f"Black-Scholes input for valuing employee option grants, real "
            f"and period-specific but not necessarily identical to a "
            f"market-implied volatility for an arbitrary traded option."
            if data.disclosed_volatility is not None and o.volatility is None
            else f"Manually overridden = {vol:.0%}." if o.volatility is not None
            else f"Realised volatility {vol:.0%}: annualised standard deviation of "
                 f"{data.return_observations or 'the'} monthly returns (the same "
                 f"series beta is regressed on) — measured, not disclosed."
            if data.realized_volatility is not None
            else f"Default = {vol:.0%} (no disclosed or measured volatility available)."
        )
        if data.market_volatility is not None and data.market_correlation is not None:
            rationale[("MPT", "market volatility / correlation")] = (
                f"Measured from {data.return_observations or 'the'} monthly returns: "
                f"benchmark volatility {mkt_vol:.0%}, company-benchmark "
                f"correlation {corr:.2f} — the same aligned series beta is "
                f"regressed on."
            )
        else:
            rationale[("MPT", "market volatility / correlation")] = (
                f"Market volatility ({mkt_vol:.0%}) and company-market "
                f"correlation ({corr:.2f}) are fixed assumptions, not derived "
                f"from this company's data: no return "
                f"history was available for this company (a PDF upload, or a "
                f"ticker whose price history couldn't be fetched). A ticker "
                f"load measures both from five years of monthly returns."
            )
        rationale[("HDEBT", "annual_lease_payment / reverse_factoring / contingent liabilities")] = (
            "No filing reliably states these as one clean, tabulated figure a "
            "regex can trust — defaulted to $0 / 0% (an unadjusted company "
            "reports truthfully as unadjusted) rather than guess a number. "
            "Set the real figures from the filing's lease and contingency "
            "footnotes in MANUAL mode."
        )
        rationale[("HDEBT", "depreciation_amortization / rd_cash_spend / maintenance_capex")] = (
            self._disclosed_source(data)
            if (data.depreciation_amortization and data.rd_expense
                and data.capital_expenditures)
            else "Partially or fully defaulted to $0 where the filing's D&A, R&D "
                 "expense or capex line wasn't confidently found."
        )
        rationale[("RDCF", "implied growth")] = (
            f"Solves for the year-1 FCF growth that, fading linearly to the "
            f"{g_terminal:.2%} terminal rate over {_FORECAST_YEARS} years like the "
            f"forward DCF, justifies today's price; the headline is that path's "
            f"average annual growth. Base FCF is the DCF's own normalised base."
        )
        rationale[("RDCF", "total_addressable_market")] = (
            "No filing states its own TAM in a form a regex can trust (when "
            "disclosed at all, it's prose in the MD&A, not a labelled "
            "figure), and unlike WACC or terminal growth there's no "
            "formula-based proxy that's meaningfully better than a guess — "
            "real TAM estimates vary 5-50x by segment definition and aren't "
            "derivable from a filing's own numbers. Rather than compute a "
            "10x-revenue placeholder that looks precise but isn't, the "
            "implied-market-share read is left blank until the company's real "
            "addressable market is entered in MANUAL mode. The implied growth "
            "rate — the model's actual answer — does not need it."
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
        ff_months = ff_real_returns(data)
        if ff_months is not None:
            rationale[("FF3", "asset returns")] = (
                f"Real regression: the company's own monthly total returns "
                f"({len(ff_months)} completed months, dividend-adjusted) against "
                f"Ken French's US Mkt-RF, SMB and HML factors, over the months "
                f"both series cover."
            )
        else:
            why = ("its returns are measured against a non-US index and Ken "
                   "French's factors are US-market factors, so regressing on "
                   "them wouldn't describe this company"
                   if data.return_benchmark else
                   "there's no historical return series for this company (a "
                   "PDF upload, or a ticker whose price history couldn't be "
                   "fetched)")
            partial["Fama-French 3-Factor"] = (
                "This model's 'asset' return series is synthesised from the "
                "same beta assumption it's then regressed against — the market-"
                "factor loading and R² will always look strong regardless of "
                f"the real company's actual factor exposure, because {why}. "
                "Treat this as an illustration of the methodology, not an "
                "empirical fit to this company. A US ticker load runs it on "
                "the company's real returns."
            )

        unavailable: dict[str, str] = {}
        # Reverse DCF's entire premise is inverting *today's real market
        # price* into an implied growth rate, so it needs a genuine share
        # price + share count — unlike DCF, which happily reports enterprise/
        # equity value with price_per_share left as None when no share count
        # is known, there's no partial, honest result here if either half of
        # "market cap" is fabricated. Either missing blocks it; correct them
        # via MANUAL mode (or the IB desk's per-field override).
        #
        # TAM is deliberately NOT in this list. It feeds one secondary output
        # (implied share of the market), which the model leaves None without
        # it. Requiring it here blocked the implied growth rate too, and
        # since virtually no filing states a TAM, the model never ran on an
        # automatic analysis at all.
        rdcf_missing = [n for n, v in (
            ("share price", data.current_price),
            ("share count", data.shares_outstanding),
        ) if v is None]
        if rdcf_missing:
            missing = " or ".join(rdcf_missing)
            unavailable["Reverse DCF / Market-Implied Expectations"] = (
                f"This filing doesn't state a real {missing} — Reverse DCF requires "
                "these as real, market-sourced inputs (the market "
                "capitalisation is what it inverts), so it can't produce a "
                "trustworthy result from a fabricated placeholder. Enter the "
                "real figure(s) manually to run this model."
            )
            rationale[("RDCF", "current_price / shares_outstanding")] = \
                unavailable["Reverse DCF / Market-Implied Expectations"]

        if data.dividend_per_share is not None and cost_of_equity - g_div < _GORDON_MIN_SPREAD:
            unavailable["Gordon Growth Model"] = (
                f"Cost of equity {cost_of_equity:.2%} is within "
                f"{_GORDON_MIN_SPREAD:.0%} of dividend growth {g_div:.2%}; the "
                f"model's 1/(r−g) term explodes as they converge, so any value "
                f"it printed would be an artefact of the gap, not of the "
                f"dividend. Set a lower dividend growth rate in MANUAL mode."
            )
            rationale[("Gordon Growth", "required_return")] = unavailable["Gordon Growth Model"]
        elif data.dividend_per_share is not None:
            rationale[("Gordon Growth", "required_return")] = (
                f"Cost of equity {cost_of_equity:.2%} (rf + β·ERP) — dividends go "
                f"to shareholders, so they're discounted at the equity holders' "
                f"required return, not WACC."
            )
        if data.dividend_per_share is None:
            unavailable["Gordon Growth Model"] = (
                "No dividend per share disclosed. Gordon Growth can't "
                "represent a $0 dividend, and a fabricated placeholder would "
                "either contradict a real fact about a non-dividend-paying "
                "company or guess at a real payer's actual yield — enter "
                "the real dividend per share manually to run this model."
            )
            rationale[("Gordon Growth", "dividend")] = unavailable["Gordon Growth Model"]

        # A cash-burning company's real, disclosed negative free cash flow is
        # data worth keeping — the extractor no longer discards it, because
        # discarding it meant synthesising a POSITIVE figure that contradicts
        # the filing. But neither DCF nor Reverse DCF can honestly value a
        # company on it, and for the same reason: both close with a Gordon
        # perpetuity, TV = FCF_N·(1+g)/(r−g). Feed that a negative FCF_N and
        # it returns a negative terminal value — arithmetic for "this company
        # burns cash at a growing rate, forever", which no real company does;
        # it either turns cash-positive or stops existing. The number computes
        # cleanly and means nothing, which is the failure mode this pipeline
        # exists to refuse.
        #
        # Reverse DCF already refuses it outright (ReverseDCFModel calls
        # _require_positive on base_fcf), so without this gate a filing with a
        # price, a share count and a TAM would newly surface a raw
        # ValidationError instead of an explained one. This states the same
        # constraint at the assumption layer, for both models, with a reason.
        terminal_fcf = fcfs[-1] if fcfs else 0.0
        if terminal_fcf <= 0 or base_fcf <= 0:
            unavailable["Discounted Cash Flow"] = (
                "The free cash flow this filing discloses is negative, and a "
                "DCF closes with a perpetuity on the final year's cash flow — "
                "projecting a cash burn forever produces a negative terminal "
                "value, which is arithmetic rather than a valuation. This is "
                "a real disclosed figure, not a missing one: value a "
                "cash-burning company on a forecast that reaches breakeven, "
                "by entering the projected free cash flows manually."
            )
            rationale[("DCF", "free_cash_flows")] = unavailable["Discounted Cash Flow"]
        if base_fcf <= 0:
            reason = (
                "Reverse DCF solves for the growth rate that justifies the "
                "market price by projecting a trailing free cash flow forward "
                "— a negative base can't be grown into the positive enterprise "
                "value a share price implies, at any growth rate. Enter a "
                "normalised or forecast base free cash flow manually to run it."
            )
            unavailable.setdefault("Reverse DCF / Market-Implied Expectations", reason)
            rationale.setdefault(("RDCF", "base_fcf"), reason)

        return AssumptionSet(
            kwargs_by_model=kwargs,
            market_context={"risk_free_rate": rf, "expected_market_return": erm,
                            "beta": beta, "beta_raw": beta_raw,
                            "cost_of_equity": cost_of_equity,
                            "lease_debt": lease_debt, "volatility": vol, "wacc": wacc,
                            "terminal_growth": g_terminal},
            rationale=rationale,
            unavailable=unavailable,
            partial=partial,
        )

    def _project_fcfs(self, base: float, data: ExtractedFinancials,
                      g_terminal: float = 0.025) -> list[float]:
        """Grow a trailing base FCF into the explicit forecast path.

        Growth starts at the company's current rate and fades in a straight
        line to ``g_terminal`` over :data:`_FORECAST_YEARS` years, so the
        explicit period hands over to the perpetuity smoothly instead of
        falling off a cliff at year five.

        The starting rate prefers, in order: the filing's own disclosed/
        derived rate, then its sector's real 5-year historical CAGR
        (SECTOR_BASELINES — see :meth:`_sector_growth` for the one figure
        that's capped), then the flat 5% default.
        """
        sector_baseline = self._sector_baseline(data)
        g0 = data.revenue_growth or self._sector_growth(sector_baseline) or 0.05
        n = _FORECAST_YEARS
        path, value = [], base
        for t in range(n):
            value *= 1 + g0 + (g_terminal - g0) * t / (n - 1)
            path.append(value)
        return path

    def _synth_base(self, data: ExtractedFinancials) -> float:
        """Revenue × margin stand-in for a trailing FCF the filing lacks."""
        if data.revenue is None:
            return 100.0                                     # placeholder units
        sector_baseline = self._sector_baseline(data)
        # Same margin fallback build() uses for base_revenue's own
        # abs(base_fcf)/margin path — see SECTOR_BASELINES.
        margin = data.operating_margin or (
            sector_baseline["operating_margin"] if sector_baseline else None) or 0.15
        return data.revenue * margin

    @staticmethod
    def _latest_reported_fcf(data: ExtractedFinancials) -> float | None:
        """The most recent year of reported FCF history, or ``None``.

        Which end is "most recent" comes from ``fcf_history_order`` — see
        :attr:`ExtractedFinancials.fcf_history_order`.
        """
        history = data.free_cash_flows or []
        if not history:
            return None
        return history[-1] if data.fcf_history_order == "oldest_first" else history[0]

    def _trailing_fcf(self, data: ExtractedFinancials) -> float:
        """The latest reported FCF, else the revenue × margin stand-in."""
        latest = self._latest_reported_fcf(data)
        return latest if latest is not None else self._synth_base(data)

    @staticmethod
    def _adds_back_interest(data: ExtractedFinancials) -> bool:
        """Whether reported FCF is after interest paid.

        US GAAP puts interest paid in operating cash flow (ASC 230), so
        OCF − capex is after interest and must have it added back to be a
        cash flow to ALL capital providers — the flow WACC discounts. IFRS /
        Ind AS let a company classify it as financing instead, in which case
        it was never deducted and adding it back would double-count. Unknown
        classification: follow the currency's usual convention.
        """
        cls = data.interest_paid_classification
        if cls is not None:
            return cls == "operating"
        # An IFRS filer that states nothing either way (explicitly or through
        # its lease cash-flow tags — see api/fundamentals.js) could be using
        # either presentation. Not adding back is the error that can't
        # double-count; it understates FCF by at most interest × (1 − t).
        if data.accounting_standard == "ifrs-full":
            return False
        return data.currency == "USD"

    def _normalised_base(
        self, data: ExtractedFinancials, tax: float, lease_rate: float,
    ) -> tuple[float, str]:
        """Trailing free cash flow to the firm, normalised, plus how it was built.

        For each of the latest :data:`_BASE_YEARS` reported years:

            FCFF = FCF + interest × (1 − t)   [when FCF is after interest]
                       − stock-based compensation
                       + operating-lease liability × lease rate × (1 − t)

        then averaged. Interest is added back because the DCF discounts at
        WACC and then subtracts net debt — leaving it deducted charged the
        cost of debt twice. Stock-based compensation is a real cost paid in
        shares instead of cash (it dilutes every holder), so it is deducted
        rather than left as the non-cash add-back operating cash flow treats
        it as. Operating leases now count as debt (see build()), so the
        interest embedded in their payments is added back the same way.

        Each adjustment uses the year's own figure when the ticker path
        supplied a per-year series, else the latest-year figure. A filing
        with no reported FCF falls back to the revenue × margin stand-in with
        no adjustments (there is no cash flow statement to adjust).
        """
        history = list(data.free_cash_flows or [])
        if not history:
            return self._synth_base(data), ""
        newest_first = data.fcf_history_order != "oldest_first"
        idx = list(range(len(history)))
        if not newest_first:
            idx.reverse()
        idx = idx[:_BASE_YEARS]

        def aligned(series: list, i: int, fallback: float | None) -> float | None:
            if series and len(series) == len(history) and series[i] is not None:
                return series[i]
            return fallback

        add_interest = self._adds_back_interest(data)
        lease_add = ((data.operating_lease_liabilities or 0.0) * lease_rate * (1 - tax))
        adjusted, n_int, n_sbc = [], 0, 0
        for i in idx:
            fcf = history[i]
            if add_interest:
                interest = aligned(data.interest_expense_series, i, data.interest_expense)
                if interest:
                    fcf += abs(interest) * (1 - tax)
                    n_int += 1
            sbc = aligned(data.sbc_series, i, data.stock_based_compensation)
            if sbc:
                fcf -= abs(sbc)
                n_sbc += 1
            adjusted.append(fcf + lease_add)
        base = sum(adjusted) / len(adjusted)

        parts = [f"average of the latest {len(adjusted)} reported year"
                 f"{'s' if len(adjusted) > 1 else ''}"]
        parts.append(f"+ after-tax interest ({n_int} of {len(adjusted)} years)" if n_int
                     else ("interest not added back (interest paid is classified as financing, "
                           "so operating cash flow never deducted it)"
                           if not add_interest and data.interest_paid_classification == "financing"
                           else "interest not added back (IFRS filer that doesn't say where interest "
                           "paid sits — adding it back could count it twice)"
                           if not add_interest and data.accounting_standard == "ifrs-full"
                           else "interest not added back (non-US filing; interest paid is usually "
                           "presented under financing)"
                           if not add_interest else "no interest figure to add back"))
        parts.append(f"− stock-based compensation ({n_sbc} of {len(adjusted)} years)" if n_sbc
                     else "no stock-based compensation figure to deduct")
        if lease_add:
            parts.append(f"+ after-tax operating-lease interest ({lease_rate:.2%} on the lease liability)")
        return base, "; ".join(parts)

    def _synth_fcfs(self, data: ExtractedFinancials, wacc: float,
                    g_terminal: float = 0.025) -> list[float]:
        """Fabricate the FCF projection when the filing reports none."""
        return self._project_fcfs(self._synth_base(data), data, g_terminal)


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
