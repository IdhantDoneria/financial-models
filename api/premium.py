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

Two request shapes route to two different code paths:

  - IB desk (extracted PDF financials + auto/manual assumptions):
    { model, extracted: {...ExtractedFinancials fields}, mode, overrides,
      live_rf, rf_source, erp } -> builds an AssumptionSet with the real
    AutoAssumer/ManualAssumer (src/pipeline/assumptions.py, imported
    unchanged — same logic the browser's WASM build uses) and runs the one
    requested model directly.

  - Mnemonic terminal (raw model params, no filing/assumptions involved):
    { model, params: {...raw slider values, same shape as BUILDERS[mn]
      expects} } -> reuses public/py/web_bridge.py's run_model() unchanged.

Deliberately does NOT reuse web_bridge.py's restore_extraction()+run_report()
for the IB desk path, and does NOT import AnalysisRunner: both pull in
pandas (AnalysisReport.summary_frame()), which alone pushed this function's
Vercel bundle over the 500MB Python limit (confirmed by two failed preview
builds) once combined with numpy+scipy. requirements.txt therefore excludes
pandas entirely — this file only ever touches the pandas-free half of
src/pipeline (ExtractedFinancials, AutoAssumer, ManualAssumer,
ManualOverrides) and the model classes' own .calculate(), replicating just
the few lines of src/pipeline/runner.py's dispatch/headline logic this
needs. See src/fama_french.py and src/pipeline/runner.py for the matching
lazy-pandas-import changes that make `from src import ...` and
`from src.pipeline.assumptions import ...` safe to do without pandas
installed at all.

Response: 200 { ok:true, model, headline, status, results, errors,
rationale } | 401/403/503 { ok:false, error } for auth/plan/config
failures.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "public" / "py")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np

from src import IndASHiddenDebtModel, ReverseDCFModel
from src.pipeline.assumptions import AutoAssumer, ManualAssumer, ManualOverrides
from src.pipeline.pdf_extractor import ExtractedFinancials

import web_bridge  # noqa: E402 - needs the sys.path setup above; mnemonic path only

PREMIUM_MODELS = {
    "HDEBT": "Ind AS 116 Hidden-Debt Normalizer",
    "RDCF": "Reverse DCF / Market-Implied Expectations",
}
PREMIUM_CLASSES = {
    "HDEBT": IndASHiddenDebtModel,
    "RDCF": ReverseDCFModel,
}
#: Mirrors AnalysisReport._headline() in src/pipeline/runner.py, for just
#: these two models (that method itself needs pandas-free replicating).
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


def _headline(mnemonic: str, results: dict) -> str:
    key, unit = _HEADLINE_PICK[mnemonic]
    value = results.get(key)
    if isinstance(value, (int, float)):
        if unit == "%":
            return f"{value * 100:.2f}%"
        if unit == "$":
            return f"${value:,.2f}"
        return f"{value:.4f}"
    return str(next(iter(results.values()), "-"))

REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")


def _redis_get(key: str) -> str | None:
    """GET one key via the Upstash Redis REST API — the same store
    api/_lib/store.js talks to, read directly since this function runs in a
    separate Python runtime with no access to the Node process's state."""
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    req = urllib.request.Request(
        f"{REDIS_URL.rstrip('/')}/get/{key}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("result")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


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
    if plan not in ("pro", "unlimited") or not expires_at:
        return "free"
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return "free"
    if expires <= datetime.now(timezone.utc):
        return "free"
    return plan


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

        model_kwargs = dict(assumptions.kwargs_by_model.get(model_name, {}))
        model = PREMIUM_CLASSES[mnemonic](**model_kwargs)
        results = _clean(model.calculate())
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        return {"ok": True, "model": model_name, "headline": "-", "status": err,
                "results": None, "errors": err, "rationale": {}}

    rationale = {f"{m} · {p}": text for (m, p), text in assumptions.rationale.items()
                 if m == mnemonic}
    return {
        "ok": True, "model": model_name,
        "headline": _headline(mnemonic, results), "status": "OK",
        "results": results, "errors": None, "rationale": rationale,
    }


def _run_raw(mnemonic: str, model_name: str, body: dict) -> dict:
    """Mnemonic-terminal path: raw slider params straight into the model,
    no filing/assumption step — same call web_bridge.run_model() makes
    client-side for every other model."""
    params = body.get("params") or {}
    out = json.loads(web_bridge.run_model(mnemonic, json.dumps(params)))
    if not out.get("ok"):
        err = out.get("error", "run failed")
        return {"ok": True, "model": model_name, "headline": "-", "status": err,
                "results": None, "errors": err, "rationale": {}}
    return {
        "ok": True, "model": model_name, "headline": "-", "status": "OK",
        "results": out.get("results"), "figure": out.get("figure"),
        "explain": out.get("explain"), "calc_ms": out.get("calc_ms"),
        "extras": out.get("extras"), "rationale": {},
    }


class handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
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

        plan = _effective_plan(email)
        if plan not in ("pro", "unlimited"):
            return self._json(403, {"ok": False, "error":
                "This tool requires ANALYST PRO or higher — upgrade to unlock the "
                "Ind AS hidden-debt normalizer and reverse-DCF solver."})

        mnemonic = str(body.get("model") or "")
        model_name = PREMIUM_MODELS.get(mnemonic)
        if not model_name:
            return self._json(400, {"ok": False, "error": f"unknown premium model {mnemonic!r}"})

        try:
            if "params" in body:
                result = _run_raw(mnemonic, model_name, body)
            else:
                result = _run_extracted(mnemonic, model_name, body)
        except Exception as exc:  # a bad/malformed request shouldn't 500
            result = {"ok": True, "model": model_name, "headline": "-",
                      "status": f"{type(exc).__name__}: {exc}",
                      "results": None, "errors": f"{type(exc).__name__}: {exc}", "rationale": {}}

        return self._json(200, result)
