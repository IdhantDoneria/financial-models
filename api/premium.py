"""POST /api/premium — server-side computation for the two paywalled
premium models (Ind AS 116 Hidden-Debt Normalizer, Reverse DCF).

Every other model in this product runs client-side in Pyodide/WASM by
design (see api/_lib/billing.js's comment on that tradeoff) — but that
meant the ONLY thing standing between a free account and these two paid
ones was a client-side JS check in terminal.js, trivially bypassed from
devtools (confirmed by an authorized local pentest of this app). This
endpoint moves just those two models' actual computation here, so a free
account genuinely cannot obtain a premium result no matter what the client
sends — authorization is re-derived from the session cookie against Redis
directly (the same store api/usage.js already reads), never trusted from
the request body.

Two request shapes route to two different code paths, both using the
model classes straight from src/ (imported unchanged — same logic the
browser's WASM build runs) with no other module in between:

  - IB desk (extracted PDF financials + auto/manual assumptions):
    { model, extracted: {...ExtractedFinancials fields}, mode, overrides,
      live_rf, rf_source, erp } -> builds an AssumptionSet with the real
    AutoAssumer/ManualAssumer (src/pipeline/assumptions.py) and runs the
    one requested model directly.

  - Mnemonic terminal (raw model params, no filing/assumptions involved):
    { model, params: {...raw slider values} } -> builds the model's
    constructor kwargs directly (mirrors public/py/web_bridge.py's
    _build_hdebt/_build_rdcf, which the browser's WASM build calls — kept
    in sync by hand since duplicating a ~15-line dict literal is lower-risk
    here than depending on that file: see below) and calls .calculate().

Neither path imports AnalysisRunner or public/py/web_bridge.py's run loop:
  - The extracted path does import AnalysisReport from src/pipeline/runner.py,
    for its headline and status rules only, so the paid rows are labelled by the
    same code as the free report. That import is safe here: runner.py imports
    pandas lazily (inside summary_frame), and tests/pipeline/test_premium_parity.py
    imports it with pandas blocked to keep it that way. requirements.txt excludes
    pandas entirely, which is what keeps this function under Vercel's 500MB Python
    bundle limit (two earlier preview builds failed on it).
  - web_bridge.py lives under public/py/, outside api/'s own file tree —
    Vercel's Python bundler doesn't reliably include files reached only via
    a runtime sys.path insert rather than a static import it can trace
    (confirmed by a third failed preview build: ModuleNotFoundError at
    runtime despite building successfully). The raw-params dict builders below
    are inlined instead.

Response: 200 { ok:true, model, headline, status, results, errors,
rationale } | 401/403/503 { ok:false, error } for auth/plan/config
failures.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

import numpy as np

from src import IndASHiddenDebtModel, ReverseDCFModel
from src.pipeline.assumptions import AutoAssumer, ManualAssumer, ManualOverrides
from src.pipeline.pdf_extractor import ExtractedFinancials, PDFExtractor
from src.pipeline.runner import AnalysisReport

PREMIUM_MODELS = {
    "HDEBT": "Ind AS 116 Hidden-Debt Normalizer",
    "RDCF": "Reverse DCF / Market-Implied Expectations",
}
PREMIUM_CLASSES = {
    "HDEBT": IndASHiddenDebtModel,
    "RDCF": ReverseDCFModel,
}
#: The raw-params (calculator) headline key per model. The extracted path does
#: not use this: it calls AnalysisReport._headline, which also knows the
#: revenue-mode Reverse DCF that a cash-burning company gets. It stays because
#: api/mcp.py's headline table is checked against it, and because a test pins it
#: to what AnalysisReport._headline actually picks.
_HEADLINE_PICK = {
    "HDEBT": ("adjusted_net_debt", "$"),
    "RDCF": ("implied_fcf_cagr", "%"),
}


def _clean(value):
    """numpy scalars/arrays and non-finite floats -> JSON-safe."""
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


def _headline(mnemonic: str, results: dict, currency_symbol: str = "$") -> str:
    return AnalysisReport._headline(PREMIUM_MODELS[mnemonic], results, currency_symbol)


REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")


def _redis_get(key: str) -> str | None:
    """GET one key via the Upstash Redis REST API — the same store
    api/_lib/store.js talks to, read directly since this function runs in a
    separate Python runtime with no access to the Node process's state.

    `key` is built from attacker-controlled input (the fm_sess cookie /
    Authorization bearer token feeds the `sess:` lookup below, before any
    auth check runs), so it is sent in the JSON command BODY via
    _redis_pipeline — the same POST-body transport store.js and this file's
    own rate limiter already use — and is never interpolated into the request
    URL. That removes the path-injection surface entirely (a crafted token
    cannot add path segments or a query string) with no dependence on how
    Upstash URL-decodes a key in the path."""
    results = _redis_pipeline([["GET", key]])
    return results[0] if results else None


def _invalid_input(exc: BaseException) -> dict:
    """A request the model itself rejected (a discount rate at or below terminal
    growth, a missing field): the caller's mistake, said plainly, as HTTP 422.
    The message is the model's own validation text, never a traceback. This used
    to come back as HTTP 200 "INTERNAL ERROR", which hid what to fix."""
    text = f"missing field {exc}" if isinstance(exc, KeyError) else str(exc)
    return {"ok": False, "_http": 422, "error": f"INVALID INPUT: {text[:200]}"}


_GENERIC_CALC_ERROR = "INTERNAL ERROR — CALCULATION FAILED, THIS HAS BEEN LOGGED"


def _log_server_error(context: str, exc: BaseException) -> None:
    """Print the real exception server-side (stderr — captured in Vercel
    function logs) instead of returning it to the client. The three
    do_POST/_run_raw/_run_extracted catch-alls below used to echo
    f"{type(exc).__name__}: {exc}" straight into the JSON response; that's
    an information-disclosure leak (stack-trace-adjacent detail handed to
    whoever can reach this endpoint), so callers of this helper substitute
    _GENERIC_CALC_ERROR for the client-visible message instead."""
    print(f"[api/premium] {context}: {type(exc).__name__}: {exc}", file=sys.stderr)


def _parse_cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None


def _effective_plan(email: str) -> str:
    """Mirrors api/_lib/billing.js's effectivePlan(): an expired or missing
    subscription record falls back to "free". Never trust anything the
    client claims about its own plan — this is the one source of truth."""
    raw = _redis_get(f"sub:{email}")
    if not raw:
        return "free"
    try:
        sub = json.loads(raw)
    except (ValueError, TypeError):
        return "free"
    plan = sub.get("plan")
    expires_at = sub.get("expiresAt")
    if plan not in ("pro", "unlimited", "boutique", "enterprise") or not expires_at:
        return "free"
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return "free"
    if expires <= datetime.now(timezone.utc):
        return "free"
    return plan


#: Operator switch written by the admin desk (api/_lib/billing.js proOpen()).
#: While checkout is offline nobody can buy Pro, so the two models are open to
#: every signed-in account unless the operator has locked them ("0"). Once the
#: Razorpay keys exist the plan alone decides again.
_PRO_OPEN_KEY = "flag:pro_open"
_PAID_PLANS = ("pro", "unlimited", "boutique", "enterprise")


def _billing_live() -> bool:
    return bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"))


def _premium_access(email: str) -> str | None:
    """"paid" for an account on a paying (or admin-granted) plan, "open" for any
    other account while the operator switch is open, else None."""
    if _effective_plan(email) in _PAID_PLANS:
        return "paid"
    if not _billing_live() and _redis_get(_PRO_OPEN_KEY) != "0":
        return "open"
    return None


# --------------------------------------------------------------------------- #
# Rate limiting / spend cap — every caller here is already signed in (and, once
# billing is live, on a paying plan; see do_POST below), so this isn't defending against anonymous
# abuse the way api/mcp.py's version has to. It's defending against one
# compromised or careless account running up real Vercel/Redis cost: the
# monthly upload quota in api/_lib/billing.js only meters the IB-desk PDF
# path (_run_extracted); the raw slider path (_run_raw) that also reaches
# these same two models has never had any cap of its own.
#
# Same two-tier + daily-cap shape as api/mcp.py's version of this comment
# block, itself mirroring api/_lib/net.js's withinLimitLayered() (already
# protecting api/geo.js, api/quotes.js, api/rates.js). Limits are sized
# looser than mcp.py's, on purpose: this endpoint's whole audience is paying
# customers, and the cost of wrongly throttling one of them is worse than
# under-throttling here. Fails OPEN on a Redis outage — an unreachable store
# already 503s this whole endpoint a few lines up in do_POST, so failing
# open here just avoids a second, redundant way to break the same request.
# --------------------------------------------------------------------------- #
_RL_IP_MAX, _RL_IP_WINDOW_SEC = 40, 60             # one account/IP, one minute
_RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW_SEC = 200, 60    # every caller, one minute
_RL_DAILY_MAX, _RL_DAILY_WINDOW_SEC = 2000, 86_400  # every caller, one day — the spend cap


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
    """counters: (key, max_n, window_sec) triples, all incremented together
    in one pipelined round trip (plus a second, smaller one for any
    first-hit EXPIREs). A counter whose own command errored inside an
    otherwise-successful pipeline is treated as unknown (fails open for that
    tier alone), not as "over limit"."""
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


#: An account that reaches the Pro models only through the operator's open
#: switch (no paid or granted plan) gets its own daily allowance. Sign-up is
#: open, so without this a few throwaway accounts could spend the shared daily
#: budget above and lock every paying customer out.
_RL_OPEN_ACCT_MAX, _RL_OPEN_ACCT_WINDOW_SEC = 150, 86_400


def _rate_limited(handler: BaseHTTPRequestHandler, open_access_email: str | None = None) -> bool:
    ip = _client_ip(handler)
    counters = [
        (f"premium:rl:ip:{ip}", _RL_IP_MAX, _RL_IP_WINDOW_SEC),
        ("premium:rl:global", _RL_GLOBAL_MAX, _RL_GLOBAL_WINDOW_SEC),
        ("premium:rl:daily", _RL_DAILY_MAX, _RL_DAILY_WINDOW_SEC),
    ]
    if open_access_email:
        counters.insert(0, (f"premium:rl:acct:{open_access_email}",
                            _RL_OPEN_ACCT_MAX, _RL_OPEN_ACCT_WINDOW_SEC))
    return not _within_limits(counters)


def _reasons(model_name: str, assumptions, results: dict) -> dict:
    """The text shown under the row: why it is PARTIAL or UNASSESSED, plus a note
    when Reverse DCF's implied growth is one almost no company sustains."""
    parts = [assumptions.partial[model_name]] if model_name in assumptions.partial else []
    if model_name == PREMIUM_MODELS["RDCF"]:
        note = AnalysisReport.reverse_dcf_note(results)
        if note:
            parts.append(note)
    return {model_name: " ".join(parts)} if parts else {}


