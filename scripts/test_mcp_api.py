#!/usr/bin/env python3
"""Adversarial conformance suite for api/mcp.py (the MCP endpoint).

Run:  python3 scripts/test_mcp_api.py

Starts the real handler class from api/mcp.py on a free port (the same
BaseHTTPRequestHandler interface Vercel's Python runtime invokes, so this
exercises the deployed code path rather than a stand-in), drives it over real
HTTP with nothing but the standard library, then shuts it down.

Three groups of checks, in ascending order of how much they actually matter:

  1. Transport and protocol conformance — status codes, JSON-RPC error codes,
     header/body agreement. Cheap to get wrong, cheap to verify.

  2. Numerical agreement — every model called over MCP is compared against the
     same model imported straight from src/ and called in-process. This is the
     check that proves the MCP layer is plumbing, not a second implementation
     that can drift away from the tested one.

  3. The disclosure guarantee — a tool result must always name the parameters
     the caller did NOT supply. The failure mode this endpoint has to avoid is
     not a crash, it is a plausible valuation quietly built on defaults the
     caller never chose and never saw. This project has shipped that bug once
     already (commit 49b49a3), so it is tested harder than anything else here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        _failures.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
    return cond


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    code = (
        f"import sys, os\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        f"os.chdir({ROOT!r})\n"
        f"from http.server import ThreadingHTTPServer\n"
        f"from api.mcp import handler\n"
        f"ThreadingHTTPServer(('127.0.0.1', {port}), handler).serve_forever()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode()[-2000:]
            raise SystemExit(f"server died on startup:\n{err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return proc
        except OSError:
            time.sleep(0.15)
    proc.kill()
    raise SystemExit("server did not come up within 30s")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
BASE = ""


def raw(method: str, body=None, headers: dict | None = None):
    """Returns (status, parsed_json_or_None, raw_text)."""
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(BASE, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        status = e.code
    try:
        return status, json.loads(text) if text else None, text
    except ValueError:
        return status, None, text


def rpc(method: str, params: dict | None = None, *, rid=1, modern=True,
        headers: dict | None = None, version=MODERN, bearer: str | None = None,
        omit: tuple[str, ...] = (), override: dict | None = None):
    """Build and send a well-formed request, with hooks to deliberately break it."""
    params = dict(params or {})
    hdrs = dict(headers or {})
    if modern:
        params.setdefault("_meta", {})["io.modelcontextprotocol/protocolVersion"] = version
        hdrs.setdefault("MCP-Protocol-Version", version)
        hdrs.setdefault("Mcp-Method", method)
        if method in ("tools/call", "resources/read", "prompts/get"):
            name = params.get("name") or params.get("uri")
            if name:
                hdrs.setdefault("Mcp-Name", name)
    for h in omit:
        hdrs.pop(h, None)
    hdrs.update(override or {})
    if bearer:
        hdrs["Authorization"] = f"Bearer {bearer}"
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if rid is not None:
        body["id"] = rid
    return raw("POST", body, hdrs)


def call_tool(name: str, args: dict | None = None, **kw):
    return rpc("tools/call", {"name": name, "arguments": args or {}}, **kw)


def err_code(payload) -> object:
    try:
        return payload["error"]["code"]
    except (TypeError, KeyError):
        return None


# --------------------------------------------------------------------------- #
def main() -> int:
    global BASE
    port = free_port()
    BASE = f"http://127.0.0.1:{port}/api/mcp"
    proc = start_server(port)
    try:
        run_all()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{_passed} passed · {_failed} failed")
    if _failures:
        print("\nFailures:")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _failed else 0


def run_all() -> None:
    from api import mcp as M

    # ---------------------------------------------------------------- #
    section("Transport")
    st, _, _ = raw("GET")
    check("GET returns 405 (no GET stream in 2026-07-28)", st == 405, f"got {st}")
    st, _, _ = raw("DELETE")
    check("DELETE returns 405 (sessions removed)", st == 405, f"got {st}")

    req = urllib.request.Request(BASE, method="OPTIONS")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            st, cors = r.status, r.headers.get("Access-Control-Allow-Origin")
    except urllib.error.HTTPError as e:
        st, cors = e.code, e.headers.get("Access-Control-Allow-Origin")
    check("OPTIONS returns 204 with CORS", st == 204 and cors == "*", f"{st} / {cors}")

    st, p, _ = raw("POST", "{not json")
    check("malformed JSON -> -32700", err_code(p) == M.ERR_PARSE, f"{st} {err_code(p)}")

    st, p, _ = raw("POST", "[]")
    check("array body (batches removed) -> -32600",
          err_code(p) == M.ERR_INVALID_REQUEST, f"{st} {err_code(p)}")

    st, p, _ = raw("POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []})
    check("non-object params -> -32602", err_code(p) == M.ERR_INVALID_PARAMS, f"{err_code(p)}")

    st, p, _ = raw("POST", {"jsonrpc": "2.0", "id": 1})
    check("missing method -> -32600", err_code(p) == M.ERR_INVALID_REQUEST, f"{err_code(p)}")

    st, p, txt = rpc("notifications/progress", rid=None, modern=False)
    check("notification -> 202 with empty body", st == 202 and not txt.strip(), f"{st} {txt!r}")

    st, p, _ = rpc("nope/nope", modern=False)
    check("unknown method -> HTTP 404 + -32601",
          st == 404 and err_code(p) == M.ERR_METHOD_NOT_FOUND, f"{st} {err_code(p)}")

    # ---------------------------------------------------------------- #
    section("Header validation (modern era)")
    cases = [
        ("missing MCP-Protocol-Version", dict(omit=("MCP-Protocol-Version",))),
        ("MCP-Protocol-Version disagrees with body",
         dict(override={"MCP-Protocol-Version": "2025-06-18"})),
        ("missing Mcp-Method", dict(omit=("Mcp-Method",))),
        ("Mcp-Method disagrees with body", dict(override={"Mcp-Method": "tools/list"})),
    ]
    for label, kw in cases:
        st, p, _ = call_tool("finmodels_capm", **kw)
        check(f"{label} -> 400/-32020",
              st == 400 and err_code(p) == M.ERR_HEADER_MISMATCH, f"{st} {err_code(p)}")

    st, p, _ = call_tool("finmodels_capm", omit=("Mcp-Name",))
    check("missing Mcp-Name on tools/call -> 400/-32020",
          st == 400 and err_code(p) == M.ERR_HEADER_MISMATCH, f"{st} {err_code(p)}")

    st, p, _ = call_tool("finmodels_capm", override={"Mcp-Name": "finmodels_dcf"})
    check("Mcp-Name disagrees with params.name -> 400/-32020",
          st == 400 and err_code(p) == M.ERR_HEADER_MISMATCH, f"{st} {err_code(p)}")

    sentinel = "=?base64?" + b64encode(b"finmodels_capm").decode() + "?="
    st, p, _ = call_tool("finmodels_capm", override={"Mcp-Name": sentinel})
    check("Mcp-Name in base64 sentinel form is decoded and accepted",
          st == 200 and p and "result" in p, f"{st} {err_code(p)}")

    # ---------------------------------------------------------------- #
    section("Version negotiation")
    st, p, _ = rpc("tools/list", version="1900-01-01")
    ok = st == 400 and err_code(p) == M.ERR_UNSUPPORTED_VERSION
    sup = (p or {}).get("error", {}).get("data", {}).get("supported")
    check("bogus version -> 400/-32022 with supported list",
          ok and isinstance(sup, list) and MODERN in sup, f"{st} {err_code(p)} {sup}")

    for v in (MODERN, *LEGACY):
        st, p, _ = rpc("tools/list", version=v, modern=(v == MODERN))
        check(f"advertised version {v} is accepted", st == 200 and p and "result" in p,
              f"{st} {err_code(p)}")

    st, p, _ = rpc("server/discover")
    d = (p or {}).get("result") or {}
    check("server/discover returns supportedVersions + capabilities + instructions",
          isinstance(d.get("supportedVersions"), list) and "tools" in (d.get("capabilities") or {})
          and bool(d.get("instructions")), json.dumps(d)[:160])
    check("server/discover carries serverInfo in _meta",
          bool((d.get("_meta") or {}).get("io.modelcontextprotocol/serverInfo")))
    check("server/discover resultType is 'complete'", d.get("resultType") == "complete")

    st, p, _ = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}, modern=False)
    r = (p or {}).get("result") or {}
    check("legacy initialize works without modern headers",
          r.get("protocolVersion") == "2025-06-18" and "serverInfo" in r, json.dumps(r)[:160])

    # ---------------------------------------------------------------- #
    section("Tool listing")
    st, p, _ = rpc("tools/list")
    tools = ((p or {}).get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    check("tools/list returns 11 models + list_models = 12", len(tools) == 12, f"got {len(tools)}")
    check("tool names unique", len(set(names)) == len(names))
    import re
    check("tool names match the spec's character rules",
          all(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n) for n in names))
    check("every tool has a description", all(t.get("description") for t in tools))
    check("every inputSchema is a closed object schema",
          all(t["inputSchema"].get("type") == "object" for t in tools)
          and all(t["inputSchema"].get("additionalProperties") is False for t in tools))
    _, p2, _ = rpc("tools/list")
    check("tool order is deterministic across calls",
          [t["name"] for t in p2["result"]["tools"]] == names)

    st, p, _ = call_tool("finmodels_list_models")
    listing = p["result"]["structuredContent"]
    check("list_models names FF3 as unavailable with a reason",
          any(u["mnemonic"] == "FF3" and u["reason"] for u in listing["unavailable"]),
          json.dumps(listing.get("unavailable"))[:160])

    st, p, _ = call_tool("finmodels_nonexistent")
    check("unknown tool name is a JSON-RPC error, not a tool error",
          err_code(p) == M.ERR_INVALID_PARAMS, f"{st} {err_code(p)}")

    # ---------------------------------------------------------------- #
    section("Numerical agreement with src/ (no drift)")
    from src import (BlackScholesModel, CAPMModel, DiscountedCashFlowModel,
                     GordonGrowthModel, ValueAtRiskModel)
    import numpy as np

    def mcp_results(tool, args):
        _, pp, _ = call_tool(tool, args)
        res = pp["result"]
        assert res["isError"] is False, res["content"][0]["text"]
        return res["structuredContent"]["results"]

    a = dict(spot=100.0, strike=95.0, rate=0.04, sigma=0.25, maturity=2.0,
             dividend_yield=0.01, option_type="put")
    direct = BlackScholesModel(**a).calculate()
    got = mcp_results("finmodels_black_scholes", a)
    check("BSM price matches a direct src/ call to 1e-9",
          abs(got["price"] - float(direct["price"])) < 1e-9,
          f"{got['price']} vs {direct['price']}")

    a = dict(base_fcf=120.0, fcf_growth=0.06, years=7, discount_rate=0.11,
             terminal_growth=0.02, net_debt=300.0, shares_outstanding=200.0)
    fcfs = [a["base_fcf"] * (1 + a["fcf_growth"]) ** t for t in range(1, a["years"] + 1)]
    direct = DiscountedCashFlowModel(
        free_cash_flows=fcfs, discount_rate=a["discount_rate"],
        terminal_growth=a["terminal_growth"], net_debt=a["net_debt"],
        shares_outstanding=a["shares_outstanding"]).calculate()
    got = mcp_results("finmodels_dcf", a)
    check("DCF price_per_share matches a direct src/ call to 1e-9",
          abs(got["price_per_share"] - float(direct["price_per_share"])) < 1e-9,
          f"{got['price_per_share']} vs {direct['price_per_share']}")

    a = dict(risk_free_rate=0.03, expected_market_return=0.10, beta=1.4)
    direct = CAPMModel(**a).calculate()
    got = mcp_results("finmodels_capm", a)
    check("CAPM expected_return matches a direct src/ call to 1e-12",
          abs(got["expected_return"] - float(direct["expected_return"])) < 1e-12)

    a = dict(dividend=3.1, required_return=0.09, growth=0.035)
    direct = GordonGrowthModel(**a).calculate()
    got = mcp_results("finmodels_gordon_growth", a)
    check("Gordon Growth price matches a direct src/ call to 1e-9",
          abs(got["price"] - float(direct["price"])) < 1e-9)

    a = dict(mu_annual=0.05, sigma_annual=0.30, confidence=0.99, horizon_days=5,
             portfolio_value=250.0, method="historical")
    rng = np.random.default_rng(M._SEED)
    returns = rng.normal(a["mu_annual"] / 252, a["sigma_annual"] / (252 ** 0.5), size=10 * 252)
    direct = ValueAtRiskModel(returns=returns, confidence_level=a["confidence"],
                              horizon_days=a["horizon_days"],
                              portfolio_value=a["portfolio_value"],
                              method=a["method"]).calculate()
    got = mcp_results("finmodels_var_cvar", a)
    check("VaR matches a direct src/ call to 1e-9 (seeded sample reproduces)",
          abs(got["var"] - float(direct["var"])) < 1e-9, f"{got['var']} vs {direct['var']}")

    # ---------------------------------------------------------------- #
    section("Disclosure guarantee (defaults are never silent)")
    dcf_params = [s["id"] for s in M.PARAMS["DCF"]]
    _, p, _ = call_tool("finmodels_dcf", {})
    sc = p["result"]["structuredContent"]
    check("no arguments -> every parameter reported as defaulted",
          sorted(sc["defaulted_inputs"]) == sorted(dcf_params),
          f"{sc['defaulted_inputs']}")
    check("no-argument result still reports inputs_used in full",
          sorted(sc["inputs_used"]) == sorted(dcf_params))

    full = {s["id"]: s["def"] for s in M.PARAMS["DCF"]}
    _, p, _ = call_tool("finmodels_dcf", full)
    check("all arguments supplied -> defaulted_inputs is empty",
          p["result"]["structuredContent"]["defaulted_inputs"] == [],
          f"{p['result']['structuredContent']['defaulted_inputs']}")

    _, p, _ = call_tool("finmodels_dcf", {"base_fcf": 250.0})
    d = p["result"]["structuredContent"]["defaulted_inputs"]
    check("one argument supplied -> exactly that one absent from defaulted_inputs",
          "base_fcf" not in d and sorted(d) == sorted(set(dcf_params) - {"base_fcf"}), f"{d}")
    check("defaults are also called out in the human-readable text",
          "Defaults used" in p["result"]["content"][0]["text"])

    _, p, _ = call_tool("finmodels_dcf", full)
    check("no-defaults case says so explicitly in the text",
          "no defaults used" in p["result"]["content"][0]["text"].lower())

    # ---------------------------------------------------------------- #
    section("Input validation (tool errors, not protocol errors)")

    def tool_err(label, tool, args, must_mention):
        st_, pp, _ = call_tool(tool, args)
        res = (pp or {}).get("result")
        if res is None:
            return check(label, False, f"got JSON-RPC error {err_code(pp)}, expected isError")
        txt = res["content"][0]["text"]
        return check(label, res.get("isError") is True and must_mention in txt,
                     f"isError={res.get('isError')} text={txt[:110]!r}")

    tool_err("above maximum -> isError naming the bound",
             "finmodels_dcf", {"discount_rate": 9.0}, "between 0.04 and 0.2")
    tool_err("below minimum -> isError naming the bound",
             "finmodels_dcf", {"base_fcf": -5}, "between 1 and 500")
    tool_err("non-integer for an int param -> isError",
             "finmodels_dcf", {"years": 5.5}, "whole number")
    tool_err("string where a number belongs -> isError",
             "finmodels_dcf", {"base_fcf": "100"}, "must be a number")
    tool_err("bool where a number belongs -> isError (not coerced to 1.0)",
             "finmodels_dcf", {"base_fcf": True}, "must be a number")
    tool_err("unknown parameter -> isError listing valid ones",
             "finmodels_dcf", {"nonsense": 1}, "unknown parameter")
    tool_err("invalid enum -> isError showing the choices",
             "finmodels_black_scholes", {"option_type": "straddle"}, "must be one of")
    tool_err("percentage mistake gets an explicit decimal-fraction hint",
             "finmodels_capm", {"beta": 1.15, "risk_free_rate": 4.2}, "decimal fraction")
    tool_err("in-range but economically impossible combination is rejected by the model",
             "finmodels_dcf", {"discount_rate": 0.05, "terminal_growth": 0.05}, "")

    _, p, _ = call_tool("finmodels_dcf", {"base_fcf": None})
    sc = p["result"]["structuredContent"]
    check("explicit null is treated as 'not supplied' and disclosed",
          p["result"]["isError"] is False and "base_fcf" in sc["defaulted_inputs"])

    # ---------------------------------------------------------------- #
    section("Premium gating (paywall boundary)")
    for tool in ("finmodels_hidden_debt", "finmodels_reverse_dcf"):
        _, p, _ = call_tool(tool, {})
        res = p["result"]
        check(f"{tool} denied without a token", res.get("isError") is True)
        check(f"{tool} leaks no computed numbers when denied",
              "structuredContent" not in res, json.dumps(res)[:160])

        _, p, _ = call_tool(tool, {}, bearer="deadbeef" * 8)
        check(f"{tool} denied with a bogus token", p["result"].get("isError") is True)

    for tool in ("finmodels_black_scholes", "finmodels_dcf", "finmodels_capm",
                 "finmodels_gordon_growth", "finmodels_mpt", "finmodels_var_cvar",
                 "finmodels_binomial", "finmodels_monte_carlo", "finmodels_heston"):
        _, p, _ = call_tool(tool, {})
        check(f"{tool} works with no credential at all",
              p["result"].get("isError") is False, p["result"]["content"][0]["text"][:90])

    # Bypass attempts. Tool names are case-sensitive per the spec, so a cased
    # variant must not resolve to the premium tool.
    _, p, _ = call_tool("FINMODELS_HIDDEN_DEBT", {})
    check("uppercased premium tool name does not resolve", err_code(p) == M.ERR_INVALID_PARAMS)
    _, p, _ = call_tool("finmodels_hidden_debt", {"plan": "pro"})
    check("a 'plan' argument cannot grant entitlement", p["result"].get("isError") is True)
    _, p, _ = call_tool("finmodels_hidden_debt", {}, override={"X-Plan": "unlimited"})
    check("an invented plan header cannot grant entitlement",
          p["result"].get("isError") is True)
    _, p, _ = call_tool("finmodels_hidden_debt", {},
                        override={"Cookie": "fm_sess=forged-session-token"})
    check("a session COOKIE cannot grant entitlement (cookie is ignored by design)",
          p["result"].get("isError") is True)

    # ---------------------------------------------------------------- #
    section("JSON safety at every parameter boundary")
    bad_tokens = ("NaN", "Infinity", "-Infinity")
    swept = 0
    for mnemonic in M.BUILDERS:
        if mnemonic in M.PREMIUM:
            continue                      # gated; no numeric body to inspect
        tool = f"finmodels_{M.MODEL_META[mnemonic][0]}"
        for edge in ("min", "max"):
            args = {}
            for s in M.PARAMS[mnemonic]:
                if s["kind"] == "enum":
                    continue
                v = s[edge]
                args[s["id"]] = int(v) if s["kind"] == "int" else float(v)
            _, _, text = call_tool(tool, args)
            swept += 1
            if any(f'"{t}"' not in text and t in text for t in bad_tokens):
                check(f"{tool} @ all-{edge} emits no bare NaN/Infinity", False, text[:160])
                break
    else:
        check(f"no bare NaN/Infinity in any response across {swept} boundary sweeps", True)

    # Every response must also survive a STRICT parser (one that rejects the
    # non-standard constants Python's json accepts by default).
    ok_strict = True
    for mnemonic in M.BUILDERS:
        tool = f"finmodels_{M.MODEL_META[mnemonic][0]}"
        _, _, text = call_tool(tool, {})
        try:
            json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        except ValueError as exc:
            ok_strict = False
            check(f"{tool} response parses under a strict JSON parser", False, str(exc))
            break
    if ok_strict:
        check("every model's response parses under a strict JSON parser", True)


if __name__ == "__main__":
    raise SystemExit(main())
