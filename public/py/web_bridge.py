"""Browser-side bridge between the terminal UI and the real model classes.

Runs inside Pyodide. The front end calls :func:`run_model` with a mnemonic and
a flat dict of slider values; this module translates those scalars into each
model's constructor arguments (synthesizing series where a model consumes
series — covariance assembly for MPT, seeded return draws for VaR, factor-
history windowing for Fama-French) and returns results, the ``explain()``
markdown and the ``visualize()`` figure as JSON.

The model classes themselves are imported *unchanged* from ``src/`` — this file
contains zero pricing/valuation logic.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

import numpy as np

from src import (
    BinomialTreeModel,
    BlackScholesModel,
    CAPMModel,
    DiscountedCashFlowModel,
    FamaFrenchModel,
    GordonGrowthModel,
    HestonModel,
    IndASHiddenDebtModel,
    ModernPortfolioTheoryModel,
    MonteCarloOptionModel,
    ReverseDCFModel,
    ValueAtRiskModel,
)

_TRADING_DAYS = 252
_SEED = 20260702  # deterministic demo draws: same sliders -> same numbers


# --------------------------------------------------------------------------- #
# Builders: flat slider params -> model instance (+ optional extra outputs)
# --------------------------------------------------------------------------- #
def _build_dcf(p: dict) -> DiscountedCashFlowModel:
    # Project the FCF path from a base amount and constant growth ($M units;
    # shares are in millions, so per-share values land in plain dollars).
    years = int(p["years"])
    fcfs = [p["base_fcf"] * (1.0 + p["fcf_growth"]) ** t for t in range(1, years + 1)]
    return DiscountedCashFlowModel(
        free_cash_flows=fcfs,
        discount_rate=p["discount_rate"],
        terminal_growth=p["terminal_growth"],
        net_debt=p["net_debt"],
        shares_outstanding=p["shares_outstanding"],
    )


def _build_gg(p: dict) -> GordonGrowthModel:
    return GordonGrowthModel(
        dividend=p["dividend"],
        required_return=p["required_return"],
        growth=p["growth"],
    )


def _build_mpt(p: dict) -> ModernPortfolioTheoryModel:
    mu = np.array([p["mu1"], p["mu2"], p["mu3"]])
    sig = np.array([p["sigma1"], p["sigma2"], p["sigma3"]])
    rho = max(float(p["rho"]), -0.45)  # keep the 3-asset matrix positive-definite
    corr = np.full((3, 3), rho)
    np.fill_diagonal(corr, 1.0)
    cov = corr * np.outer(sig, sig)
    return ModernPortfolioTheoryModel(
        expected_returns=mu, covariance=cov, risk_free_rate=p["risk_free_rate"]
    )


def _build_var(p: dict) -> ValueAtRiskModel:
    # Scale annual mu/sigma to daily; historical & MC methods consume a seeded
    # synthetic daily return sample, parametric takes the moments directly.
    mu_d = p["mu_annual"] / _TRADING_DAYS
    sd_d = p["sigma_annual"] / math.sqrt(_TRADING_DAYS)
    common = dict(
        confidence_level=p["confidence"],
        horizon_days=int(p["horizon_days"]),
        portfolio_value=p["portfolio_value"],
        method=p["method"],
    )
    if p["method"] == "parametric":
        return ValueAtRiskModel(mean=mu_d, std=sd_d, **common)
    rng = np.random.default_rng(_SEED)
    returns = rng.normal(mu_d, sd_d, size=10 * _TRADING_DAYS)
    return ValueAtRiskModel(returns=returns, **common)


def _build_capm(p: dict) -> CAPMModel:
    return CAPMModel(
        risk_free_rate=p["risk_free_rate"],
        expected_market_return=p["expected_market_return"],
        beta=p["beta"],
    )


def _build_ff3(p: dict) -> tuple[FamaFrenchModel, dict]:
    # Real Ken French history (bundled snapshot) + user-chosen "true" loadings
    # -> synthetic asset returns; the regression must recover the loadings.
    factors = FamaFrenchModel.load_factors().tail(int(p["window"]))
    rng = np.random.default_rng(_SEED)
    eps = rng.normal(0.0, p["idio_sigma"], size=len(factors))
    r = (factors["RF"] + p["alpha"] + p["b_mkt"] * factors["Mkt-RF"]
         + p["s_smb"] * factors["SMB"] + p["h_hml"] * factors["HML"] + eps)
    extras = {
        "true_alpha": p["alpha"], "true_b_mkt": p["b_mkt"],
        "true_s_smb": p["s_smb"], "true_h_hml": p["h_hml"],
        "sample_start": str(factors.index[0]), "sample_end": str(factors.index[-1]),
    }
    return FamaFrenchModel(asset_returns=r.to_numpy(), factors=factors), extras


def _build_bsm(p: dict) -> BlackScholesModel:
    return BlackScholesModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        dividend_yield=p["dividend_yield"],
    )


def _build_crr(p: dict) -> BinomialTreeModel:
    return BinomialTreeModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        exercise=p["exercise"], dividend_yield=p["dividend_yield"],
        n_steps=int(p["n_steps"]),
    )


def _build_mc(p: dict) -> MonteCarloOptionModel:
    return MonteCarloOptionModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"], sigma=p["sigma"],
        maturity=p["maturity"], option_type=p["option_type"],
        dividend_yield=p["dividend_yield"], n_sims=int(p["n_sims"]),
        antithetic=bool(p["antithetic"]), seed=_SEED,
    )


def _build_hes(p: dict) -> HestonModel:
    return HestonModel(
        spot=p["spot"], strike=p["strike"], rate=p["rate"],
        maturity=p["maturity"], v0=p["v0"], kappa=p["kappa"],
        theta=p["theta"], xi=p["xi"], rho=p["rho"],
        option_type=p["option_type"],
    )


def _build_hdebt(p: dict) -> IndASHiddenDebtModel:
    return IndASHiddenDebtModel(
        net_income=p["net_income"], reported_net_debt=p["reported_net_debt"],
        reported_equity_value=p["reported_equity_value"],
        shares_outstanding=p["shares_outstanding"],
        annual_lease_payment=p["annual_lease_payment"],
        lease_term_years=int(p["lease_term_years"]),
        lease_discount_rate=p["lease_discount_rate"],
        reverse_factoring_exposure=p["reverse_factoring_exposure"],
        cl1_amount=p["cl1_amount"], cl1_probability=p["cl1_probability"],
        cl2_amount=p["cl2_amount"], cl2_probability=p["cl2_probability"],
        depreciation_amortization=p["depreciation_amortization"],
        rd_capitalized_amortization=p["rd_capitalized_amortization"],
        rd_cash_spend=p["rd_cash_spend"], maintenance_capex=p["maintenance_capex"],
    )


def _build_rdcf(p: dict) -> ReverseDCFModel:
    return ReverseDCFModel(
        current_price=p["current_price"], shares_outstanding=p["shares_outstanding"],
        net_debt=p["net_debt"], base_fcf=p["base_fcf"], base_revenue=p["base_revenue"],
        total_addressable_market=p["total_addressable_market"],
        years=int(p["years"]), discount_rate=p["discount_rate"],
        terminal_growth=p["terminal_growth"],
    )


BUILDERS: dict[str, Callable[[dict], Any]] = {
    "DCF": _build_dcf, "GG": _build_gg, "MPT": _build_mpt, "VAR": _build_var,
    "CAPM": _build_capm, "FF3": _build_ff3, "BSM": _build_bsm,
    "CRR": _build_crr, "MC": _build_mc, "HES": _build_hes,
    "HDEBT": _build_hdebt, "RDCF": _build_rdcf,
}


# --------------------------------------------------------------------------- #
# JSON sanitation: numpy scalars/arrays and non-finite floats -> JSON-safe
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (np.integer, int, bool)) or value is None:
        return value
    return str(value)


def run_model(mnemonic: str, params_json: str) -> str:
    """Build + run one model; returns a JSON payload for the front end."""
    params = json.loads(params_json)
    built = BUILDERS[mnemonic](params)
    model, extras = built if isinstance(built, tuple) else (built, {})

    t0 = time.perf_counter()
    results = model.calculate()
    calc_ms = (time.perf_counter() - t0) * 1000.0

    try:
        figure = model.visualize().to_json()
    except Exception as exc:  # chart failure must not take down the numbers
        figure = None
        extras = {**extras, "figure_error": str(exc)}

    return json.dumps({
        "ok": True,
        "results": _clean(results),
        "extras": _clean(extras),
        "explain": model.explain(),
        "figure": figure,
        "calc_ms": round(calc_ms, 2),
    })


# =========================================================================== #
# IB DESK — PDF analyzer (upload -> extract -> assume -> run -> export)
#
# Pipeline imports live inside the functions: the analyzer's pure-Python
# backends (pypdf, pdfminer.six, reportlab, openpyxl, python-docx) are
# micropip-installed on first use, after the main terminal has booted.
# =========================================================================== #
import base64
import re as _re

_ANALYZER: dict[str, Any] = {"data": None, "report": None, "period": None}

#: Fields the UI reports as FOUND/MISSING (order = display order).
_KEY_FIELDS = (
    "company_name", "ticker", "fiscal_year", "revenue", "free_cash_flows",
    "net_income", "total_debt", "cash_and_equivalents", "shares_outstanding",
    "current_price", "dividend_per_share", "beta", "revenue_growth",
    "operating_margin", "tax_rate", "depreciation_amortization", "rd_expense",
    "capital_expenditures",
)

_QUARTERLY_RE = _re.compile(
    r"(?i)\b(10-Q|quarterly report|three months ended|for the quarter ended|"
    r"third quarter|first quarter|second quarter|fourth quarter)\b")
_ANNUAL_RE = _re.compile(
    r"(?i)\b(10-K|annual report|fiscal year ended|for the year ended|"
    r"twelve months ended|full[- ]year)\b")


def _detect_period(text: str) -> str:
    """Classify the filing as annual or quarterly from its own language."""
    q = len(_QUARTERLY_RE.findall(text))
    a = len(_ANNUAL_RE.findall(text))
    return "quarterly" if q > a else "annual"


#: Every income-statement/cash-flow figure that accrues OVER the reporting
#: period, and therefore has to be scaled together when a quarter is put on
#: an annual footing. Depreciation & amortisation, R&D expense and capital
#: expenditure belong here for exactly the same reason revenue does — a
#: quarter's worth of each — and their omission was a real bug, not a
#: deliberate exclusion: the docstring below already claimed to scale
#: "flows", and these are flows.
#:
#: The damage was silent and specific. On a real Caplin Point quarterly
#: filing, revenue and net income were multiplied by 4 while D&A and R&D
#: stayed at one quarter, so the object handed to the models mixed two
#: different time bases. Nothing crashes; every ratio built from a mixed
#: pair just quietly comes out 4x wrong — D&A/revenue, R&D intensity, and
#: the interest-expense/total-debt cost of debt that feeds WACC.
_QUARTERLY_FLOW_FIELDS = (
    "revenue", "net_income", "interest_expense",
    "depreciation_amortization", "rd_expense", "capital_expenditures",
)

#: Point-in-time balances and per-share/market attributes, deliberately NOT
#: scaled: total debt, cash, share count, price, beta and volatility all
#: describe a moment or the equity itself, not a period's activity.
#: Dividend per share is scaled separately below — it IS a per-period flow,
#: but only when the filing states a quarterly dividend.


def _period_basis_label() -> str:
    """"ANNUAL", "QUARTERLY (x4 RUN-RATE)" or "QUARTERLY (TRAILING 12M)".

    Two quarterly filings analysed the same way can now produce annual
    figures that differ by several percent depending on whether the filing
    carried the period columns a TTM needs. Saying which was used is the
    difference between a reader being able to reconcile the report against
    the source and merely having to trust it.
    """
    period = _ANALYZER["period"] or "annual"
    if period != "quarterly":
        return period.upper()
    data = _ANALYZER["data"]
    used_ttm = bool(getattr(data, "ttm_flows", None)) if data is not None else False
    return "QUARTERLY (TRAILING 12M)" if used_ttm else "QUARTERLY (x4 RUN-RATE)"


def _annualise_quarterly(data: Any, dividend_is_periodic: bool = True) -> None:
    """Put quarterly *flow* figures on an annual footing, in place.

    Stocks (debt, cash, shares, price, beta) are point-in-time and unchanged.
    Every flow in :data:`_QUARTERLY_FLOW_FIELDS` is annualised, preferring a
    real trailing-twelve-month figure derived from the filing's own period
    columns and falling back to a x4 run-rate where none is available.

    The two are not interchangeable. A run-rate asserts that the other three
    quarters look like this one, which for a seasonal or simply growing
    company they do not: a real Caplin Point Q1 run-rates to ₹2,575.64 Cr of
    revenue against a true TTM of ₹2,413.28 Cr, a 6.7% overstatement carried
    straight into the DCF's base year and compounded by every projection
    built on top of it.

    A caveat worth stating plainly: TTM is applied per field, so a filing
    that states capex or R&D only in prose keeps those on the run-rate while
    revenue moves to TTM. That leaves a residual few-percent inconsistency
    in ratios spanning the two. It is the smaller error — abandoning TTM
    whenever any one field lacks a period-column row would give the run-rate
    back on almost every real filing, which is the larger error, uniformly
    applied.

    Args:
        data: The extracted financials, mutated in place.
        dividend_is_periodic: Whether ``dividend_per_share`` represents this
            quarter's dividend (so x4 gives the annual rate). A board-
            recommended FINAL dividend is an annual declaration already and
            must not be quadrupled — see :func:`analyze_pdf`.
    """
    ttm = getattr(data, "ttm_flows", None) or {}
    for field_name in _QUARTERLY_FLOW_FIELDS:
        value = getattr(data, field_name)
        if value is None:
            continue
        setattr(data, field_name, ttm.get(field_name, value * 4.0))
    data.free_cash_flows = [f * 4.0 for f in data.free_cash_flows]
    if dividend_is_periodic and data.dividend_per_share is not None:
        data.dividend_per_share *= 4.0
    # revenue_growth is deliberately NOT touched. It was previously
    # compounded as (1+g)**4, which treats it as a quarter-over-quarter
    # rate; it is not one. Every path that sets it produces a YEAR-over-year
    # figure already — _derive_yoy_metrics divides this quarter by the same
    # quarter one year earlier, and a filing that narrates its own growth in
    # a quarterly release means YoY by convention ("Q1 FY27 Total revenue at
    # ₹644 Crores; an increase of 20.7% YoY"). Compounding it turned a real
    # 20.7% into 112.4% and handed that to the DCF as a growth assumption.


#: Extraction fields the user may set by hand (numeric; ``net_debt`` is a
#  derived property and ``free_cash_flows`` a synthesised series, so neither
#  is directly editable — adjust their inputs instead).
_OVERRIDABLE_FIELDS = (
    "revenue", "net_income", "total_debt", "cash_and_equivalents",
    "shares_outstanding", "current_price", "dividend_per_share", "beta",
    "revenue_growth", "operating_margin", "tax_rate",
    "depreciation_amortization", "rd_expense", "capital_expenditures",
    "interest_expense",
)


def _assumed_preview(data: Any) -> dict:
    """The exact numbers the IB bot will use for each MISSING field.

    Mirrors :class:`AutoAssumer`'s defaults (and calls its own synthesiser for
    the FCF path) so the UI can show what "AUTO-ASSUMED" actually means —
    e.g. a company with no debt shows an assumed 0, not a hidden guess.

    Beta/margin/growth specifically call AutoAssumer's own
    ``_sector_baseline``/``_sector_growth`` helpers rather than
    reimplementing the sector-vs-flat-default choice a third time — that
    reimplementation is exactly how this preview drifted out of sync with
    reality in the first place: build() and _synth_fcfs() both gained a
    sector-baseline fallback, this function didn't, and the previewed
    free_cash_flows (which DOES call _synth_fcfs) came out computed from a
    different margin/growth pair than the beta/margin/growth values shown
    right next to it in the same response — silently breaking this
    docstring's own "the exact numbers" promise.
    """
    from src.pipeline import AutoAssumer

    auto = AutoAssumer()
    sector_baseline = auto._sector_baseline(data)
    spot = data.current_price or 100.0
    preview: dict[str, Any] = {}
    if data.current_price is None:
        preview["current_price"] = 100.0            # normalised units
    if data.beta is None:
        preview["beta"] = sector_baseline["beta"] if sector_baseline else auto.default_beta
    if data.dividend_per_share is None:
        preview["dividend_per_share"] = round(0.02 * spot, 4)
    if data.revenue is None:
        preview["revenue"] = 100.0                  # synth-FCF base, normalised
    if data.revenue_growth is None:
        preview["revenue_growth"] = auto._sector_growth(sector_baseline) or 0.05
    if data.operating_margin is None:
        preview["operating_margin"] = (
            sector_baseline["operating_margin"] if sector_baseline else 0.15)
    if data.tax_rate is None:
        preview["tax_rate"] = auto.tax
    if data.total_debt is None:
        preview["total_debt"] = 0.0
    if data.cash_and_equivalents is None:
        preview["cash_and_equivalents"] = 0.0
    if data.net_debt is None:
        preview["net_debt"] = 0.0
    if data.shares_outstanding is None:
        preview["shares_outstanding"] = 1_000_000   # VaR notional proxy
    if data.net_income is None:
        preview["net_income"] = None                # not consumed by any model
    if data.depreciation_amortization is None:
        preview["depreciation_amortization"] = 0.0   # HDEBT owner-earnings add-back
    if data.rd_expense is None:
        preview["rd_expense"] = 0.0                  # HDEBT owner-earnings R&D spend
    if data.capital_expenditures is None:
        preview["capital_expenditures"] = 0.0        # HDEBT maintenance-capex proxy
    if not data.free_cash_flows:
        preview["free_cash_flows"] = [round(f, 2) for f in auto._synth_fcfs(data, 0.09)]
    return _clean(preview)


def override_field(key: str, value: Any = None) -> str:
    """Manually set (or reset to auto) one extracted field.

    ``value`` is a number, or ``None``/empty to hand the field back to the
    auto-assumer. Returns the refreshed fields + assumed preview so the UI
    can re-render, and invalidates any computed report (assumptions changed).
    """
    data = _ANALYZER["data"]
    if data is None:
        return json.dumps({"ok": False, "error": "No PDF analysed yet."})
    if key not in _OVERRIDABLE_FIELDS:
        return json.dumps({"ok": False, "error": f"Field {key!r} is not manually editable."})
    try:
        val = None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "Enter a number."})
    setattr(data, key, val)
    _ANALYZER["report"] = None            # previous report used old assumptions
    return json.dumps({"ok": True, "fields": _clean(data.to_dict()),
                       "assumed": _assumed_preview(data)})


def get_assumed() -> str:
    """Assumed-value preview for the current extraction (history restores)."""
    data = _ANALYZER["data"]
    if data is None:
        return json.dumps({"ok": False, "error": "No extraction loaded."})
    return json.dumps({"ok": True, "assumed": _assumed_preview(data)})


def analyze_pdf(pdf_bytes: Any, period_mode: str = "auto") -> str:
    """Extract financials from an uploaded PDF; returns a JSON payload.

    Args:
        pdf_bytes: The raw PDF (JS ``Uint8Array`` proxy or Python bytes).
        period_mode: ``"auto"`` (detect from the filing's language),
            ``"annual"`` or ``"quarterly"``.
    """
    from src.pipeline import PDFExtractor

    raw = bytes(pdf_bytes.to_py()) if hasattr(pdf_bytes, "to_py") else bytes(pdf_bytes)
    try:
        data = PDFExtractor().extract(raw)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    period = period_mode if period_mode in ("annual", "quarterly") \
        else _detect_period(data.raw_text)
    if period == "quarterly":
        # A "Final Dividend ... for the financial year ended" recommended in
        # a quarterly filing is already the annual figure; only a genuinely
        # per-period dividend gets put on an annual footing.
        _annualise_quarterly(
            data, dividend_is_periodic=not getattr(data, "dividend_is_annual", False))

    _ANALYZER.update(data=data, report=None, period=period)
    fields = _clean(data.to_dict())
    missing = [k for k in _KEY_FIELDS
               if fields.get(k) in (None, [], "") and k != "free_cash_flows"
               or (k == "free_cash_flows" and not fields.get(k))]
    return json.dumps({
        "ok": True, "fields": fields, "missing": missing, "period": period,
        "backends": fields.get("backends_used", []),
        "assumed": _assumed_preview(data),   # what AUTO-ASSUMED will really use
    })


def load_fundamentals(fields_json: str) -> str:
    """Load a company from SEC XBRL data instead of an uploaded PDF.

    ``fields_json`` is the ``fields`` object from ``/api/fundamentals`` —
    already keyed to :class:`ExtractedFinancials` and already in raw currency
    units, the same scale the PDF extractor normalises to, so nothing is
    rescaled here.

    Returns exactly the payload shape :func:`analyze_pdf` returns, so the whole
    downstream UI (extraction grid, missing-field warnings, assumption preview,
    model run, export) works against a ticker load without a second code path.
    The alternative — a parallel render path for tickers — is how the two
    drift apart and how one of them quietly stops reporting missing fields.
    """
    from src.pipeline.pdf_extractor import ExtractedFinancials

    try:
        fields = json.loads(fields_json)
        allowed = set(ExtractedFinancials.__dataclass_fields__)
        kwargs = {k: v for k, v in fields.items() if k in allowed}
        if not kwargs.get("free_cash_flows"):
            kwargs["free_cash_flows"] = []
        #: Provenance marker. assumptions.py reads backends_used to decide how
        #  to DESCRIBE a figure's origin, and must never call an XBRL fact
        #  "scraped from PDF" — being able to say where each number came from
        #  is the whole point of this tool.
        kwargs.setdefault("backends_used", ["sec-edgar-xbrl"])
        #: /api/fundamentals sorts its FCF series by period end, ascending.
        kwargs.setdefault("fcf_history_order", "oldest_first")
        data = ExtractedFinancials(**kwargs)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    #: SEC company facts are annual-period figures already; there is nothing to
    #  annualise, so the quarterly ×4 path is deliberately not run here.
    _ANALYZER.update(data=data, report=None, period="annual")
    clean = _clean(data.to_dict())
    missing = [k for k in _KEY_FIELDS
               if clean.get(k) in (None, [], "") and k != "free_cash_flows"
               or (k == "free_cash_flows" and not clean.get(k))]
    return json.dumps({
        "ok": True, "fields": clean, "missing": missing, "period": "annual",
        "backends": clean.get("backends_used", []),
        "assumed": _assumed_preview(data),
        "source": "sec-edgar-xbrl",
    })


def run_report(params_json: str) -> str:
    """Build assumptions (auto or manual) and run the selected models.

    ``params_json``: ``{"mode": "auto"|"manual", "selected": [names],
    "live_rf": float|null, "rf_source": str, "erp": float|null,
    "country": str, "currency": str, "overrides": {field: value}}``.

    ``live_rf`` and ``erp`` carry the SELECTED COUNTRY's live 10-year
    sovereign yield and Damodaran equity risk premium from the browser, so an
    Indian filing analysed under the India market uses India's cost of
    capital — never a hardcoded US base case.
    """
    from src.pipeline import (
        AnalysisRunner, AutoAssumer, ManualAssumer, ManualOverrides,
    )

    p = json.loads(params_json)
    data = _ANALYZER["data"]
    if data is None:
        return json.dumps({"ok": False, "error": "No PDF analysed yet — upload a filing first."})

    auto_kwargs = {}
    if p.get("live_rf") is not None:
        auto_kwargs["risk_free_rate"] = float(p["live_rf"])
    if p.get("erp") is not None:
        auto_kwargs["equity_risk_premium"] = float(p["erp"])
    auto = AutoAssumer(**auto_kwargs)

    if p.get("mode") == "manual":
        allowed = set(ManualOverrides.__dataclass_fields__)
        raw_overrides = {k: v for k, v in (p.get("overrides") or {}).items()
                         if k in allowed and v is not None}
        for int_field in ("var_horizon_days", "monte_carlo_paths", "lease_term_years"):
            if int_field in raw_overrides:
                raw_overrides[int_field] = int(raw_overrides[int_field])
        assumptions = ManualAssumer(auto).build(data, ManualOverrides(**raw_overrides))
        mode = "manual"
    else:
        assumptions = auto.build(data)
        mode = "auto"

    report = AnalysisRunner(data).run(assumptions, list(p.get("selected") or []), mode)
    _ANALYZER["report"] = report

    summary = report.summary_frame().to_dict(orient="records")
    rationale = {f"{model} · {param}": text
                 for (model, param), text in assumptions.rationale.items()}
    # Audit trail: which market the numbers were built in, in what currency.
    market_context = dict(assumptions.market_context)
    for extra_key in ("country", "currency", "fx_per_usd"):
        if p.get(extra_key) not in (None, ""):
            market_context[extra_key] = p[extra_key]
    if p.get("erp") is not None:
        market_context["equity_risk_premium"] = float(p["erp"])
    return json.dumps({
        "ok": True, "mode": mode, "summary": summary,
        "results": _clean(report.results), "errors": report.errors,
        "market_context": _clean(market_context),
        "rationale": rationale,
        "currency_symbol": p.get("currency_symbol") or "$",
        "rf_source": p.get("rf_source") or "default (Damodaran base case 4.25%)",
    })


def restore_extraction(fields_json: str, period: str = "annual") -> str:
    """Rehydrate a saved extraction (browser history) into the analyzer state.

    Saved analyses live in ``localStorage``; on reopen the UI shows the stored
    snapshot, but ``run_report``/``export_report`` need the Python-side
    ``ExtractedFinancials`` object back. ``fields_json`` is exactly what
    ``analyze_pdf`` returned (post-annualisation, so no re-scaling here).
    """
    from src.pipeline.pdf_extractor import ExtractedFinancials

    try:
        fields = json.loads(fields_json)
        allowed = set(ExtractedFinancials.__dataclass_fields__)
        kwargs = {k: v for k, v in fields.items() if k in allowed}
        if not kwargs.get("free_cash_flows"):
            kwargs["free_cash_flows"] = []
        #: Analyses saved before fcf_history_order existed don't carry it.
        #  Ticker loads were always ascending, so infer it from provenance
        #  rather than defaulting a saved SEC series to the PDF order.
        if "fcf_history_order" not in kwargs and any(
                "sec-edgar" in str(b).lower() for b in kwargs.get("backends_used") or []):
            kwargs["fcf_history_order"] = "oldest_first"
        data = ExtractedFinancials(**kwargs)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    _ANALYZER.update(
        data=data, report=None,
        period=period if period in ("annual", "quarterly") else "annual")
    return json.dumps({"ok": True, "company": data.company_name})


def _fmt_docx(value: Any) -> str:
    """Human formatting for report values (mirrors the terminal grid)."""
    if isinstance(value, float):
        if abs(value) >= 1e5:
            return f"{value:,.0f}"
        if abs(value) >= 100:
            return f"{value:,.2f}"
        return f"{value:.6g}"
    if isinstance(value, list):
        return f"series · {len(value)} pts"
    return str(value)


def _build_docx(report: Any, path: str) -> None:
    """Write the analysis as a .docx — the format Google Docs imports natively."""
    import docx  # python-docx (lxml comes from the Pyodide distribution)

    doc = docx.Document()
    company = report.company.company_name or "Uploaded company"
    doc.add_heading(f"Financial Model Report — {company}", level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"Mode: {report.mode.upper()}   ·   Period basis: "
        f"{_period_basis_label()}   ·   "
        f"Generated by FINMODELS Terminal (in-browser Python)").italic = True

    doc.add_heading("Extracted financials", level=1)
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for key, value in report.company.to_dict().items():
        if key == "backends_used" or value in (None, [], ""):
            continue
        cells = table.add_row().cells
        cells[0].text = key.replace("_", " ")
        cells[1].text = _fmt_docx(value)

    doc.add_heading("Assumptions (market context)", level=1)
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for key, value in report.assumptions.market_context.items():
        cells = table.add_row().cells
        cells[0].text = key.replace("_", " ")
        cells[1].text = _fmt_docx(value)

    doc.add_heading("Model results", level=1)
    for model_name, results in report.results.items():
        doc.add_heading(model_name, level=2)
        table = doc.add_table(rows=0, cols=2)
        table.style = "Light Grid Accent 1"
        for key, value in results.items():
            cells = table.add_row().cells
            cells[0].text = key.replace("_", " ")
            cells[1].text = _fmt_docx(value)

    if report.errors:
        doc.add_heading("Models not run", level=1)
        for model_name, err in report.errors.items():
            doc.add_paragraph(f"{model_name}: {err}")
    doc.save(path)


def export_report(fmt: str) -> str:
    """Render the last report as pdf / docx / xlsx; returns base64 JSON."""
    from src.pipeline import export_pdf, export_xlsx

    report = _ANALYZER["report"]
    if report is None:
        return json.dumps({"ok": False, "error": "Run a report before exporting."})

    company = (report.company.company_name or "company").strip()
    slug = _re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_").lower() or "company"
    path = f"/tmp/{slug}_report.{fmt}"
    try:
        if fmt == "pdf":
            export_pdf(report, path)
            mime = "application/pdf"
        elif fmt == "xlsx":
            export_xlsx(report, path)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "docx":
            _build_docx(report, path)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            return json.dumps({"ok": False, "error": f"Unknown format {fmt!r}"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode()
    return json.dumps({"ok": True, "filename": f"{slug}_report.{fmt}",
                       "mime": mime, "b64": payload})