def _run_extracted(mnemonic: str, model_name: str, body: dict) -> dict:
    """IB desk path: build an ExtractedFinancials from the client's own
    earlier PDF extraction, run it through the real auto/manual assumption
    pipeline, then instantiate + calculate() just this one model directly
    (deliberately not AnalysisRunner — see the module docstring: that pulls
    in pandas, which this function's dependency budget can't afford)."""
    try:
        fields = body.get("extracted") or {}
        allowed = set(ExtractedFinancials.__dataclass_fields__)
        kwargs = {k: v for k, v in fields.items() if k in allowed}
        if not kwargs.get("free_cash_flows"):
            kwargs["free_cash_flows"] = []
        data = ExtractedFinancials(**kwargs)

        auto_kwargs = {}
        if body.get("live_rf") is not None:
            auto_kwargs["risk_free_rate"] = float(body["live_rf"])
        if body.get("erp") is not None:
            auto_kwargs["equity_risk_premium"] = float(body["erp"])
        # The selected market's long-run growth cap, as the free report applies
        # it (public/py/web_bridge.py run_report); without it every market got
        # the default 2.5% terminal growth.
        if body.get("lt_growth") is not None:
            auto_kwargs["terminal_growth_cap"] = float(body["lt_growth"])
        auto = AutoAssumer(**auto_kwargs)

        if body.get("mode") == "manual":
            allowed_o = set(ManualOverrides.__dataclass_fields__)
            raw_overrides = {k: v for k, v in (body.get("overrides") or {}).items()
                             if k in allowed_o and v is not None}
            if "lease_term_years" in raw_overrides:
                raw_overrides["lease_term_years"] = int(raw_overrides["lease_term_years"])
            assumptions = ManualAssumer(auto).build(data, ManualOverrides(**raw_overrides))
        else:
            assumptions = auto.build(data)

        unavailable_reason = assumptions.unavailable.get(model_name)
        if unavailable_reason is not None:
            return {"ok": True, "model": model_name, "headline": "-",
                    "status": "INSUFFICIENT DATA", "results": None,
                    "errors": unavailable_reason, "rationale": {}}

        model_kwargs = dict(assumptions.kwargs_by_model.get(model_name, {}))
        model = PREMIUM_CLASSES[mnemonic](**model_kwargs)
        results = _clean(model.calculate())
    except (ValueError, KeyError) as exc:
        return _invalid_input(exc)
    except Exception as exc:
        _log_server_error("_run_extracted", exc)
        return {"ok": True, "model": model_name, "headline": "-",
                "status": _GENERIC_CALC_ERROR, "results": None,
                "errors": _GENERIC_CALC_ERROR, "rationale": {}}

    rationale = {f"{m} · {p}": text for (m, p), text in assumptions.rationale.items()
                 if m == mnemonic}
    currency_symbol = PDFExtractor.currency_prefix(data.currency)
    partial = model_name in assumptions.partial
    return {
        "ok": True, "model": model_name,
        "headline": AnalysisReport._headline(model_name, results, currency_symbol,
                                             partial=partial, price=data.current_price),
        # Same OK / PARTIAL / UNASSESSED rule as the free report's summary, and
        # the same reason text shown under the row.
        "status": AnalysisReport.status_of(model_name, assumptions),
        "status_reasons": _reasons(model_name, assumptions, results),
        "results": results, "errors": None, "rationale": rationale,
    }


