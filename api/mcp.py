"""POST /api/mcp — Model Context Protocol server for the FINMODELS terminal.

Lets an AI client (Claude, Claude Code, Cursor, or anything else speaking MCP)
call this product's quantitative models directly as tools, instead of a human
driving the sliders in a browser. Same model code, different front door.

WHY THIS FILE EXISTS AT ALL, IN THIS SHAPE
------------------------------------------
MCP is JSON-RPC 2.0 over a single HTTP POST endpoint. That is *exactly* what a
Vercel Python function already is, so this needs no framework and — critically
— NO NEW DEPENDENCIES. That is not a stylistic preference: requirements.txt is
shared by every Python function in this project, and its comment records that
adding pandas measured ~259MB against this project's real 225MB bundle limit
(see api/premium.py's docstring for the three failed preview builds that
established it). A framework, or an MCP SDK, risks that budget and would take
api/premium.py down with it. stdlib json + BaseHTTPRequestHandler cannot.

Model classes are imported from src/ unchanged — the same files the browser
runs under Pyodide (via scripts/sync_web_assets.py) and the same ones
api/premium.py imports. Nothing here reimplements a formula, so nothing here
can drift away from the tested implementation.

DUAL-ERA, ON PURPOSE
--------------------
MCP revision 2026-07-28 removed the `initialize` handshake, protocol-level
sessions, and the GET stream: "modern" clients declare their version in each
request's `_meta` and discover the server with a mandatory `server/discover`.
Revisions 2025-11-25 and earlier ("legacy") still expect `initialize`. Real
clients are spread across both eras right now, so this server implements BOTH
on the one endpoint and picks its behaviour from how the client opens — the
spec's "dual-era server". Supporting only one era would silently fail for
roughly half of the clients that might connect.

AUTH: BEARER ONLY, NEVER THE COOKIE
-----------------------------------
The nine free models need no credential at all, so this endpoint can be added
to a client by URL alone. The two paid models re-derive entitlement from Redis
exactly the way api/premium.py does — never from anything the caller claims.

This endpoint deliberately does NOT read the `fm_sess` cookie, even though
api/premium.py does. api/premium.py is same-origin, called by our own page. An
MCP endpoint is cross-origin by nature, so honouring an ambient cookie here
would let any website a signed-in user visits spend that user's paid
entitlement (and their monthly quota) with a scripted cross-origin POST. A
bearer token has to be deliberately handed over, so there is no ambient
credential to steal. That is also *why* it is safe for this endpoint to accept
any Origin: the DNS-rebinding attack the MCP spec's Origin rule defends
against needs an ambient credential to be worth mounting, and there isn't one.

WHAT IS NOT HERE: FAMA-FRENCH
-----------------------------
FF3 is the one model of the twelve that cannot run in this function.
FamaFrenchModel.__init__ does an unconditional `import pandas as pd` and
_build_ff3's factor history is a DataFrame — and pandas is the exact
dependency that blew the bundle limit above. Omitting it is a real gap, so it
is named explicitly in the server instructions and in list_models rather than
quietly left out of the tool list: a caller who wants FF3 is told where to get
it instead of being left to wonder why eleven of twelve models showed up.

Response: MCP JSON-RPC over HTTP. See scripts/test_mcp_api.py for the
conformance suite this is verified against.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

import numpy as np

from src import (
    BinomialTreeModel,
    BlackScholesModel,
    CAPMModel,
    DiscountedCashFlowModel,
    GordonGrowthModel,
    HestonModel,
    IndASHiddenDebtModel,
    ModernPortfolioTheoryModel,
    MonteCarloOptionModel,
    ReverseDCFModel,
    ValueAtRiskModel,
)

SERVER_NAME = "finmodels-terminal"
SERVER_VERSION = "1.0.0"
SITE = "https://financial-models-six.vercel.app"

#: Protocol revisions this server implements, newest first. "2026-07-28" is
#: the modern (per-request _meta) era; the rest are legacy (initialize
#: handshake). Advertised verbatim in server/discover and in the `supported`
#: list of an UnsupportedProtocolVersionError.
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)

#: Mirrors public/py/web_bridge.py — deterministic draws so the same arguments
#: always produce the same numbers. An MCP tool that returned a different
#: answer each call would be untestable and would quietly poison any analysis
#: built on two calls to it.
_TRADING_DAYS = 252
_SEED = 20260702

REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")

# JSON-RPC + MCP error codes. -32020/-32022 are allocated by the MCP spec from
# its reserved protocol-error sub-range; the rest are plain JSON-RPC 2.0.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_HEADER_MISMATCH = -32020
ERR_UNSUPPORTED_VERSION = -32022
#: Plain JSON-RPC 2.0 "Server error" range (-32000 to -32099), not an
#: MCP-allocated code — rate limiting is this server's own policy, not a
#: protocol-level concept, so it doesn't belong in the -3202x cluster above.
ERR_RATE_LIMITED = -32000


# --------------------------------------------------------------------------- #
# Parameter registry
#
# One entry per model, mirroring the MODELS registry in
# public/assets/terminal.js — same ids, same bounds, same defaults, so a tool
# call and a slider drag are the same computation with the same limits.
# tests/test_mcp_schema.py parses that JS registry and fails if the two drift,
# which is the only thing keeping this copy honest.
#
# "kind" drives both JSON-schema generation and validation:
#   num  -> number, clamped to [min, max]
#   int  -> integer, clamped to [min, max]
#   enum -> one of `choices`
# --------------------------------------------------------------------------- #
def _n(pid, lo, hi, default, desc, pct=False):
    return {"id": pid, "kind": "num", "min": lo, "max": hi, "def": default,
            "desc": desc, "pct": pct}


def _i(pid, lo, hi, default, desc):
    return {"id": pid, "kind": "int", "min": lo, "max": hi, "def": default, "desc": desc}


def _e(pid, choices, default, desc):
    return {"id": pid, "kind": "enum", "choices": list(choices), "def": default,
            "desc": desc}


PARAMS: dict[str, list[dict]] = {
    "DCF": [
        _n("base_fcf", 1, 500, 100.0, "Base-year free cash flow, in millions of currency units."),
        _n("fcf_growth", -0.10, 0.25, 0.08, "Annual FCF growth over the explicit horizon, as a decimal (0.08 = 8%).", pct=True),
        _i("years", 3, 10, 5, "Length of the explicit forecast horizon, in years."),
        _n("discount_rate", 0.04, 0.20, 0.09, "WACC / discount rate, as a decimal (0.09 = 9%).", pct=True),
        _n("terminal_growth", 0.0, 0.05, 0.025, "Perpetual growth rate in the terminal value, as a decimal. Must be below discount_rate.", pct=True),
        _n("net_debt", -500, 2000, 250.0, "Net debt (debt minus cash), in millions. Negative means net cash."),
        _n("shares_outstanding", 10, 2000, 150.0, "Diluted shares outstanding, in millions."),
    ],
    "GG": [
        _n("dividend", 0.1, 20, 2.5, "Most recent annual dividend per share, D0, in currency units."),
        _n("required_return", 0.02, 0.25, 0.08, "Required rate of return, as a decimal. Must exceed growth.", pct=True),
        _n("growth", 0.0, 0.10, 0.04, "Perpetual dividend growth rate, as a decimal.", pct=True),
    ],
    "MPT": [
        _n("mu1", 0, 0.25, 0.08, "Expected annual return of asset 1, as a decimal.", pct=True),
        _n("mu2", 0, 0.25, 0.12, "Expected annual return of asset 2, as a decimal.", pct=True),
        _n("mu3", 0, 0.25, 0.15, "Expected annual return of asset 3, as a decimal.", pct=True),
        _n("sigma1", 0.05, 0.60, 0.15, "Annual volatility of asset 1, as a decimal.", pct=True),
        _n("sigma2", 0.05, 0.60, 0.22, "Annual volatility of asset 2, as a decimal.", pct=True),
        _n("sigma3", 0.05, 0.60, 0.30, "Annual volatility of asset 3, as a decimal.", pct=True),
        _n("rho", -0.45, 0.90, 0.25, "Uniform pairwise correlation between the three assets. Floored at -0.45 to keep the covariance matrix positive-definite."),
        _n("risk_free_rate", 0, 0.08, 0.03, "Risk-free rate used for the tangency/max-Sharpe portfolio, as a decimal.", pct=True),
    ],
    "VAR": [
        _n("mu_annual", -0.20, 0.30, 0.07, "Expected annual return of the portfolio, as a decimal.", pct=True),
        _n("sigma_annual", 0.05, 0.80, 0.20, "Annual volatility of the portfolio, as a decimal.", pct=True),
        _n("confidence", 0.90, 0.99, 0.95, "VaR confidence level, as a decimal (0.95 = 95%).", pct=True),
        _i("horizon_days", 1, 30, 10, "Risk horizon, in trading days."),
        _n("portfolio_value", 0.1, 1000, 100.0, "Portfolio value, in millions of currency units."),
        _e("method", ("historical", "parametric", "monte_carlo"), "historical",
           "Estimation method. 'parametric' uses the mean/vol directly; 'historical' and 'monte_carlo' consume a seeded synthetic daily return sample built from them."),
    ],
    "CAPM": [
        _n("risk_free_rate", 0, 0.08, 0.042, "Risk-free rate, as a decimal.", pct=True),
        _n("expected_market_return", 0.02, 0.20, 0.09, "Expected return of the market portfolio, as a decimal.", pct=True),
        _n("beta", -1, 3, 1.15, "Systematic risk of the asset relative to the market."),
    ],
    "BSM": [
        _n("spot", 1, 500, 100.0, "Current price of the underlying."),
        _n("strike", 1, 500, 100.0, "Strike price of the option."),
        _n("rate", 0, 0.15, 0.05, "Continuously compounded risk-free rate, as a decimal.", pct=True),
        _n("sigma", 0.05, 1.0, 0.20, "Annualised volatility of the underlying, as a decimal.", pct=True),
        _n("maturity", 0.05, 5, 1.0, "Time to expiry, in years."),
        _n("dividend_yield", 0, 0.08, 0.0, "Continuous dividend yield, as a decimal.", pct=True),
        _e("option_type", ("call", "put"), "call", "Option type."),
    ],
    "CRR": [
        _n("spot", 1, 500, 100.0, "Current price of the underlying."),
        _n("strike", 1, 500, 100.0, "Strike price of the option."),
        _n("rate", 0, 0.15, 0.05, "Continuously compounded risk-free rate, as a decimal.", pct=True),
        _n("sigma", 0.05, 1.0, 0.20, "Annualised volatility of the underlying, as a decimal.", pct=True),
        _n("maturity", 0.05, 5, 1.0, "Time to expiry, in years."),
        _n("dividend_yield", 0, 0.08, 0.0, "Continuous dividend yield, as a decimal.", pct=True),
        _i("n_steps", 10, 2000, 500, "Number of steps in the binomial lattice. More steps converge toward Black-Scholes."),
        _e("option_type", ("call", "put"), "call", "Option type."),
        _e("exercise", ("european", "american"), "european",
           "Exercise style. American allows early exercise, which is what makes the lattice worth using over Black-Scholes."),
    ],
    "MC": [
        _n("spot", 1, 500, 100.0, "Current price of the underlying."),
        _n("strike", 1, 500, 100.0, "Strike price of the option."),
        _n("rate", 0, 0.15, 0.05, "Continuously compounded risk-free rate, as a decimal.", pct=True),
        _n("sigma", 0.05, 1.0, 0.20, "Annualised volatility of the underlying, as a decimal.", pct=True),
        _n("maturity", 0.05, 5, 1.0, "Time to expiry, in years."),
        _n("dividend_yield", 0, 0.08, 0.0, "Continuous dividend yield, as a decimal.", pct=True),
        _i("n_sims", 10000, 500000, 100000, "Number of simulated price paths. Seeded, so a given argument set always returns the same price."),
        _e("option_type", ("call", "put"), "call", "Option type."),
        _e("antithetic", (True, False), True, "Use antithetic variates for variance reduction."),
    ],
    "HES": [
        _n("spot", 1, 500, 100.0, "Current price of the underlying."),
        _n("strike", 1, 500, 100.0, "Strike price of the option."),
        _n("rate", 0, 0.15, 0.03, "Continuously compounded risk-free rate, as a decimal.", pct=True),
        _n("maturity", 0.1, 5, 1.0, "Time to expiry, in years."),
        _n("v0", 0.005, 0.5, 0.04, "Initial instantaneous variance (not volatility): 0.04 corresponds to 20% vol."),
        _n("kappa", 0.1, 10, 2.0, "Speed of mean reversion of the variance process."),
        _n("theta", 0.005, 0.5, 0.04, "Long-run mean of the variance process."),
        _n("xi", 0.05, 1.5, 0.5, "Volatility of volatility."),
        _n("rho", -0.95, 0.5, -0.7, "Correlation between the asset and variance Brownian motions. Negative values produce the equity volatility skew."),
        _e("option_type", ("call", "put"), "call", "Option type."),
    ],
    "HDEBT": [
        _n("net_income", -500, 1000, 100.0, "Reported net income, in millions."),
        _n("reported_net_debt", -500, 2000, 500.0, "Net debt as reported on the balance sheet, in millions."),
        _n("reported_equity_value", -1000, 5000, 2000.0, "Reported market value of equity, in millions."),
        _n("shares_outstanding", 10, 2000, 100.0, "Diluted shares outstanding, in millions."),
        _n("annual_lease_payment", 0, 200, 50.0, "Annual operating-lease payment to capitalise, in millions."),
        _i("lease_term_years", 1, 15, 3, "Remaining weighted-average lease term, in years."),
        _n("lease_discount_rate", 0.02, 0.15, 0.08, "Incremental borrowing rate used to discount the lease, as a decimal.", pct=True),
        _n("reverse_factoring_exposure", 0, 500, 75.0, "Supply-chain / reverse-factoring balance treated as debt, in millions."),
        _n("cl1_amount", 0, 1000, 200.0, "Gross amount of contingent liability 1, in millions."),
        _n("cl1_probability", 0, 1, 0.25, "Probability that contingent liability 1 crystallises, as a decimal.", pct=True),
        _n("cl2_amount", 0, 1000, 100.0, "Gross amount of contingent liability 2, in millions."),
        _n("cl2_probability", 0, 1, 0.10, "Probability that contingent liability 2 crystallises, as a decimal.", pct=True),
        _n("depreciation_amortization", 0, 300, 40.0, "Depreciation and amortisation add-back, in millions."),
        _n("rd_capitalized_amortization", 0, 200, 15.0, "Amortisation of previously capitalised R&D, in millions."),
        _n("rd_cash_spend", 0, 300, 25.0, "Current-year cash R&D spend, in millions."),
        _n("maintenance_capex", 0, 300, 30.0, "Maintenance capital expenditure, in millions."),
    ],
    "RDCF": [
        _n("current_price", 1, 2000, 42.0, "Current market price per share."),
        _n("shares_outstanding", 1, 5000, 100.0, "Diluted shares outstanding, in millions."),
        _n("net_debt", -1000, 5000, 200.0, "Net debt, in millions."),
        _n("base_fcf", 1, 1000, 100.0, "Base-year free cash flow, in millions."),
        _n("base_revenue", 1, 5000, 1000.0, "Base-year revenue, in millions."),
        _n("total_addressable_market", 1, 100000, 10000.0, "Total addressable market, in millions. Used to sanity-check the implied revenue against a ceiling."),
        _i("years", 1, 10, 5, "Length of the explicit horizon, in years."),
        _n("discount_rate", 0.04, 0.20, 0.10, "WACC / discount rate, as a decimal.", pct=True),
        _n("terminal_growth", 0.0, 0.05, 0.03, "Perpetual growth rate in the terminal value, as a decimal.", pct=True),
    ],
}

MODEL_META = {
    "DCF": ("dcf", "Discounted Cash Flow", "EV = sum FCF_t/(1+r)^t + TV/(1+r)^N",
            "Intrinsic enterprise and equity value from a projected free-cash-flow path plus a Gordon terminal value. Returns enterprise value, equity value and value per share."),
    "GG": ("gordon_growth", "Gordon Growth / Dividend Discount", "P0 = D1 / (r - g)",
           "Values a share as a constantly growing dividend perpetuity."),
    "MPT": ("mpt", "Modern Portfolio Theory", "min w'Sw s.t. w'1 = 1",
            "Markowitz mean-variance optimisation over three assets: efficient frontier, global-minimum-variance portfolio and the max-Sharpe tangency portfolio with its weights."),
    "VAR": ("var_cvar", "Value at Risk / CVaR", "VaR_a = -Q_a(P&L), CVaR = E[loss | loss > VaR]",
            "Downside risk of a portfolio by historical, parametric or Monte Carlo method, with expected shortfall (CVaR) alongside VaR."),
    "CAPM": ("capm", "Capital Asset Pricing Model", "E[R] = rf + beta (E[Rm] - rf)",
             "Expected return of an asset given its systematic risk."),
    "BSM": ("black_scholes", "Black-Scholes-Merton", "C = S e^-qT N(d1) - K e^-rT N(d2)",
            "Closed-form European option price plus the full set of Greeks (delta, gamma, vega, theta, rho)."),
    "CRR": ("binomial", "Binomial Tree (Cox-Ross-Rubinstein)", "p = (e^(r-q)dt - d)/(u - d)",
            "European or American option price on a recombining lattice. Use this rather than Black-Scholes when early exercise matters."),
    "MC": ("monte_carlo", "Monte Carlo (geometric Brownian motion)", "C = e^-rT mean[payoff(S_T)]",
           "Simulation-based European option price with antithetic variates and a standard error, so you can see how converged the estimate is."),
    "HES": ("heston", "Heston Stochastic Volatility", "dv_t = kappa(theta - v_t)dt + xi sqrt(v_t) dW_t",
            "Semi-analytical option price under stochastic volatility via the characteristic function. Reproduces the volatility smile that Black-Scholes cannot."),
    "HDEBT": ("hidden_debt", "Ind AS 116 Hidden-Debt Normalizer",
              "Adjusted debt = reported + L + F + sum(A_i p_i), L = C(1-(1+r)^-n)/r",
              "Forensic-accounting normalisation: capitalises operating leases, reverse-factoring exposure and probability-weighted contingent liabilities onto the balance sheet, then recomputes owner earnings and a debt-adjusted valuation."),
    "RDCF": ("reverse_dcf", "Reverse DCF (market-implied expectations)",
             "Solve g such that EV(g) = price x shares + net debt",
             "Inverts the DCF identity: takes the market price as given and solves numerically for the free-cash-flow growth rate — and implied revenue versus TAM — that the price already embeds."),
}

PREMIUM = {"HDEBT", "RDCF"}

#: The one model that cannot run here. Named explicitly everywhere a caller
#: might notice its absence, rather than silently missing from tools/list.
OMITTED = {
    "FF3": ("Fama-French 3-Factor", (
        "Not available over MCP. The regression needs pandas, which does not fit "
        "in this function's serverless bundle budget alongside numpy and scipy. "
        f"Run it in the browser terminal at {SITE} (mnemonic FF3), where the "
        "whole scientific Python stack is already loaded via WebAssembly."
    )),
}


# --------------------------------------------------------------------------- #
# Builders: validated arguments -> model instance.
# Mirrors public/py/web_bridge.py's BUILDERS. Inlined for the same reason
# api/premium.py inlines _build_hdebt/_build_rdcf: web_bridge.py lives under
# public/py/, outside this function's file tree, and Vercel's Python bundler
# does not reliably trace a file reached only by a runtime sys.path insert
# (confirmed by a failed preview build — see api/premium.py's docstring).
# --------------------------------------------------------------------------- #
def _b_dcf(p):
    years = int(p["years"])
    fcfs = [p["base_fcf"] * (1.0 + p["fcf_growth"]) ** t for t in range(1, years + 1)]
    return DiscountedCashFlowModel(
        free_cash_flows=fcfs, discount_rate=p["discount_rate"],
        terminal_growth=p["terminal_growth"], net_debt=p["net_debt"],
        shares_outstanding=p["shares_outstanding"])


def _b_gg(p):
    return GordonGrowthModel(dividend=p["dividend"],
                             required_return=p["required_return"], growth=p["growth"])


def _b_mpt(p):
    mu = np.array([p["mu1"], p["mu2"], p["mu3"]])
    sig = np.array([p["sigma1"], p["sigma2"], p["sigma3"]])
    rho = max(float(p["rho"]), -0.45)   # keeps the 3-asset matrix positive-definite
    corr = np.full((3, 3), rho)
    np.fill_diagonal(corr, 1.0)
    return ModernPortfolioTheoryModel(expected_returns=mu, covariance=corr * np.outer(sig, sig),
                                      risk_free_rate=p["risk_free_rate"])


def _b_var(p):
    mu_d = p["mu_annual"] / _TRADING_DAYS
    sd_d = p["sigma_annual"] / math.sqrt(_TRADING_DAYS)
    common = dict(confidence_level=p["confidence"], horizon_days=int(p["horizon_days"]),
                  portfolio_value=p["portfolio_value"], method=p["method"])
    if p["method"] == "parametric":
        return ValueAtRiskModel(mean=mu_d, std=sd_d, **common)
    rng = np.random.default_rng(_SEED)
    return ValueAtRiskModel(returns=rng.normal(mu_d, sd_d, size=10 * _TRADING_DAYS), **common)


def _b_capm(p):
    return CAPMModel(risk_free_rate=p["risk_free_rate"],
                     expected_market_return=p["expected_market_return"], beta=p["beta"])


def _b_bsm(p):
    return BlackScholesModel(spot=p["spot"], strike=p["strike"], rate=p["rate"],
                             sigma=p["sigma"], maturity=p["maturity"],
                             option_type=p["option_type"], dividend_yield=p["dividend_yield"])


def _b_crr(p):
    return BinomialTreeModel(spot=p["spot"], strike=p["strike"], rate=p["rate"],
                             sigma=p["sigma"], maturity=p["maturity"],
                             option_type=p["option_type"], exercise=p["exercise"],
                             dividend_yield=p["dividend_yield"], n_steps=int(p["n_steps"]))


def _b_mc(p):
    return MonteCarloOptionModel(spot=p["spot"], strike=p["strike"], rate=p["rate"],
                                 sigma=p["sigma"], maturity=p["maturity"],
                                 option_type=p["option_type"],
                                 dividend_yield=p["dividend_yield"],
                                 n_sims=int(p["n_sims"]), antithetic=bool(p["antithetic"]),
                                 seed=_SEED)


def _b_hes(p):
    return HestonModel(spot=p["spot"], strike=p["strike"], rate=p["rate"],
                       maturity=p["maturity"], v0=p["v0"], kappa=p["kappa"],
                       theta=p["theta"], xi=p["xi"], rho=p["rho"],
                       option_type=p["option_type"])


def _b_hdebt(p):
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
        rd_cash_spend=p["rd_cash_spend"], maintenance_capex=p["maintenance_capex"])


def _b_rdcf(p):
    return ReverseDCFModel(
        current_price=p["current_price"], shares_outstanding=p["shares_outstanding"],
        net_debt=p["net_debt"], base_fcf=p["base_fcf"], base_revenue=p["base_revenue"],
        total_addressable_market=p["total_addressable_market"], years=int(p["years"]),
        discount_rate=p["discount_rate"], terminal_growth=p["terminal_growth"])


BUILDERS = {
    "DCF": _b_dcf, "GG": _b_gg, "MPT": _b_mpt, "VAR": _b_var, "CAPM": _b_capm,
    "BSM": _b_bsm, "CRR": _b_crr, "MC": _b_mc, "HES": _b_hes,
    "HDEBT": _b_hdebt, "RDCF": _b_rdcf,
}

TOOL_TO_MNEMONIC = {f"finmodels_{MODEL_META[m][0]}": m for m in BUILDERS}


# --------------------------------------------------------------------------- #
# JSON sanitation — numpy scalars/arrays and non-finite floats -> JSON-safe.
# Mirrors web_bridge.py's _clean(). NaN/Infinity are not valid JSON, and a
# client that receives them either fails to parse or silently reads a null it
# thinks is a real number; mapping them to null makes the gap explicit.
# --------------------------------------------------------------------------- #
def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (np.integer, int)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #
class ToolError(Exception):
    """A tool *execution* error: the caller can fix this by retrying with
    different arguments. Surfaced as isError:true in a CallToolResult rather
    than a JSON-RPC error, because the MCP spec reserves protocol errors for
    things a model cannot self-correct, and a model absolutely can correct an
    out-of-range number if it is told the bound."""


def _validate(mnemonic: str, args: dict) -> tuple[dict, list[str]]:
    """Coerce and bounds-check `args`, filling defaults for anything absent.

    Returns (values, defaulted) where `defaulted` names every parameter the
    caller did NOT supply. That list is reported back in every result on
    purpose: the dangerous failure mode for a valuation tool is not a crash,
    it is a plausible number silently computed from assumptions the caller
    never made and cannot see. This project has already shipped that bug once
    — a fabricated price/share-count placeholder produced a
    "$54,280,899,174.28" DCF headline (commit 49b49a3) — so defaults here are
    always disclosed, never hidden.
    """
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")

    spec = PARAMS[mnemonic]
    known = {s["id"] for s in spec}
    unknown = sorted(set(args) - known)
    if unknown:
        raise ToolError(
            f"unknown parameter(s) {', '.join(unknown)} for this model. "
            f"Valid parameters: {', '.join(sorted(known))}.")

    values: dict = {}
    defaulted: list[str] = []
    for s in spec:
        pid = s["id"]
        if pid not in args or args[pid] is None:
            values[pid] = s["def"]
            defaulted.append(pid)
            continue
        raw = args[pid]

        if s["kind"] == "enum":
            # bool choices arrive as real JSON booleans; everything else as a
            # string. Compare on identity of type to avoid Python's 1 == True.
            if raw not in s["choices"] or not isinstance(raw, type(s["choices"][0])):
                pretty = ", ".join(json.dumps(c) for c in s["choices"])
                raise ToolError(f"{pid} must be one of {pretty} (got {json.dumps(raw)})")
            values[pid] = raw
            continue

        # bool is a subclass of int in Python; accepting it here would let
        # true silently become 1.0 for a numeric parameter.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ToolError(f"{pid} must be a number (got {json.dumps(raw)})")
        num = float(raw)
        if not math.isfinite(num):
            raise ToolError(f"{pid} must be a finite number (got {raw})")
        if num < s["min"] or num > s["max"]:
            raise ToolError(
                f"{pid} must be between {s['min']} and {s['max']} (got {_fmt(num)}). "
                + ("This parameter is a decimal fraction, not a percentage — "
                   "use 0.08 for 8%." if s.get("pct") and num > 1 else ""))
        if s["kind"] == "int":
            if abs(num - round(num)) > 1e-9:
                raise ToolError(f"{pid} must be a whole number (got {_fmt(num)})")
            values[pid] = int(round(num))
        else:
            values[pid] = num

    return values, defaulted


def _fmt(x: float) -> str:
    return f"{int(x)}" if float(x).is_integer() else f"{x:g}"


# --------------------------------------------------------------------------- #
# Entitlement — bearer token only. See the module docstring for why the
# session cookie is deliberately ignored on this endpoint.
# --------------------------------------------------------------------------- #
def _redis_get(key: str) -> str | None:
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    req = urllib.request.Request(f"{REDIS_URL.rstrip('/')}/get/{key}",
                                 headers={"Authorization": f"Bearer {REDIS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


def _effective_plan(email: str) -> str:
    """Mirrors api/premium.py's _effective_plan() and api/_lib/billing.js's
    effectivePlan(): an expired or missing subscription falls back to free."""
    raw = _redis_get(f"sub:{email}")
    if not raw:
        return "free"
    try:
        sub = json.loads(raw)
    except (ValueError, TypeError):
        return "free"
    if sub.get("plan") not in ("pro", "unlimited") or not sub.get("expiresAt"):
        return "free"
    try:
        expires = datetime.fromisoformat(str(sub["expiresAt"]).replace("Z", "+00:00"))
    except ValueError:
        return "free"
    return "free" if expires <= datetime.now(timezone.utc) else sub["plan"]


UPGRADE_HINT = (
    "This model requires an ANALYST PRO (or higher) plan. Pass your session "
    "token as an Authorization: Bearer <token> header on the MCP endpoint — in "
    f"Claude Code: claude mcp add --transport http finmodels {SITE}/api/mcp "
    '--header "Authorization: Bearer <token>". The other nine models need no '
    "credential at all."
)


def _check_entitlement(bearer: str | None) -> str | None:
    """None if the caller may run a premium model, else the reason they may not."""
    if not (REDIS_URL and REDIS_TOKEN):
        return "Premium models are unavailable: this deployment has no auth backend configured."
    if not bearer:
        return "Not signed in. " + UPGRADE_HINT
    sess = _redis_get(f"sess:{bearer}")
    if not sess:
        return "That session token is expired or invalid. Sign in again to get a new one."
    try:
        email = json.loads(sess)["email"]
    except (ValueError, KeyError, TypeError):
        return "That session token is not valid."
    if _effective_plan(email) not in ("pro", "unlimited"):
        return "Your account is on the FREE plan. " + UPGRADE_HINT
    return None


# --------------------------------------------------------------------------- #
# Rate limiting / spend cap — protects the two real costs a public,
# credential-free endpoint can incur: Vercel function-invocation time (nine
# of the eleven tools run real computation, and a couple of them — Monte
# Carlo, the binomial lattice — are not cheap) and Upstash Redis REST calls.
# Nothing upstream of this file limits call volume: the nine free tools need
# zero credential, so this endpoint can be hit by anyone who finds the URL.
#
# Same two-tier shape as api/_lib/net.js's withinLimitLayered() (already
# protecting api/geo.js, api/quotes.js, api/rates.js): a per-caller-IP
# window catches one abusive source, and an IP-agnostic global window bounds
# the *total* rate regardless of how many IPs a caller spreads across — see
# net.js's own comment on why X-Forwarded-For is trivial to rotate against
# anything but a trusted edge. A third, day-wide global window is this
# endpoint's actual spend cap: a caller that stays under both short-window
# limits but keeps calling for hours could still run up real cost, and this
# is what bounds that worst case to something predictable no matter the
# burst shape. All three fail OPEN (Redis unconfigured/unreachable ->
# unmetered, not 429) — same posture net.js documents: a transient outage
# turning every tool call into a false rate-limit rejection would be a worse
# failure than a temporarily-unmetered endpoint.
#
# Python can't `require()` net.js's module (separate runtime; see the module
# docstring on why api/premium.py's _redis_get/_effective_plan are
# duplicated here rather than shared) — this duplicates the same primitive
# api/premium.py's version of this comment block also duplicates.
# --------------------------------------------------------------------------- #
_RL_IP_MAX, _RL_IP_WINDOW_SEC = 30, 60           # one caller, one minute
_RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW_SEC = 300, 60  # every caller, one minute
_RL_DAILY_MAX, _RL_DAILY_WINDOW_SEC = 5000, 86_400  # every caller, one day — the spend cap


def _redis_pipeline(commands: list[list[str]]) -> list | None:
    """Run several Redis commands in one Upstash REST round trip. Returns the
    list of raw `result` values in order, or None if the store isn't
    configured or the call failed outright — callers fail open on None."""
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    try:
        req = urllib.request.Request(
            f"{REDIS_URL.rstrip('/')}/pipeline",
            data=json.dumps(commands).encode("utf-8"),
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            results = json.loads(resp.read().decode("utf-8"))
        return [r.get("result") for r in results]
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError,
            AttributeError, TypeError):
        return None


def _within_limits(counters: list[tuple[str, int, int]]) -> bool:
    """counters: (key, max_n, window_sec) triples. Every tier's INCR fires
    together in this same request — one pipelined round trip, plus a second
    smaller one for any first-hit EXPIREs — mirroring net.js's
    withinLimitLayered() extended from two tiers to three. A counter whose
    own command errored inside an otherwise-successful pipeline is treated
    as unknown (fails open for that tier alone), not as "over limit"."""
    results = _redis_pipeline([["INCR", key] for key, _, _ in counters])
    if results is None:
        return True
    expire_cmds = [["EXPIRE", key, str(window)]
                   for (key, _max, window), n in zip(counters, results)
                   if isinstance(n, int) and n == 1]
    if expire_cmds:
        _redis_pipeline(expire_cmds)
    return all((not isinstance(n, int)) or n <= max_n
               for (_key, max_n, _window), n in zip(counters, results))


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    """Mirrors api/_lib/net.js's clientIp(): trust X-Real-Ip (Vercel's edge
    sets this to the actual connecting client), else the LAST
    X-Forwarded-For entry (closest to the edge, hardest for a caller to
    spoof), else the raw socket address."""
    real = handler.headers.get("X-Real-Ip")
    if real:
        return real.strip()
    xf = handler.headers.get("X-Forwarded-For")
    if xf:
        parts = [p.strip() for p in xf.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return handler.client_address[0] if handler.client_address else "unknown"


def _rate_limited(handler: BaseHTTPRequestHandler) -> bool:
    ip = _client_ip(handler)
    return not _within_limits([
        (f"mcp:rl:ip:{ip}", _RL_IP_MAX, _RL_IP_WINDOW_SEC),
        ("mcp:rl:global", _RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW_SEC),
        ("mcp:rl:daily", _RL_DAILY_MAX, _RL_DAILY_WINDOW_SEC),
    ])


# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #
def _input_schema(mnemonic: str) -> dict:
    props: dict = {}
    for s in PARAMS[mnemonic]:
        if s["kind"] == "enum":
            node = {"enum": list(s["choices"]), "default": s["def"]}
            node["type"] = "boolean" if isinstance(s["choices"][0], bool) else "string"
        else:
            node = {"type": "integer" if s["kind"] == "int" else "number",
                    "minimum": s["min"], "maximum": s["max"], "default": s["def"]}
        node["description"] = s["desc"]
        props[s["id"]] = node
    # Nothing is "required": every parameter has a documented default, and the
    # response names which ones were used. Marking them required would make the
    # common "price this option" call fail rather than answer.
    return {"type": "object", "properties": props, "additionalProperties": False}


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "description": "Model mnemonic."},
        "headline": {"type": "string", "description": "The single most important number, preformatted."},
        "results": {"type": "object", "description": "Every value the model computed."},
        "inputs_used": {"type": "object", "description": "The exact parameter values the model ran on."},
        "defaulted_inputs": {"type": "array", "items": {"type": "string"},
                             "description": "Parameters the caller did not supply, which fell back to a default. Treat any result depending on these as an assumption, not a finding."},
        "explanation": {"type": "string", "description": "The model's own written explanation of the result."},
    },
    "required": ["model", "headline", "results", "inputs_used", "defaulted_inputs"],
}

_LIST_TOOL = {
    "name": "finmodels_list_models",
    "title": "List available models",
    "description": ("List every quantitative-finance model this server exposes, with its "
                    "mnemonic, formula, parameters and whether it needs a paid plan. Call "
                    "this first if you are unsure which model fits the question."),
    "inputSchema": {"type": "object", "additionalProperties": False},
}


def _tools() -> list[dict]:
    out = [_LIST_TOOL]
    for mnemonic, builder in BUILDERS.items():
        slug, title, formula, desc = MODEL_META[mnemonic]
        note = ("  REQUIRES A PAID PLAN: pass an Authorization: Bearer <session token> "
                "header." if mnemonic in PREMIUM else "")
        out.append({
            "name": f"finmodels_{slug}",
            "title": title,
            "description": (
                f"{desc}\n\nFormula: {formula}\nMnemonic: {mnemonic}. "
                "All rate/growth/volatility parameters are decimal fractions, not "
                "percentages (0.08 means 8%). Monetary parameters are in millions "
                "unless the description says otherwise. Every parameter is optional "
                "and falls back to a documented default; the result lists exactly "
                f"which defaults were used.{note}"),
            "inputSchema": _input_schema(mnemonic),
            "outputSchema": _OUTPUT_SCHEMA,
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
        })
    return out


INSTRUCTIONS = (
    "FINMODELS TERMINAL exposes eleven canonical quantitative-finance models as tools. "
    "They compute from the parameters you pass — they do NOT fetch live market data, "
    "so any price, growth rate or volatility must come from you or from the user. "
    "Every parameter is optional and has a documented default; each result reports "
    "which defaults it fell back on, and you should treat any conclusion that rests "
    "on a defaulted input as an assumption rather than a finding. Rates, growth rates "
    "and volatilities are decimal fractions (0.08 = 8%); monetary inputs are in "
    "millions unless stated otherwise. Nine models are open to anyone. Two "
    "(finmodels_hidden_debt, finmodels_reverse_dcf) require an ANALYST PRO plan and an "
    "Authorization: Bearer <session token> header. The Fama-French 3-factor model is "
    f"NOT available here — see finmodels_list_models — run it at {SITE}. "
    "This is a modelling tool, not investment advice."
)


# --------------------------------------------------------------------------- #
# Method handlers
# --------------------------------------------------------------------------- #
CAPABILITIES = {"tools": {"listChanged": False}}


def _call_tool(params: dict, bearer: str | None) -> dict:
    name = params.get("name")
    if name == _LIST_TOOL["name"]:
        listing = {
            "models": [
                {"mnemonic": m, "tool": f"finmodels_{MODEL_META[m][0]}",
                 "name": MODEL_META[m][1], "formula": MODEL_META[m][2],
                 "requires_paid_plan": m in PREMIUM,
                 "parameters": [s["id"] for s in PARAMS[m]]}
                for m in BUILDERS
            ],
            "unavailable": [{"mnemonic": k, "name": v[0], "reason": v[1]}
                            for k, v in OMITTED.items()],
        }
        return _ok(json.dumps(listing, indent=2), listing)

    mnemonic = TOOL_TO_MNEMONIC.get(name)
    if mnemonic is None:
        # An unknown tool is a protocol error, not a tool error — the model
        # cannot fix it by retrying with different arguments.
        raise JsonRpcError(ERR_INVALID_PARAMS, f"Unknown tool: {name}")

    if mnemonic in PREMIUM:
        denied = _check_entitlement(bearer)
        if denied:
            return _err(denied)

    try:
        values, defaulted = _validate(mnemonic, params.get("arguments") or {})
        model = BUILDERS[mnemonic](values)
        results = _clean(model.calculate())
        try:
            explanation = model.explain()
        except Exception as exc:                      # noqa: BLE001
            explanation = f"(explanation unavailable: {exc})"
    except ToolError as exc:
        return _err(str(exc))
    except Exception as exc:                          # noqa: BLE001
        # Model-raised ValidationError/ModelError land here. They describe a
        # bad input combination the caller can fix (terminal_growth above the
        # discount rate, say), so they are tool errors too.
        return _err(f"{type(exc).__name__}: {exc}")

    structured = {
        "model": mnemonic,
        "headline": _headline(mnemonic, results),
        "results": results,
        "inputs_used": _clean(values),
        "defaulted_inputs": defaulted,
        "explanation": explanation,
    }
    lines = [f"{MODEL_META[mnemonic][1]} ({mnemonic})",
             f"Headline: {structured['headline']}", "", "Results:"]
    lines += [f"  {k} = {_render(v)}" for k, v in results.items()]
    lines.append("")
    if defaulted:
        lines.append("Defaults used (NOT supplied by the caller — any conclusion "
                     "resting on these is an assumption): " + ", ".join(defaulted))
    else:
        lines.append("All parameters were supplied by the caller; no defaults used.")
    lines += ["", explanation]
    return _ok("\n".join(lines), structured)


#: Which single result key is THE answer for each model, and how to format it.
#: Taken from the two places that already made this call rather than picked
#: afresh: SCEN_HEADLINE in public/assets/terminal.js (the nine shared models)
#: and _HEADLINE_PICK in api/premium.py (HDEBT, RDCF). Picking differently
#: here would mean the terminal and an AI client disagree about what a model's
#: headline number even is. tests/test_mcp_schema.py asserts every key below
#: actually exists in that model's calculate() output.
_HEADLINE = {
    "DCF": ("price_per_share", "$"), "GG": ("price", "$"),
    "MPT": ("tangency_sharpe", "x"), "VAR": ("var", "$"),
    "CAPM": ("expected_return", "%"), "BSM": ("price", "$"),
    "CRR": ("price", "$"), "MC": ("price", "$"), "HES": ("price", "$"),
    "HDEBT": ("adjusted_net_debt", "$"), "RDCF": ("implied_fcf_cagr", "%"),
}


def _headline(mnemonic: str, results: dict) -> str:
    key, unit = _HEADLINE.get(mnemonic, (None, ""))
    value = results.get(key) if key else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if unit == "%":
            return f"{value * 100:.2f}%"
        if unit == "$":
            return f"${value:,.2f}"
        return f"{value:.4f}"
    # Deliberately NOT a "pick the first number" fallback. If a model renames
    # its result key, an arbitrary substitute would present e.g. a lease
    # liability as though it were the headline valuation — a plausible wrong
    # number, which is the worst outcome this endpoint can produce. Say so
    # instead; `results` below still carries every real value.
    return f"(no headline: expected key {key!r} is absent from this result)"


def _render(v) -> str:
    if isinstance(v, float):
        return f"{v:,.6g}"
    if isinstance(v, list):
        return "[" + ", ".join(_render(x) for x in v[:8]) + ("...]" if len(v) > 8 else "]")
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_render(x)}" for k, x in v.items()) + "}"
    return str(v)


def _ok(text: str, structured) -> dict:
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": structured, "isError": False}


def _err(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _dispatch(method: str, params: dict, bearer: str | None, modern: bool) -> dict:
    if method == "server/discover":
        return {"resultType": "complete", "supportedVersions": list(SUPPORTED_VERSIONS),
                "capabilities": CAPABILITIES, "instructions": INSTRUCTIONS,
                "_meta": {"io.modelcontextprotocol/serverInfo":
                          {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "initialize":
        # Legacy era. Echo a version the client asked for when we support it,
        # else our newest legacy version — a legacy client has no way to
        # fall forward, so handing it MODERN_VERSION would strand it.
        asked = (params or {}).get("protocolVersion")
        agreed = asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {"protocolVersion": agreed, "capabilities": CAPABILITIES,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}
    if method == "tools/list":
        result = {"tools": _tools()}
        if modern:
            result["resultType"] = "complete"
        return result
    if method == "tools/call":
        result = _call_tool(params or {}, bearer)
        if modern:
            result["resultType"] = "complete"
        return result
    if method in ("ping",):
        return {}
    raise JsonRpcError(ERR_METHOD_NOT_FOUND, f"Method not found: {method}")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
def _b64_decode_sentinel(value: str) -> str:
    """Undo the spec's `=?base64?...?=` header encoding used when a value
    cannot be represented as plain ASCII."""
    if value.startswith("=?base64?") and value.endswith("?="):
        import base64
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except Exception:                             # noqa: BLE001
            return value
    return value


class handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- responses -------------------------------------------------------- #
    def _cors(self) -> None:
        # Permissive by design: this endpoint holds no ambient credential (see
        # the module docstring), so there is nothing a hostile origin could
        # spend. Authorization must be sent explicitly, which means it is
        # never attached automatically by a browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, MCP-Protocol-Version, "
                         "Mcp-Method, Mcp-Name, Mcp-Session-Id, Last-Event-ID")
        self.send_header("Access-Control-Expose-Headers", "MCP-Protocol-Version")

    def _send(self, code: int, payload: bytes | None, ctype: str | None = None,
              extra_headers: dict | None = None) -> None:
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload or b"")))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _json(self, code: int, obj: dict, extra_headers: dict | None = None) -> None:
        # allow_nan=False: NaN/Infinity are not valid JSON and would break a
        # strict client's parser. _clean() should have removed them already;
        # this is the backstop that turns a silent corruption into an error.
        self._send(code, json.dumps(obj, allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers)

    def _rpc_error(self, http: int, rid, code: int, message: str, data=None,
                    extra_headers: dict | None = None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._json(http, {"jsonrpc": "2.0", "id": rid, "error": err}, extra_headers)

    # -- methods ---------------------------------------------------------- #
    def do_OPTIONS(self) -> None:                     # noqa: N802
        self._send(204, None)

    def do_GET(self) -> None:                         # noqa: N802
        # Revision 2026-07-28 removed the GET SSE stream; the spec says a
        # server that does not offer it answers 405.
        self._send(405, b"", "text/plain; charset=utf-8")

    def do_DELETE(self) -> None:                      # noqa: N802
        # Sessions were removed too, so there is nothing to terminate.
        self._send(405, b"", "text/plain; charset=utf-8")

    def do_POST(self) -> None:                        # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._rpc_error(400, None, ERR_PARSE, "Parse error: body is not valid JSON")
        if not isinstance(body, dict):
            # Batches were removed from MCP; a list here is a client bug.
            return self._rpc_error(400, None, ERR_INVALID_REQUEST,
                                   "Invalid request: expected a single JSON-RPC object")

        rid = body.get("id")
        method = body.get("method")
        # Deliberately not `body.get("params") or {}`: a falsy non-dict such as
        # [] or "" would pass that idiom and be silently rewritten to {}, so a
        # malformed request would be answered as though it were well-formed
        # instead of rejected. Only an absent/null params defaults.
        params = body.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._rpc_error(400, rid, ERR_INVALID_PARAMS,
                                   f"params must be an object (got {type(params).__name__})")
        if not isinstance(method, str):
            return self._rpc_error(400, rid, ERR_INVALID_REQUEST, "Invalid request: missing method")

        meta = params.get("_meta") or {}
        body_version = meta.get("io.modelcontextprotocol/protocolVersion")
        header_version = self.headers.get("MCP-Protocol-Version")
        modern = bool(body_version) or header_version == MODERN_VERSION

        if modern:
            bad = self._validate_modern_headers(body_version, header_version, method, params)
            if bad:
                code, message, data = bad
                return self._rpc_error(400, rid, code, message, data)

        version = body_version or header_version
        if version and version not in SUPPORTED_VERSIONS:
            return self._rpc_error(
                400, rid, ERR_UNSUPPORTED_VERSION, "Unsupported protocol version",
                {"supported": list(SUPPORTED_VERSIONS), "requested": version})

        # A notification (no id) gets 202 and no body, per the transport spec.
        if rid is None and method.startswith("notifications/"):
            return self._send(202, None)

        # Rate limit only the method that actually triggers computation —
        # tools/list, server/discover, initialize and ping are metadata, free
        # to answer, and shouldn't cost a legitimate client its budget just
        # for exploring what this server offers before it calls anything.
        if method == "tools/call" and _rate_limited(self):
            return self._rpc_error(
                429, rid, ERR_RATE_LIMITED,
                "Rate limit exceeded on this MCP endpoint's compute path (tools/call). "
                "This protects shared compute and Redis budget across every caller, "
                "not just this request — wait a moment and retry.",
                extra_headers={"Retry-After": "30"})

        auth = self.headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None

        try:
            result = _dispatch(method, params, bearer, modern)
        except JsonRpcError as exc:
            # The spec asks for 404 specifically on an unknown method, so a
            # client can tell a modern server from a legacy endpoint that
            # simply is not there.
            http = 404 if exc.code == ERR_METHOD_NOT_FOUND else 400
            return self._rpc_error(http, rid, exc.code, exc.message, exc.data)
        except Exception as exc:                      # noqa: BLE001
            return self._rpc_error(500, rid, ERR_INTERNAL, f"Internal error: {exc}")

        if rid is None:
            return self._send(202, None)
        self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    def _validate_modern_headers(self, body_version, header_version, method, params):
        """Modern-era header/body agreement check (spec: Server Validation).

        The point is not pedantry: intermediaries route and rate-limit on the
        headers while this function executes on the body, so a mismatch
        between them is a request-smuggling primitive. Returning
        HeaderMismatch on any disagreement keeps the two sources of truth from
        ever diverging.
        """
        if not header_version:
            return (ERR_HEADER_MISMATCH, "Missing required header: MCP-Protocol-Version", None)
        if body_version and header_version != body_version:
            return (ERR_HEADER_MISMATCH,
                    f"Header mismatch: MCP-Protocol-Version header {header_version!r} "
                    f"does not match body value {body_version!r}", None)
        mcp_method = self.headers.get("Mcp-Method")
        if not mcp_method:
            return (ERR_HEADER_MISMATCH, "Missing required header: Mcp-Method", None)
        if mcp_method != method:
            return (ERR_HEADER_MISMATCH,
                    f"Header mismatch: Mcp-Method header {mcp_method!r} does not "
                    f"match body method {method!r}", None)
        if method in ("tools/call", "resources/read", "prompts/get"):
            expected = params.get("name") or params.get("uri")
            got = self.headers.get("Mcp-Name")
            if not got:
                return (ERR_HEADER_MISMATCH, "Missing required header: Mcp-Name", None)
            if _b64_decode_sentinel(got) != expected:
                return (ERR_HEADER_MISMATCH,
                        f"Header mismatch: Mcp-Name header {got!r} does not match "
                        f"body value {expected!r}", None)
        return None

    def log_message(self, *args) -> None:             # noqa: D102
        return   # Vercel captures stdout; the default access log is noise here
