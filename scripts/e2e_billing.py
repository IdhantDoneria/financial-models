"""Browser E2E of the Razorpay billing layer (full stack, local, dev-fake).

Starts scripts/dev_auth_server.js (real api/*.js handlers; store in memory,
gateway in dev-fake mode), signs in through the real OTP flow, then drives
the PLAN tab: verifies the catalogue (PRO ₹299 · UNLIMITED ₹599-struck ₹499),
the FREE usage meter, and a complete purchase — order → signed payment →
server verify → plan chip flips to the paid tier.

    python scripts/e2e_billing.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8126
BASE = f"http://127.0.0.1:{PORT}/"


def main() -> int:
    srv = subprocess.Popen(["node", str(ROOT / "scripts" / "dev_auth_server.js"), str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "api/billing-config", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        failures: list[str] = []
        with sync_playwright() as pw:
            exe = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium")
            browser = pw.chromium.launch(
                headless=True, executable_path=exe if Path(exe).exists() else None)
            page = browser.new_page()
            # Block the pyodide boot (external CDN, minutes of work) — billing
            # UI needs only the DOM; stub the runtime before any page script.
            page.route("**/pyodide*", lambda r: r.abort())
            code_box: dict = {}
            page.on("response", lambda r: code_box.update(
                json.loads(r.text() or "{}")) if "auth-request-otp" in r.url else None)

            print("· signing in via the real OTP flow…")
            page.goto(BASE + "login.html", timeout=30_000)
            page.wait_for_selector("#f-otp", state="visible", timeout=15_000)
            page.fill("#otp-email", "buyer@example.com")
            page.click("#otp-send")
            page.wait_for_selector("#otp-step2", state="visible", timeout=20_000)
            page.fill("#otp-code", code_box.get("devCode", ""))
            page.fill("#otp-name", "Desk Buyer")
            page.click("#otp-verify")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            print("  in terminal (pyodide boot stubbed out)")

            # The founders promo gifts this fresh signup a free month — revoke
            # it through the admin desk so the PAID path is what's under test.
            page.evaluate(
                """async () => { await fetch('api/admin', {method:'POST',
                     headers:{'X-Admin-Key':'devadmin','Content-Type':'application/json'},
                     body: JSON.stringify({action:'revoke', email:'buyer@example.com'})}); }""")
            print("  founder gift revoked — buyer starts on FREE")

            # The boot screen never finishes without pyodide — drive the menu
            # machinery directly; it's independent of the Python runtime.
            page.wait_for_function("typeof openMenuTab === 'function'", timeout=15_000)
            page.evaluate("document.getElementById('boot').style.display='none'")

            print("· PLAN tab: catalogue + free meter…")
            page.evaluate("openMenuTab('plan')")
            page.wait_for_selector(".pcard.unlimited", timeout=15_000)
            body = page.inner_text("#menu-body")
            for needle, msg in [
                ("ANALYST PRO", "PRO card missing"),
                ("₹299", "PRO price missing"),
                ("DESK UNLIMITED", "UNLIMITED card missing"),
                ("₹499", "UNLIMITED offer price missing"),
                ("SAVE ₹100", "psychological-anchor badge missing"),
                ("0 / 5", "FREE meter not at 0/5"),
                ("BEST VALUE", "best-value flag missing"),
            ]:
                if needle not in body:
                    failures.append(f"PLAN: {msg}")
            struck = page.locator(".pcard.unlimited .pprice s").inner_text()
            print(f"  cards ok · UNLIMITED shows struck {struck} -> ₹499")
            if struck != "₹599":
                failures.append(f"PLAN: struck MRP wrong ({struck})")

            print("· buying ANALYST PRO through the dev-fake gateway…")
            page.click('.pbuy[data-plan="pro"]')
            page.wait_for_function(
                "() => document.querySelector('#pmsg') && "
                "/ACTIVE — VALID UNTIL/.test(document.querySelector('#pmsg').textContent)",
                timeout=30_000)
            print(f"  {page.inner_text('#pmsg')[:80]}")
            page.wait_for_function(
                "() => /ANALYST PRO/.test(document.querySelector('#planchip').textContent)",
                timeout=15_000)
            chip = page.inner_text("#planchip")
            print(f"  status chip: {chip}")
            if "0/50" not in chip.replace(" ", ""):
                failures.append(f"BUY: chip not showing PRO 0/50 ({chip})")
            if "CURRENT PLAN" not in page.inner_text(".pcard.pro"):
                failures.append("BUY: PRO card not marked CURRENT PLAN")

            # server agrees: entitlement is PRO 50/mo
            us = page.evaluate(
                """async () => { const s = JSON.parse(localStorage.getItem('finmodels.session'));
                     const r = await fetch('api/usage', {headers:{Authorization:'Bearer '+s.token}});
                     return await r.json(); }""")
            print(f"  server entitlement: {us['planName']} · {us['used']}/{us['limit']}")
            if not (us["plan"] == "pro" and us["limit"] == 50):
                failures.append(f"BUY: server entitlement wrong ({us})")

            browser.close()
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(" -", f)
            return 1
        print("\nBILLING E2E: ALL CHECKS PASS")
        return 0
    finally:
        srv.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