#: Mirrors public/py/web_bridge.py's _build_hdebt/_build_rdcf — the raw
#: slider-param -> constructor-kwarg mapping for the mnemonic terminal.
#: Keep in sync by hand if either model's constructor signature changes
#: (see the module docstring for why this isn't imported from there).
def _hdebt_kwargs(p: dict) -> dict:
    return dict(
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


def _rdcf_kwargs(p: dict) -> dict:
    return dict(
        current_price=p["current_price"], shares_outstanding=p["shares_outstanding"],
        net_debt=p["net_debt"], base_fcf=p["base_fcf"], base_revenue=p["base_revenue"],
        total_addressable_market=p["total_addressable_market"],
        years=int(p["years"]), discount_rate=p["discount_rate"],
        terminal_growth=p["terminal_growth"],
    )


_RAW_KWARGS_BUILDER = {"HDEBT": _hdebt_kwargs, "RDCF": _rdcf_kwargs}


def _run_raw(mnemonic: str, model_name: str, body: dict) -> dict:
    """Mnemonic-terminal path: raw slider params straight into the model,
    no filing/assumption step."""
    params = body.get("params") or {}
    try:
        kwargs = _RAW_KWARGS_BUILDER[mnemonic](params)
        model = PREMIUM_CLASSES[mnemonic](**kwargs)
        t0 = time.perf_counter()
        results = _clean(model.calculate())
        calc_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        explain = model.explain()
    except (ValueError, KeyError) as exc:
        return _invalid_input(exc)
    except Exception as exc:
        _log_server_error("_run_raw", exc)
        return {"ok": True, "model": model_name, "headline": "-",
                "status": _GENERIC_CALC_ERROR, "results": None,
                "errors": _GENERIC_CALC_ERROR, "rationale": {}}

    # Chart rendering is a nice-to-have, not needed for the numbers — and
    # plotly isn't part of this function's dependency budget (see the
    # module docstring). Degrade the same way web_bridge.run_model() does
    # client-side when a chart fails: numbers still return, figure is null.
    figure = None
    try:
        import plotly  # noqa: F401 - presence check only; not in requirements.txt

        figure = model.visualize().to_json()
    except Exception:
        pass

    return {
        "ok": True, "model": model_name, "headline": _headline(mnemonic, results),
        "status": "OK", "results": results, "figure": figure, "explain": explain,
        "calc_ms": calc_ms, "errors": None, "rationale": {},
    }


class handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict, extra_headers: dict | None = None) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "invalid JSON"})

        if not (REDIS_URL and REDIS_TOKEN):
            return self._json(503, {"ok": False, "error": "SERVER AUTH NOT CONFIGURED"})

        token = _parse_cookie(self.headers.get("Cookie"), "fm_sess")
        if not token:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            return self._json(401, {"ok": False, "error": "SIGN IN TO USE THIS MODEL"})

        sess_raw = _redis_get(f"sess:{token}")
        if not sess_raw:
            return self._json(401, {"ok": False, "error": "SESSION EXPIRED — SIGN IN AGAIN"})
        try:
            email = json.loads(sess_raw)["email"]
        except (ValueError, KeyError, TypeError):
            return self._json(401, {"ok": False, "error": "SESSION INVALID"})

        access = _premium_access(email)
        if access is None:
            return self._json(403, {"ok": False, "error":
                "This tool requires ANALYST PRO or higher — upgrade to unlock the "
                "Ind AS hidden-debt normalizer and reverse-DCF solver."})

        mnemonic = str(body.get("model") or "")
        model_name = PREMIUM_MODELS.get(mnemonic)
        if not model_name:
            return self._json(400, {"ok": False, "error": f"unknown premium model {mnemonic!r}"})

        # Checked here, after the (cheap) plan/model-name validation above and
        # right before the actual compute call — a request rejected for a bad
        # model name shouldn't spend rate budget it was never going to use.
        if _rate_limited(self, email if access == "open" else None):
            return self._json(429, {"ok": False, "error":
                "RATE LIMIT EXCEEDED — this protects shared compute and Redis "
                "budget across every caller, not just this request. Wait a "
                "moment and retry."}, extra_headers={"Retry-After": "30"})

        try:
            if "params" in body:
                result = _run_raw(mnemonic, model_name, body)
            else:
                result = _run_extracted(mnemonic, model_name, body)
        except Exception as exc:  # a bad/malformed request shouldn't 500
            _log_server_error("do_POST", exc)
            result = {"ok": True, "model": model_name, "headline": "-",
                      "status": _GENERIC_CALC_ERROR,
                      "results": None, "errors": _GENERIC_CALC_ERROR, "rationale": {}}

        return self._json(result.pop("_http", 200), result)

    def _not_allowed(self) -> None:
        self._json(405, {"ok": False, "error": "POST only"}, extra_headers={"Allow": "POST"})

    #: Anything but POST used to get http.server's HTML 501 page.
    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed  # noqa: N815
