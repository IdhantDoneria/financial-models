#!/usr/bin/env python3
"""Adversarial test for the rate limiter / spend cap in api/mcp.py and
api/premium.py (see the "Rate limiting / spend cap" comment block in each
file for the design).

Run:  python3 scripts/test_rate_limit.py

WHY THIS IS A SEPARATE SCRIPT FROM scripts/test_mcp_api.py
------------------------------------------------------------
test_mcp_api.py runs with no Redis configured (REDIS_URL/REDIS_TOKEN unset),
which is the right choice for a conformance suite that shouldn't need a real
store to prove the protocol is correct — but it means every rate-limit check
in that file can only ever prove the FAIL-OPEN path. A limiter that always
fails open passes every one of those checks and also never blocks anyone,
which is a bug this repo doesn't have a test for anywhere else, so it gets
one here.

This starts a tiny in-process Upstash-REST-API-shaped emulator (real INCR
with real TTL-on-first-hit EXPIRE semantics, just backed by a dict instead of
a real Upstash cluster), points a real subprocess of api.mcp.handler and
api.premium.handler at it via the same KV_REST_API_URL/TOKEN env vars
Vercel's Upstash integration injects, and drives real HTTP bursts at both —
so this proves the limiter actually blocks, not just that it compiles.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
    return cond


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
# Fake Upstash REST backend — just GET / pipeline(INCR, EXPIRE), which is all
# _redis_get()/_redis_pipeline() in either handler ever calls.
# --------------------------------------------------------------------------- #
_store: dict[str, tuple[object, float | None]] = {}
_lock = threading.Lock()


def _expired(key: str) -> bool:
    v = _store.get(key)
    if v is None:
        return True
    _val, exp = v
    if exp is not None and time.time() > exp:
        del _store[key]
        return True
    return False


def _run_cmd(cmd: list[str]):
    op = cmd[0].upper()
    with _lock:
        if op == "INCR":
            key = cmd[1]
            if _expired(key):
                _store[key] = (1, None)
                return 1
            val, exp = _store[key]
            val = int(val) + 1
            _store[key] = (val, exp)
            return val
        if op == "EXPIRE":
            key, ttl = cmd[1], int(cmd[2])
            if key in _store:
                val, _exp = _store[key]
                _store[key] = (val, time.time() + ttl)
            return 1
        if op == "GET":
            key = cmd[1]
            return None if _expired(key) else str(_store[key][0])
        return None


class _FakeUpstashHandler(BaseHTTPRequestHandler):
    def _reply(self, obj) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:                        # noqa: N802
        parts = [p for p in self.path.split("/") if p]
        if parts and parts[0] == "get":
            return self._reply({"result": _run_cmd(["GET", "/".join(parts[1:])])})
        self._reply({"result": None})

    def do_POST(self) -> None:                       # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"[]"
        if self.path.rstrip("/").endswith("/pipeline"):
            commands = json.loads(raw)
            return self._reply([{"result": _run_cmd(c)} for c in commands])
        self._reply({"result": None})

    def log_message(self, *a) -> None:                # noqa: D102
        return


def start_fake_upstash() -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstashHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"


def reset_store() -> None:
    with _lock:
        _store.clear()


def seed(key: str, value) -> None:
    with _lock:
        _store[key] = (value, None)


# --------------------------------------------------------------------------- #
# Real handler subprocesses, pointed at the fake store
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(import_line: str, port: int, env: dict) -> subprocess.Popen:
    code = (
        f"import sys, os\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        f"os.chdir({ROOT!r})\n"
        f"from http.server import ThreadingHTTPServer\n"
        f"{import_line}\n"
        f"ThreadingHTTPServer(('127.0.0.1', {port}), handler).serve_forever()\n"
    )
    full_env = dict(os.environ)
    full_env.update(env)
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=full_env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode()[-3000:]
            raise SystemExit(f"server died on startup:\n{err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("server did not come up within 15s")


def post(base: str, body: dict, headers: dict | None = None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), dict(e.headers)
        except Exception:
            return e.code, None, {}


# --------------------------------------------------------------------------- #
def main() -> int:
    upstash_url = start_fake_upstash()
    env = {"KV_REST_API_URL": upstash_url, "KV_REST_API_TOKEN": "test-token"}

    mcp_port = free_port()
    mcp_proc = start_server("from api.mcp import handler", mcp_port, env)
    MCP_BASE = f"http://127.0.0.1:{mcp_port}"

    prem_port = free_port()
    prem_proc = start_server("from api.premium import handler", prem_port, env)
    PREM_BASE = f"http://127.0.0.1:{prem_port}"

    # The in-process global/daily-tier checks below call _rate_limited()
    # directly rather than over HTTP, so THIS process also needs
    # REDIS_URL/REDIS_TOKEN set before importing api.mcp — its module-level
    # globals are read once, at import time, the same way the subprocess's
    # copy of the module reads them from the env dict passed to Popen above.
    os.environ["KV_REST_API_URL"] = upstash_url
    os.environ["KV_REST_API_TOKEN"] = "test-token"
    import api.mcp as mcp_mod  # for the module-level tier constants, in-process checks

    try:
        section("api/mcp.py — per-IP burst tier (tools/call)")
        reset_store()

        def call_tool(ip="9.9.9.1"):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "finmodels_black_scholes",
                               "arguments": {"spot": 100, "strike": 95, "sigma": 0.25,
                                            "maturity": 1, "rate": 0.04},
                               "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
            h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                 "Mcp-Name": "finmodels_black_scholes", "X-Forwarded-For": ip}
            return post(MCP_BASE, body, h)

        statuses = [call_tool()[0] for _ in range(mcp_mod._RL_IP_MAX)]
        check(f"first {mcp_mod._RL_IP_MAX} calls from one IP all succeed",
              all(s == 200 for s in statuses), f"statuses={set(statuses)}")

        s, d, hdrs = call_tool()
        check("the next call from the same IP is 429 / ERR_RATE_LIMITED",
              s == 429 and d and d.get("error", {}).get("code") == mcp_mod.ERR_RATE_LIMITED,
              f"status={s} body={d}")
        check("429 response carries Retry-After", hdrs.get("Retry-After") == "30", f"{hdrs}")

        s2, _d2, _h2 = call_tool(ip="9.9.9.2")
        check("a different IP is unaffected by the first IP's limit", s2 == 200, f"status={s2}")

        section("api/mcp.py — non-compute methods are never rate limited")
        reset_store()
        exhausted_ip = "9.9.9.1"
        for _ in range(mcp_mod._RL_IP_MAX + 5):
            call_tool(ip=exhausted_ip)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
        h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list",
             "X-Forwarded-For": exhausted_ip}
        s, d, _ = post(MCP_BASE, body, h)
        check("tools/list from a tools/call-exhausted IP still succeeds",
              s == 200 and "tools" in (d or {}).get("result", {}), f"status={s}")

        section("api/mcp.py — global and daily tiers block independently of the IP tier")
        reset_store()
        seed("mcp:rl:global", mcp_mod._RL_GLOBAL_MAX)

        class _FakeHandler:
            headers: dict = {}
            client_address = ("1.2.3.4", 0)

        check("global tier at exactly its max blocks the next call, alone",
              mcp_mod._rate_limited(_FakeHandler()) is True)
        reset_store()
        seed("mcp:rl:global", mcp_mod._RL_GLOBAL_MAX - 2)
        check("global tier below its max does not block",
              mcp_mod._rate_limited(_FakeHandler()) is False)
        reset_store()
        seed("mcp:rl:daily", mcp_mod._RL_DAILY_MAX)
        check("daily tier at exactly its max blocks the next call, alone",
              mcp_mod._rate_limited(_FakeHandler()) is True)

        section("api/mcp.py — fails open when the store is unreachable")
        # REDIS_URL/REDIS_TOKEN are read once at import time into module-level
        # globals, not re-read per call — so simulating "unreachable" means
        # patching those globals directly, not the environment.
        real_url = mcp_mod.REDIS_URL
        mcp_mod.REDIS_URL = "http://127.0.0.1:1"  # nothing listens here
        try:
            blocked = mcp_mod._within_limits([("nowhere:x", 1, 60)])
        finally:
            mcp_mod.REDIS_URL = real_url
        check("_within_limits([...]) against an unreachable store returns True (fail open)",
              blocked is True, f"blocked={blocked}")

        section("api/premium.py — per-IP burst tier on the compute path")
        reset_store()
        seed("sess:test-token", json.dumps({"email": "rl-test@example.com"}))
        seed("sub:rl-test@example.com",
             json.dumps({"plan": "unlimited", "expiresAt": "2099-01-01T00:00:00Z"}))

        def call_premium(ip="9.9.9.9"):
            body = {"model": "HDEBT", "params": {
                "net_income": 100, "reported_net_debt": 50, "reported_equity_value": 500,
                "shares_outstanding": 10, "annual_lease_payment": 5, "lease_term_years": 5,
                "lease_discount_rate": 0.06, "reverse_factoring_exposure": 0,
                "cl1_amount": 0, "cl1_probability": 0, "cl2_amount": 0, "cl2_probability": 0,
                "depreciation_amortization": 10, "rd_capitalized_amortization": 0,
                "rd_cash_spend": 0, "maintenance_capex": 5,
            }}
            h = {"Authorization": "Bearer test-token", "X-Forwarded-For": ip}
            return post(PREM_BASE, body, h)

        import api.premium as prem_mod
        statuses = [call_premium()[0] for _ in range(prem_mod._RL_IP_MAX)]
        check(f"first {prem_mod._RL_IP_MAX} premium calls from one IP all succeed",
              all(s == 200 for s in statuses), f"statuses={set(statuses)}")

        s, d, hdrs = call_premium()
        check("the next premium call from the same IP is 429",
              s == 429 and d and "RATE LIMIT" in d.get("error", ""), f"status={s} body={d}")
        check("premium 429 also carries Retry-After", hdrs.get("Retry-After") == "30", f"{hdrs}")

    finally:
        mcp_proc.terminate()
        prem_proc.terminate()
        mcp_proc.wait(timeout=5)
        prem_proc.wait(timeout=5)

    print(f"\n{_passed} passed · {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
