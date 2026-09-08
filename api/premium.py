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

Reuses public/py/web_bridge.py UNCHANGED (the exact module Pyodide runs in
the browser) rather than re-implementing the model-building/assumption
logic here — a given filing/params produce identical numbers either way,
and there's one place to fix bugs, not two. Two request shapes route to
web_bridge's two entrypoints:

  - IB desk (extracted PDF financials + auto/manual assumptions):
    { model, extracted: {...ExtractedFinancials fields}, mode, overrides,
      live_rf, rf_source, erp, country, currency, currency_symbol,
      fx_per_usd } -> web_bridge.restore_extraction() + .run_report()

  - Mnemonic terminal (raw model params, no filing/assumptions involved):
    { model, params: {...raw slider values, same shape as BUILDERS[mn]
      expects} } -> web_bridge.run_model()

Response: 200 { ok:true, model, headline, status, results, errors,
rationale } | 401/403/503 { ok:false, error } for auth/plan/config
failures.
"""

from __future__ import annotations

import json
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

import web_bridge  # noqa: E402 - needs the sys.path setup above

PREMIUM_MODELS = {
    "HDEBT": "Ind AS 116 Hidden-Debt Normalizer",
    "RDCF": "Reverse DCF / Market-Implied Expectations",
}

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
    """IB desk path: rehydrate the client's own earlier PDF extraction into
    web_bridge's analyzer state, then run just this one model through the
    real auto/manual assumption pipeline."""
    extracted = body.get("extracted") or {}
    restored = json.loads(web_bridge.restore_extraction(
        json.dumps(extracted), body.get("period") or "annual"))
    if not restored.get("ok"):
        err = restored.get("error", "could not load the extracted filing")
        return {"ok": True, "model": model_name, "headline": "-", "status": err,
                "results": None, "errors": err, "rationale": {}}

    run_params = {
        "mode": body.get("mode"), "selected": [model_name],
        "live_rf": body.get("live_rf"), "rf_source": body.get("rf_source"),
        "erp": body.get("erp"), "country": body.get("country"),
        "currency": body.get("currency"), "currency_symbol": body.get("currency_symbol"),
        "fx_per_usd": body.get("fx_per_usd"), "overrides": body.get("overrides") or {},
    }
    out = json.loads(web_bridge.run_report(json.dumps(run_params)))
    if not out.get("ok"):
        err = out.get("error", "run failed")
        return {"ok": True, "model": model_name, "headline": "-", "status": err,
                "results": None, "errors": err, "rationale": {}}

    summary = out.get("summary") or []
    row = summary[0] if summary else {"Headline result": "-", "Status": "ERROR"}
    rationale = {k: v for k, v in (out.get("rationale") or {}).items()
                 if k.startswith(mnemonic + " ")}
    return {
        "ok": True, "model": model_name,
        "headline": row.get("Headline result", "-"), "status": row.get("Status", "OK"),
        "results": (out.get("results") or {}).get(model_name),
        "errors": (out.get("errors") or {}).get(model_name),
        "rationale": rationale,
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
