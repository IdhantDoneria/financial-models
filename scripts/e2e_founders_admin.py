"""Browser E2E: complimentary-access notice, password sign-in layer, admin desk.

Full local stack (scripts/dev_auth_server.js — real api/*.js, in-memory
store, dev admin key). Drives:
  1. login page shows the complimentary-access (concierge) notice,
  2. OTP signup that also sets a password -> stays on the FREE plan (the
     automatic first-20 founders promo is retired; free access is now
     granted manually via the admin desk),
  3. sign-out -> sign back in through the PASSWORD tab (no email code),
  4. /admin: unlock with the operator key, read the user directory
     (email, PW tag), confirm the visitor-geography section renders,
     grant premium to a typed email + duration, revoke it.

    python scripts/e2e_founders_admin.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8128
BASE = f"http://127.0.0.1:{PORT}/"


def main() -> int:
    srv = subprocess.Popen(["node", str(ROOT / "scripts" / "dev_auth_server.js"), str(PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
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
            page.route("**/pyodide*", lambda r: r.abort())   # billing UI needs no runtime
            code_box: dict = {}
            page.on("response", lambda r: code_box.update(
                json.loads(r.text() or "{}")) if "auth-request-otp" in r.url else None)

            print("· complimentary-access notice on the login page…")
            page.goto(BASE + "login.html", timeout=30_000)
            page.wait_for_selector("#concierge", state="visible", timeout=15_000)
            banner = page.inner_text("#concierge")
            print(f"  {banner[:90]}…")
            if "finmodels10@gmail.com" not in banner or "48 business hours" not in banner:
                failures.append(f"CONCIERGE: banner wrong ({banner[:80]})")
            afoot = page.inner_text(".afoot")
            if "finmodels10@gmail.com" not in afoot or "doneriaidhant@gmail.com" not in afoot:
                failures.append(f"CONCIERGE: footer support line missing ({afoot[:100]})")

            print("· OTP signup + set password -> stays on FREE (no auto-grant)…")
            page.fill("#otp-email", "founder1@example.com")
            page.click("#otp-send")
            page.wait_for_selector("#otp-step2", state="visible", timeout=20_000)
            page.fill("#otp-code", code_box.get("devCode", ""))
            page.fill("#otp-name", "First Founder")
            page.fill("#otp-pass", "Str0ngPass!Word")
            page.click("#otp-verify")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            # Pyodide's CDN is unreachable in this sandbox, so boot() never
            # completes and buildUI() (which wires up initBilling/#planchip)
            # never runs on its own — call it directly, same as the other
            # Pyodide-bypass suites in this repo.
            page.wait_for_function("typeof buildUI === 'function'", timeout=15_000)
            page.evaluate("""() => {
              state.user = currentUser();
              state.country = COUNTRIES[0];
              applyCountryDefaults(state.country);
              buildUI();
              document.getElementById('boot').style.display = 'none';
              document.getElementById('app').classList.add('ready');
            }""")
            page.wait_for_function(
                "() => document.querySelector('#planchip').textContent.trim().length > 0",
                timeout=15_000)
            chip = page.inner_text("#planchip")
            print(f"  chip: {chip}")
            if "DESK UNLIMITED" in chip or "FREE" not in chip.upper():
                failures.append(f"CONCIERGE: new signup unexpectedly not on FREE ({chip})")

            print("· sign out -> back in via the PASSWORD tab…")
            page.evaluate("localStorage.removeItem('finmodels.session')")
            page.goto(BASE + "login.html", timeout=30_000)
            page.wait_for_selector("#stabs", state="visible", timeout=15_000)
            page.click("#stab-pass")
            page.fill("#pw-email", "founder1@example.com")
            page.fill("#pw-pass", "Str0ngPass!Word")
            page.click("#pw-btn")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            sess = json.loads(page.evaluate("localStorage.getItem('finmodels.session')"))
            print(f"  signed in — provider={sess['provider']} token={'yes' if sess.get('token') else 'NO'}")
            if not sess.get("token"):
                failures.append("PASSWORD: no server token after password login")

            print("· admin desk at /admin…")
            page.goto(BASE + "admin", timeout=30_000)
            page.fill("#key", "devadmin")
            page.click("#enter")
            page.wait_for_selector("#app", state="visible", timeout=15_000)
            table = page.inner_text("#users")
            stats = page.inner_text("#stats")
            geo = page.inner_text("#geo")
            print(f"  stats: {stats.replace(chr(10), ' · ')[:110]}")
            if "founder1@example.com" not in table:
                failures.append("ADMIN: user email missing from directory")
            if "PW" not in table:
                failures.append("ADMIN: password tag missing")
            if "TRACKED VISITS" not in stats:
                failures.append("ADMIN: visitor-geography stat missing")
            if "COUNTRY" not in geo:
                failures.append("ADMIN: geo breakdown table missing")

            print("· granting premium to a typed email + duration…")
            page.fill("#g-email", "vip@bigbank.com")
            page.select_option("#g-plan", "pro")
            page.select_option("#g-days", "90")
            page.click("#g-go")
            page.wait_for_function(
                "() => document.querySelector('#msg').classList.contains('ok')", timeout=15_000)
            page.wait_for_function(
                "() => document.querySelector('#users').textContent.includes('vip@bigbank.com')",
                timeout=15_000)
            row = page.locator("#users tr", has_text="vip@bigbank.com").inner_text()
            print(f"  granted row: {row.replace(chr(10), ' | ')[:110]}")
            if "ANALYST PRO" not in row or "GRANT" not in row:
                failures.append(f"ADMIN: grant row wrong ({row[:90]})")

            page.locator('#users [data-revoke="vip@bigbank.com"]').click()
            page.wait_for_function(
                """() => { const r = [...document.querySelectorAll('#users tr')]
                       .find((t) => t.textContent.includes('vip@bigbank.com'));
                     return r && r.textContent.includes('FREE'); }""", timeout=15_000)
            print("  revoked — back to FREE")

            browser.close()
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(" -", f)
            return 1
        print("\nCONCIERGE + PASSWORD + ADMIN E2E: ALL CHECKS PASS")
        return 0
    finally:
        srv.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
