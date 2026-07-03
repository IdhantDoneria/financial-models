"""Browser E2E of the server-side OTP login (full stack, local).

Starts scripts/dev_auth_server.js (real api/*.js handlers, in-memory store,
email in echo mode), then drives the actual login page through the complete
passwordless flow: request code -> read the code from the API response (as
the emailed user would from their inbox) -> verify -> land in the terminal
with a server session -> validate via /api/auth-me -> sign out (revokes the
token) -> confirm the token is dead.

    python scripts/e2e_auth_otp.py
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
PORT = 8124
BASE = f"http://127.0.0.1:{PORT}/"


def main() -> int:
    srv = subprocess.Popen(["node", str(ROOT / "scripts" / "dev_auth_server.js"), str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):  # wait for the server
            try:
                urllib.request.urlopen(BASE + "api/auth-config", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        failures: list[str] = []
        with sync_playwright() as pw:
            exe = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium")
            browser = pw.chromium.launch(
                headless=True, executable_path=exe if Path(exe).exists() else None)
            page = browser.new_page()
            # No real inbox in the harness: capture the dev-echoed code from the
            # API response, exactly the digits the email would contain.
            code_box: dict = {}
            page.on("response", lambda r: code_box.update(
                json.loads(r.text() or "{}")) if "auth-request-otp" in r.url else None)

            print("· login page must be in SERVER AUTH (OTP) mode…")
            page.goto(BASE + "login.html", timeout=30_000)
            page.wait_for_selector("#f-otp", state="visible", timeout=15_000)
            tabs_hidden = page.locator(".atabs").is_hidden()
            print(f"  otp form shown · password tabs hidden={tabs_hidden}")
            if not tabs_hidden:
                failures.append("OTP: device-local tabs still visible in server mode")

            print("· request code -> verify -> terminal…")
            page.fill("#otp-email", "vp@example.com")
            page.click("#otp-send")
            page.wait_for_selector("#otp-step2", state="visible", timeout=20_000)
            code = code_box.get("devCode")
            print(f"  code delivered (echo): {code}")
            if not code:
                return _fail(failures + ["OTP: no code issued"])

            page.fill("#otp-code", "000000" if code != "000000" else "111111")
            page.click("#otp-verify")
            page.wait_for_selector("#auth-err.on", timeout=15_000)
            print(f"  wrong code rejected: {page.inner_text('#auth-err')!r}")

            page.fill("#otp-code", code)
            page.fill("#otp-name", "Priya Raghavan")
            page.click("#otp-verify")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            sess = json.loads(page.evaluate("localStorage.getItem('finmodels.session')"))
            print(f"  in terminal — provider={sess['provider']} token={'yes' if sess.get('token') else 'NO'}")
            if sess.get("provider") != "otp" or not sess.get("token"):
                failures.append("OTP: session missing provider/token")

            # server agrees the session is live and holds the profile
            me = json.loads(page.evaluate(
                """async (t) => { const r = await fetch('api/auth-me',
                     {headers:{Authorization:'Bearer '+t}}); return JSON.stringify(
                     {code:r.status, body: await r.json()}); }""", sess["token"]))
            print(f"  /api/auth-me -> {me['code']} · {me['body'].get('user', {})}")
            if me["code"] != 200 or me["body"]["user"]["name"] != "Priya Raghavan":
                failures.append(f"OTP: /me mismatch ({me})")

            # sign out must revoke the server token (page is mid-boot; the
            # status bar isn't interactive yet, so call the same path signOut uses)
            page.evaluate(
                """async (t) => { await fetch('api/auth-logout',
                     {method:'POST', headers:{Authorization:'Bearer '+t}});
                   localStorage.removeItem('finmodels.session'); }""", sess["token"])
            dead = page.evaluate(
                """async (t) => (await fetch('api/auth-me',
                     {headers:{Authorization:'Bearer '+t}})).status""", sess["token"])
            print(f"  token after logout -> HTTP {dead}")
            if dead != 401:
                failures.append(f"OTP: token still alive after logout ({dead})")

            browser.close()
        return _fail(failures)
    finally:
        srv.terminate()


def _fail(failures: list[str]) -> int:
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nSERVER-SIDE OTP LOGIN: ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
