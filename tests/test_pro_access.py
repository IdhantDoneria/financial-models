"""Who may run the two Pro models (Ind AS 116, Reverse DCF) while billing is offline.

Nobody can buy Pro until Razorpay is connected, so the models are open to every
signed-in account by default. The admin desk can lock them to granted accounts
(flag:pro_open = "0"), and the whole switch is ignored once billing is live.
api/premium.py and api/mcp.py each carry a copy of the gate, so both are run
through the same scenarios here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import api.mcp as mcp
import api.premium as premium

ROOT = Path(__file__).resolve().parents[1]
FUTURE = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _store(monkeypatch, *, flag=None, sub=None, billing_live=False):
    data = {"sess:tok": json.dumps({"email": "a@b.co"})}
    if flag is not None:
        data["flag:pro_open"] = flag
    if sub is not None:
        data["sub:a@b.co"] = json.dumps(sub)
    for mod in (premium, mcp):
        monkeypatch.setattr(mod, "_redis_get", lambda key, d=data: d.get(key))
    monkeypatch.setattr(mcp, "REDIS_URL", "https://redis.invalid")
    monkeypatch.setattr(mcp, "REDIS_TOKEN", "t")
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
        if billing_live:
            monkeypatch.setenv(var, "x")
        else:
            monkeypatch.delenv(var, raising=False)


def _allowed(module_name: str) -> bool:
    if module_name == "premium":
        return premium._premium_access("a@b.co") is not None
    return mcp._check_entitlement("tok") is None


GRANT = {"plan": "pro", "expiresAt": FUTURE}
EXPIRED = {"plan": "pro", "expiresAt": PAST}
SCENARIOS = [
    # (id, flag, sub, billing_live, expected)
    ("offline, default: any signed-in account", None, None, False, True),
    ("offline, switch open", "1", None, False, True),
    ("offline, admin locked: free account refused", "0", None, False, False),
    ("offline, admin locked: granted account still allowed", "0", GRANT, False, True),
    ("offline, admin locked: expired grant refused", "0", EXPIRED, False, False),
    ("billing live: the flag is ignored, free account refused", None, None, True, False),
    ("billing live, switch open: still refused", "1", None, True, False),
    ("billing live: paying account allowed", None, GRANT, True, True),
]


@pytest.mark.parametrize("module_name", ["premium", "mcp"])
@pytest.mark.parametrize("label,flag,sub,live,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_who_may_run_the_pro_models(monkeypatch, module_name, label, flag, sub, live, expected):
    _store(monkeypatch, flag=flag, sub=sub, billing_live=live)
    assert _allowed(module_name) is expected


def test_a_signed_out_caller_is_refused_even_when_the_models_are_open(monkeypatch):
    """Open means every SIGNED-IN account; the session is still required."""
    _store(monkeypatch)
    assert mcp._check_entitlement(None) is not None
    assert mcp._check_entitlement("no-such-token") is not None


def test_node_and_python_read_the_same_redis_key():
    js = (ROOT / "api" / "_lib" / "billing.js").read_text()
    assert re.search(r'PRO_OPEN_KEY = "flag:pro_open"', js)
    assert premium._PRO_OPEN_KEY == "flag:pro_open"
    assert '"flag:pro_open"' in (ROOT / "api" / "mcp.py").read_text()


def test_the_client_gate_leaves_the_decision_to_the_server_when_billing_is_offline():
    """A granted account looks FREE while billing is offline, so the browser must
    not block on its own plan: the server (which sees the grant) decides."""
    js = (ROOT / "public" / "assets" / "terminal.js").read_text()
    gate = js[js.index("async function premiumModelGate"):]
    gate = gate[:gate.index("\n}\n")]
    assert "if (!cfg || !cfg.billing) return { allowed: true };" in gate


def test_the_unenforced_banner_says_what_the_switch_is_set_to():
    js = (ROOT / "public" / "assets" / "terminal.js").read_text()
    start = js.index("MONTHLY LIMITS ARE NOT ENFORCED")
    end = js.index("</div>`);", start)
    banner = js[start:end]
    assert "cfg.proOpen === false" in js[start - 200:start]
    assert "open to every signed-in account" in banner and "reserved for accounts" in banner


def test_a_failed_ticker_load_puts_the_loaded_company_back_in_the_box():
    js = (ROOT / "public" / "assets" / "terminal.js").read_text()
    body = js[js.index("async function onIBTicker"):]
    body = body[:body.index("await new Promise((r) => setTimeout(r, 25));")]
    assert 'if (state.ib.extracted) $("#ibticker").value = state.ib.ticker' in body


def test_an_open_access_account_gets_its_own_daily_cap_and_a_paying_one_does_not(monkeypatch):
    """Sign-up is open, so accounts using the open switch must not be able to
    spend the shared daily budget that paying customers depend on."""
    seen = {}

    def fake_within(counters):
        seen["keys"] = [k for k, _, _ in counters]
        seen["max"] = {k: m for k, m, _ in counters}
        return True

    monkeypatch.setattr(premium, "_within_limits", fake_within)
    monkeypatch.setattr(premium, "_client_ip", lambda h: "1.2.3.4")
    assert premium._rate_limited(None, "a@b.co") is False
    assert "premium:rl:acct:a@b.co" in seen["keys"]
    assert seen["max"]["premium:rl:acct:a@b.co"] == premium._RL_OPEN_ACCT_MAX < premium._RL_DAILY_MAX
    assert premium._rate_limited(None, None) is False
    assert not any(k.startswith("premium:rl:acct:") for k in seen["keys"])


@pytest.mark.parametrize("label,flag,sub,live,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_access_kind_is_paid_open_or_none(monkeypatch, label, flag, sub, live, expected):
    _store(monkeypatch, flag=flag, sub=sub, billing_live=live)
    kind = premium._premium_access("a@b.co")
    assert (kind is not None) is expected
    if sub is GRANT:
        assert kind == "paid"
    elif expected:
        assert kind == "open"


# --------------------------------------------------------------------------- #
# The real handler, over HTTP, with a fake Redis
# --------------------------------------------------------------------------- #
def _serve(monkeypatch, data, counters=None):
    import threading
    import urllib.request
    from http.server import HTTPServer

    counters = {} if counters is None else counters
    monkeypatch.setattr(premium, "REDIS_URL", "https://redis.invalid")
    monkeypatch.setattr(premium, "REDIS_TOKEN", "t")
    monkeypatch.setattr(premium, "_redis_get", lambda key: data.get(key))

    def fake_pipeline(commands):
        out = []
        for cmd in commands:
            if cmd[0] == "INCR":
                counters[cmd[1]] = counters.get(cmd[1], 0) + 1
                out.append(counters[cmd[1]])
            else:
                out.append(1)
        return out

    monkeypatch.setattr(premium, "_redis_pipeline", fake_pipeline)
    server = HTTPServer(("127.0.0.1", 0), premium.handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(body, token="tok"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/premium", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **({"Cookie": f"fm_sess={token}"} if token else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    return server, post, counters


RDCF_BODY = {"model": "RDCF", "mode": "auto", "extracted": None}


def _fields():
    import tests.pipeline.test_premium_parity as parity
    return parity.FIELDS["AAPL"]


def test_over_http_an_open_account_is_served_then_locked_out_by_the_admin_switch(monkeypatch):
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
        monkeypatch.delenv(var, raising=False)
    data = {"sess:tok": json.dumps({"email": "free@x.co"})}
    server, post, _ = _serve(monkeypatch, data)
    try:
        body = dict(RDCF_BODY, extracted=_fields())
        code, out = post(body)
        assert code == 200 and out["ok"] and out["status"] in ("OK", "PARTIAL", "UNASSESSED"), out
        assert post(body, token=None)[0] == 401                       # no session: still refused
        data["flag:pro_open"] = "0"                                    # the admin locks it
        code, out = post(body)
        assert code == 403 and "ANALYST PRO" in out["error"]
        data["sub:free@x.co"] = json.dumps({"plan": "pro", "expiresAt": FUTURE})   # then a grant
        assert post(body)[0] == 200
    finally:
        server.shutdown()


def test_over_http_the_switch_is_ignored_once_billing_is_live(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "k")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "s")
    data = {"sess:tok": json.dumps({"email": "free@x.co"}), "flag:pro_open": "1"}
    server, post, _ = _serve(monkeypatch, data)
    try:
        assert post(dict(RDCF_BODY, extracted=_fields()))[0] == 403
    finally:
        server.shutdown()


def test_over_http_only_open_access_accounts_are_charged_to_the_per_account_cap(monkeypatch):
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
        monkeypatch.delenv(var, raising=False)
    data = {"sess:tok": json.dumps({"email": "free@x.co"}), "sess:pay": json.dumps({"email": "pay@x.co"}),
            "sub:pay@x.co": json.dumps({"plan": "pro", "expiresAt": FUTURE})}
    server, post, counters = _serve(monkeypatch, data)
    try:
        body = dict(RDCF_BODY, extracted=_fields())
        post(body)
        post(body, token="pay")
        assert counters.get("premium:rl:acct:free@x.co") == 1
        assert "premium:rl:acct:pay@x.co" not in counters
        counters["premium:rl:acct:free@x.co"] = premium._RL_OPEN_ACCT_MAX          # the allowance is spent
        code, out = post(body)
        assert code == 429
        assert post(body, token="pay")[0] == 200                                   # paying accounts unaffected
    finally:
        server.shutdown()


def test_the_plan_chip_and_tab_show_a_plan_the_operator_granted_while_billing_is_offline():
    js = (ROOT / "public" / "assets" / "terminal.js").read_text()
    chip = js[js.index("function syncPlanChip"):]
    chip = chip[:chip.index("\n}\n")]
    assert "usage.grant" in chip and "g.planName" in chip
    tab = js[js.index("async function renderPlanTab"):js.index("const canBuy")]
    assert "us.grant" in tab and "granted to this account by the" in tab
    usage = (ROOT / "api" / "usage.js").read_text()
    assert "base.grant = " in usage


def test_over_http_bad_input_is_a_422_with_the_reason_and_other_methods_get_json(monkeypatch):
    import urllib.request
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
        monkeypatch.delenv(var, raising=False)
    data = {"sess:tok": json.dumps({"email": "free@x.co"})}
    server, post, _ = _serve(monkeypatch, data)
    try:
        code, out = post(dict(RDCF_BODY, extracted=_fields(), mode="manual",
                              overrides={"discount_rate": 0.02, "terminal_growth": 0.05}))
        assert code == 422 and out["ok"] is False and out["error"].startswith("INVALID INPUT:"), out
        assert "Traceback" not in json.dumps(out)
        code, out = post({"model": "HDEBT", "params": {}})
        assert code == 422 and "missing field" in out["error"]
        code, out = post(dict(RDCF_BODY, extracted=_fields(), live_rf="abc"))
        assert code == 422
        for method in ("GET", "OPTIONS", "PUT"):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/premium", method=method)
            try:
                urllib.request.urlopen(req)
                raise AssertionError("expected 405")
            except urllib.error.HTTPError as e:
                assert e.code == 405 and e.headers["Allow"] == "POST"
                assert json.loads(e.read())["ok"] is False
    finally:
        server.shutdown()


def test_the_client_gates_follow_the_operator_switches_when_checkout_is_off():
    """Plans are enforced without a gateway: buy buttons become REQUEST ACCESS,
    a checkout can never start, and the Pro gate honours the open switch."""
    js = (ROOT / "public" / "assets" / "terminal.js").read_text()
    gate = js[js.index("async function premiumModelGate"):]
    gate = gate[:gate.index("\n}\n")]
    assert "!cfg.checkout && cfg.proOpen" in gate
    tab = js[js.index("async function renderPlanTab"):js.index("async function startCheckout")]
    assert "REQUEST ACCESS" in tab and "const canBuy = cfg && cfg.checkout && isOtp;" in tab
    start = js[js.index("async function startCheckout"):]
    assert "if (!cfg || !cfg.checkout || !isServerBacked(u)) return;" in start[:400]
    upload = js[js.index("async function uploadGate"):]
    upload = upload[:upload.index("\n}\n")]
    assert "cfg.tracking && isServerBacked(state.user)" in upload


def test_premium_runs_are_logged_in_the_same_shape_as_the_node_activity_log(monkeypatch):
    calls = []
    monkeypatch.setattr(premium, "_redis_pipeline", lambda cmds: calls.append(cmds) or [1] * len(cmds))
    premium._track("a@b.co", "RDCF", "OK")
    cmds = calls[0]
    ev = json.loads(cmds[0][2])
    assert cmds[0][:2] == ["LPUSH", "events:a@b.co"] and cmds[1] == ["LTRIM", "events:a@b.co", "0", "199"]
    assert cmds[2][:2] == ["LPUSH", "events:all"] and cmds[3] == ["LTRIM", "events:all", "0", "499"]
    assert ev["type"] == "premium" and ev["model"] == "RDCF" and ev["email"] == "a@b.co"
    js = (ROOT / "api" / "_lib" / "activity.js").read_text()
    assert "const PER_USER = 200;" in js and "const ALL = 500;" in js and "premium: [\"model\", \"status\"]" in js
    monkeypatch.setattr(premium, "_redis_pipeline", lambda cmds: (_ for _ in ()).throw(RuntimeError("down")))
    premium._track("a@b.co", "RDCF", "OK")          # never raises
