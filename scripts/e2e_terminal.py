"""End-to-end check of the browser terminal: boot Pyodide, run all 10 models.

Serves ``public/`` on localhost, drives Chromium via Playwright, waits for the
WASM Python runtime to come online, then selects every mnemonic and asserts
the OUTPUT panel renders numeric results with no error banner and the CHART
panel renders an SVG. This exercises the exact code path a visitor gets on the
deployed Vercel URL.

Usage::

    python scripts/e2e_terminal.py [--headed]

Requires network access (Pyodide/CDN downloads) and a Playwright Chromium.
Not part of the pytest suite — run manually or in a dedicated CI job.
"""

from __future__ import annotations

import functools
import http.server
import os
import sys
import threading
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8123
#: Sandbox proxies can reset Chromium's TLS handshakes; fetch CDN assets with
#: requests (which trusts the proxy CA) and fulfill the browser routes from a
#: local cache. Purely a test-harness concern — deployed visitors fetch CDNs
#: directly.
CDN_CACHE = Path(os.environ.get("E2E_CDN_CACHE", "/tmp/e2e-cdn-cache"))


#: Host under test (set in main when --url is used); never served from cache so
#: a fresh deployment is always what gets exercised.
_TARGET_HOST: str | None = None


def _fulfill_external(route) -> None:
    url = route.request.url
    if _TARGET_HOST and _TARGET_HOST in url:
        try:
            resp = requests.get(url, timeout=120)
        except Exception:
            return route.abort()
        return route.fulfill(status=resp.status_code, body=resp.content,
                             headers={"content-type": resp.headers.get(
                                 "content-type", "application/octet-stream")})
    key = CDN_CACHE / (url.replace("://", "_").replace("/", "_")[:200])
    meta = key.with_suffix(key.suffix + ".ct")
    if not key.exists():
        try:
            resp = requests.get(url, timeout=120)
        except Exception:
            return route.abort()
        if resp.status_code != 200:
            return route.fulfill(status=resp.status_code, body=b"")
        CDN_CACHE.mkdir(parents=True, exist_ok=True)
        key.write_bytes(resp.content)
        meta.write_text(resp.headers.get("content-type", "application/octet-stream"))
    route.fulfill(status=200, body=key.read_bytes(),
                  headers={"content-type": meta.read_text(),
                           "access-control-allow-origin": "*"})
MNEMONICS = ["DCF", "GG", "MPT", "VAR", "CAPM", "FF3", "BSM", "CRR", "MC", "HES"]
#: Headline output key expected per model (sanity that real numbers rendered).
EXPECT_KEY = {
    "DCF": "ENTERPRISE VALUE", "GG": "PRICE", "MPT": "TANGENCY RETURN",
    "VAR": "VAR", "CAPM": "EXPECTED RETURN", "FF3": "BETA MKT",
    "BSM": "PRICE", "CRR": "PRICE", "MC": "PRICE", "HES": "PRICE",
}


def serve() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "public")
    )
    handler.log_message = lambda *a, **k: None  # quiet
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _synthetic_10q(path: Path) -> Path:
    """Small quarterly filing whose figures the extractor must recover."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    doc.build([Paragraph(t, styles["Normal"]) for t in (
        "Acme Corporation", "NYSE: ACME",
        "Form 10-Q — For the quarter ended March 31, 2026",
        "Consolidated Financial Statements (in $ millions)",
        "Total revenue     $ 3,112", "Net income        $ 457",
        "Total debt        $ 3,500", "Cash and cash equivalents $ 850",
        "Weighted-average shares outstanding 425 million",
        "Share price $ 87.50", "Beta 1.15", "Revenue growth 3%",
        "Operating margin 18%", "Effective tax rate 22%",
    )])
    return path


def run_ib_desk_scenario(page) -> list[str]:
    """Exercise the PDF analyzer: upload -> auto run -> all three exports."""
    failures: list[str] = []
    pdf = _synthetic_10q(Path("/tmp/e2e_synth_10q.pdf"))

    print("· IB DESK: opening analyzer (installs PDF/export backends)…")
    page.fill("#cmd", "IB")
    page.click("#go")
    page.wait_for_selector("#ibrun", timeout=30_000)
    page.set_input_files("#ibfile", str(pdf))
    try:
        page.wait_for_selector("#ibrun:not([disabled])", timeout=240_000)
    except Exception:
        return [f"IB: extraction never became ready — status: {page.inner_text('#ostat')}"]
    grid = page.inner_text("#ogrid")
    found = grid.count("PDF")
    print(f"  extraction ok — {found} fields scraped from the filing")
    if "COMPANY" not in grid or found < 6:
        failures.append(f"IB: too few fields extracted ({found})")

    page.click("#ibrun")
    try:
        page.wait_for_selector("#report table", timeout=180_000)
    except Exception:
        return failures + [f"IB: report never rendered — {page.inner_text('#ostat')}"]
    rows = page.locator("#report table >> nth=0 >> tr").count() - 1
    errs = page.locator("#report tr.err").count()
    print(f"  report ok — {rows} models, {errs} errors")
    if rows != 10 or errs:
        failures.append(f"IB: expected 10 clean models, got rows={rows} errors={errs}")

    sig = {"pdf": b"%PDF-", "docx": b"PK", "xlsx": b"PK"}
    for fmt, magic in sig.items():
        try:
            with page.expect_download(timeout=120_000) as dl:
                page.click(f"#exp-{fmt}")
            target = Path(f"/tmp/e2e_report.{fmt}")
            dl.value.save_as(target)
            blob = target.read_bytes()
            ok = blob[: len(magic)] == magic and len(blob) > 2000
            print(f"  export {fmt}: {dl.value.suggested_filename} · {len(blob):,} bytes {'ok' if ok else 'BAD'}")
            if not ok:
                failures.append(f"IB: {fmt} export invalid")
        except Exception as exc:
            failures.append(f"IB: {fmt} export failed — {str(exc)[:120]}")
    return failures


def run_auth_scenario(browser, base: str) -> list[str]:
    """Full account lifecycle on a fresh (sessionless) page.

    Gate redirect -> signup -> identity chip -> sign out -> wrong password
    rejected -> correct login -> guest access. Google SSO is asserted to show
    its explicit 'not configured' state when no GOOGLE_CLIENT_ID is exposed.
    """
    failures: list[str] = []
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    page.route(lambda url: "127.0.0.1" not in url and "localhost" not in url,
               _fulfill_external)

    print("· AUTH: unauthenticated visit must gate to the login page…")
    page.goto(base, timeout=60_000)
    try:
        page.wait_for_url("**/login*", timeout=15_000)
    except Exception:
        page.close()
        return ["AUTH: terminal did not redirect to login.html without a session"]

    # Google button state (no client id configured locally -> honest fallback)
    page.wait_for_function(
        "() => document.querySelector('#ghint').textContent.length > 0 || "
        "document.querySelector('#gwrap').classList.contains('live')", timeout=15_000)
    gis_live = page.evaluate("document.querySelector('#gwrap').classList.contains('live')")
    print(f"  google sso: {'LIVE (client id configured)' if gis_live else 'declared not-configured (expected without env var)'}")

    print("· AUTH: creating an account…")
    page.click("#tab-signup")
    page.fill("#su-name", "Alexandra Whitmore")
    page.fill("#su-email", "vp@example.com")
    page.fill("#su-pass", "Vampire$quid1869")
    page.fill("#su-pass2", "Vampire$quid1869")
    page.click("#su-btn")
    try:
        page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
    except Exception:
        err = page.inner_text("#auth-err")
        page.close()
        return [f"AUTH: signup did not enter the terminal — {err or 'no error shown'}"]
    page.wait_for_function("window.TERMINAL_READY === true", timeout=300_000)
    who = page.inner_text("#who")
    print(f"  signed up + booted — chip: {who!r}")
    if "ALEXANDRA" not in who:
        failures.append(f"AUTH: identity chip wrong after signup ({who!r})")

    print("· AUTH: sign out -> wrong password -> correct login…")
    page.click("#signout")
    page.wait_for_url("**/login*", timeout=15_000)
    page.fill("#si-email", "vp@example.com")
    page.fill("#si-pass", "wrong-password-123")
    page.click("#si-btn")
    page.wait_for_selector("#auth-err.on", timeout=15_000)
    err = page.inner_text("#auth-err")
    print(f"  wrong password rejected: {err!r}")
    if "INVALID PASSWORD" not in err:
        failures.append(f"AUTH: wrong password not rejected cleanly ({err!r})")
    page.fill("#si-pass", "Vampire$quid1869")
    page.click("#si-btn")
    try:
        page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
        print("  correct login accepted")
    except Exception:
        failures.append("AUTH: correct login did not enter the terminal")

    print("· AUTH: guest path…")
    page.evaluate("localStorage.removeItem('finmodels.session')")
    page.goto(base.rstrip('/') + "/login.html", timeout=60_000)
    page.click("#guest")
    try:
        page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
        print("  guest access ok")
    except Exception:
        failures.append("AUTH: guest access failed")
    page.close()
    return failures


def run_history_restore_scenario(page) -> list[str]:
    """Reopen the saved IB-desk analysis: snapshot shown, exports gated until
    a re-run recomputes the report in-runtime, then export must work."""
    failures: list[str] = []
    print("· HISTORY: reopening the saved company analysis…")
    page.click("#burger")
    page.click('.mtab[data-tab="history"]')
    if not page.locator(".hist-item").count():
        return ["HIST: IB-desk run was not recorded to history"]
    page.click(".hist-item .hload")
    try:
        page.wait_for_function(
            "() => document.querySelector('#ostat').textContent.includes('RESTORED')",
            timeout=120_000)
    except Exception:
        return [f"HIST: restore never completed — {page.inner_text('#ostat')}"]
    snapshot_rows = page.locator("#report table >> nth=0 >> tr").count() - 1
    exp_gated = page.locator("#exp-pdf").is_disabled()
    print(f"  snapshot shown ({snapshot_rows} models) · exports gated={exp_gated}")
    if snapshot_rows < 1:
        failures.append("HIST: restored snapshot has no report rows")
    if not exp_gated:
        failures.append("HIST: exports must be disabled on a stale snapshot")

    print("  re-running from restored extraction…")
    page.click("#ibrun")
    try:
        page.wait_for_function(
            "() => document.querySelector('#ostat').textContent.includes('REPORT READY')",
            timeout=180_000)
    except Exception:
        return failures + [f"HIST: re-run failed — {page.inner_text('#ostat')}"]
    try:
        with page.expect_download(timeout=120_000) as dl:
            page.click("#exp-pdf")
        blob = Path("/tmp/e2e_restore.pdf")
        dl.value.save_as(blob)
        ok = blob.read_bytes()[:5] == b"%PDF-"
        print(f"  re-run + export ok — {dl.value.suggested_filename} valid={ok}")
        if not ok:
            failures.append("HIST: export after restore produced an invalid PDF")
    except Exception as exc:
        failures.append(f"HIST: export after restore failed — {str(exc)[:120]}")
    return failures


def run_menu_country_scenario(page) -> list[str]:
    """Exercise the hamburger menu (guide/models/history) and country selector."""
    failures: list[str] = []

    print("· MENU: opening hamburger…")
    page.click("#burger")
    page.wait_for_selector("#menu.on", timeout=5_000)
    guide_len = len(page.inner_text("#menu-body"))
    page.click('.mtab[data-tab="models"]')
    briefs = page.locator("#menu-body .brief").count()
    best_for = page.inner_text("#menu-body").count("Best for:")
    page.click('.mtab[data-tab="history"]')
    hist_txt = page.inner_text("#menu-body")
    print(f"  guide={guide_len} chars · {briefs} model briefs · {best_for} 'best for' · history tab ok")
    if guide_len < 400:
        failures.append("MENU: guide tab too short")
    if briefs != 10 or best_for != 10:
        failures.append(f"MENU: expected 10 briefs w/ guidance, got {briefs}/{best_for}")
    if "SAVED COMPANY" not in hist_txt.upper():
        failures.append("MENU: history tab missing")
    page.click("#menu-close")

    print("· COUNTRY: switching market to India…")
    page.click("#country-btn")
    page.wait_for_selector("#country-drop.on", timeout=5_000)
    rows = page.locator("#country-drop .crow").count()
    page.click('.crow[data-code="IN"]')
    page.wait_for_function(
        "() => !document.querySelector('#country-drop').classList.contains('on')",
        timeout=5_000)
    code = page.inner_text("#country-btn .ccode")
    # CAPM's risk-free default should now track India's ~6.90% sovereign yield.
    page.fill("#cmd", "CAPM")
    page.click("#go")
    page.wait_for_function(
        "() => document.querySelector('#output .title').textContent.endsWith('CAPM')",
        timeout=30_000)
    rf_field = page.locator('#pform .prow').filter(has_text="RISK-FREE").locator(".val").input_value()
    print(f"  {rows} countries · selected={code} · CAPM risk-free now {rf_field}")
    if rows != 15:
        failures.append(f"COUNTRY: expected 15 markets, got {rows}")
    if code != "IN":
        failures.append(f"COUNTRY: button did not update (got {code})")
    if not rf_field.startswith("6.9"):
        failures.append(f"COUNTRY: CAPM risk-free did not follow market (got {rf_field})")
    # restore US so later scenarios use the default market
    page.click("#country-btn")
    page.click('.crow[data-code="US"]')
    return failures


def run_scenario_engine_scenario(page) -> list[str]:
    """SCEN tab: bear/base/bull compare, tornado chart, 2-way sensitivity grid."""
    failures: list[str] = []

    print("· SCEN: opening scenario engine on DCF…")
    page.fill("#cmd", "DCF")
    page.click("#go")
    page.wait_for_function(
        "() => document.querySelector('#output .title').textContent.endsWith('DCF')",
        timeout=60_000)
    page.click("#tab-scen")
    page.wait_for_selector("#scen-auto", timeout=5_000)

    # auto-seed bear/base/bull (probes each input's impact direction in-runtime)
    page.click("#scen-auto")
    page.wait_for_selector(".scen-chips .chip.bull.set", timeout=120_000)
    chips = page.locator(".scen-chips .chip.set").count()
    print(f"  auto-seeded — {chips}/3 slots set")
    if chips != 3:
        failures.append(f"SCEN: auto-seed set {chips}/3 slots")

    # side-by-side comparison: bear per-share value must be below bull
    page.click("#scen-compare")
    page.wait_for_selector("#scen-cmp table tr.headline", timeout=60_000)
    import re as _re
    cells = page.locator("#scen-cmp tr.headline td.v").all_inner_texts()
    nums = [float(_re.sub(r"[^\d.eE+-]", "", c.split()[0]).replace(",", "")) for c in cells]
    print(f"  compare — VALUE/SHARE bear={nums[0]:.2f} base={nums[1]:.2f} bull={nums[2]:.2f}")
    if not (len(nums) == 3 and nums[0] < nums[1] < nums[2]):
        failures.append(f"SCEN: bear<base<bull violated ({nums})")

    # tornado chart renders and WACC ranks among the top drivers for a DCF
    page.click("#scen-trun")
    page.wait_for_selector("#scen-tornado .main-svg", timeout=120_000)
    stat = page.inner_text("#scen-stat")
    print(f"  tornado — {stat}")
    if "BIGGEST DRIVER" not in stat:
        failures.append(f"SCEN: tornado gave no driver ranking ({stat})")

    # 7x7 two-way grid (default WACC x terminal growth), centre = current
    page.click("#scen-grun")
    page.wait_for_selector("#scen-grid table", timeout=120_000)
    grid_rows = page.locator("#scen-grid table tr").count()
    centre = page.locator("#scen-grid td.centre").count()
    print(f"  grid — {grid_rows - 1}x7 cells · centre marked={centre}")
    if grid_rows != 8 or centre != 1:
        failures.append(f"SCEN: grid malformed (rows={grid_rows}, centre={centre})")

    # scenarios must be per-model: switching models resets the panel
    page.fill("#cmd", "BSM")
    page.click("#go")
    page.wait_for_function(
        "() => document.querySelector('#output .title').textContent.endsWith('BSM')",
        timeout=60_000)
    page.click("#tab-scen")
    head = page.inner_text("#scen .scen-head")
    if "BLACK-SCHOLES" not in head.upper():
        failures.append(f"SCEN: panel did not rebuild for BSM ({head[:60]})")
    page.click("#tab-chart")
    return failures


def main() -> int:
    headed = "--headed" in sys.argv
    # --url https://… tests a deployed instance instead of the local tree.
    target = None
    if "--url" in sys.argv:
        target = sys.argv[sys.argv.index("--url") + 1].rstrip("/") + "/"
        global _TARGET_HOST
        _TARGET_HOST = target.split("//", 1)[-1].split("/", 1)[0]
    srv = None if target else serve()
    base = target or f"http://127.0.0.1:{PORT}/"
    failures: list[str] = []
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    with sync_playwright() as pw:
        # Sandbox images pre-install Chromium at a fixed path; prefer it over
        # downloading a version-matched build.
        exe = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium")
        browser = pw.chromium.launch(
            headless=not headed,
            executable_path=exe if Path(exe).exists() else None,
            proxy={"server": proxy, "bypass": "localhost,127.0.0.1"} if proxy else None,
        )
        failures += run_auth_scenario(browser, base)

        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.route(lambda url: "127.0.0.1" not in url and "localhost" not in url,
                   _fulfill_external)
        page.on("console", lambda m: m.type == "error" and print(f"  [console] {m.text[:200]}"))
        # Model/menu/IB scenarios run as a signed-in guest (the auth lifecycle
        # itself is covered above) — seed the session before any page script.
        page.add_init_script(
            "localStorage.setItem('finmodels.session', JSON.stringify("
            "{uid:'guest',name:'GUEST',provider:'guest',ts:Date.now(),"
            "exp:Date.now()+3600000}))")

        print(f"· loading terminal at {base} + booting Pyodide…")
        page.goto(base, timeout=60_000)
        page.wait_for_function("window.TERMINAL_READY === true", timeout=300_000)
        print("· runtime online")

        # Live ticker tape: /api/quotes only exists on the deployed instance;
        # locally it 404s and the CoinGecko crypto fallback fills the tape. In
        # both cases the tape must show a live price with a % change.
        try:
            page.wait_for_function(
                "() => /%/.test(document.querySelector('#tape .inner').textContent) && "
                "document.querySelectorAll('#tape .inner i.up, #tape .inner i.down, #tape .inner i.flat').length > 0",
                timeout=30_000)
            tape = page.inner_text("#tape .inner")[:140]
            print(f"· ticker live — {tape.strip()[:110]}…")
        except Exception:
            failures.append("TAPE: no live quotes rendered")

        for mn in MNEMONICS:
            # Drive through the command bar — the primary interaction path.
            page.fill("#cmd", mn)
            page.click("#go")
            try:
                page.wait_for_function(
                    """(mn) => {
                        const s = document.querySelector('#ostat').textContent;
                        const t = document.querySelector('#output .title').textContent;
                        return t.endsWith(mn) && / MS$/.test(s);
                    }""",
                    arg=mn, timeout=120_000,
                )
                body = page.inner_text("#ogrid")
                err = page.is_visible("#oerr")
                key = EXPECT_KEY[mn]
                has_chart = page.locator("#chart .main-svg").count() > 0
                ok = (not err) and key in body and has_chart
                n_rows = body.count("\n") + 1
                status = page.inner_text("#ostat")
                print(f"  {mn:5s} {'PASS' if ok else 'FAIL'}  rows={n_rows:<3d} "
                      f"chart={'y' if has_chart else 'N'}  {status}")
                if not ok:
                    failures.append(f"{mn}: err={err} key_found={key in body} chart={has_chart}")
            except Exception as exc:
                print(f"  {mn:5s} FAIL  {type(exc).__name__}: {str(exc)[:120]}")
                failures.append(f"{mn}: {exc}")

        # DOC tab renders explain() with math
        page.click("#tab-doc")
        doc_len = len(page.inner_text("#doc"))
        katex = page.locator("#doc .katex").count()
        print(f"· DOC panel: {doc_len} chars, {katex} KaTeX spans")
        if doc_len < 400:
            failures.append("DOC panel suspiciously short")

        failures += run_menu_country_scenario(page)
        failures += run_scenario_engine_scenario(page)
        failures += run_ib_desk_scenario(page)
        failures += run_history_restore_scenario(page)

        page.screenshot(path=str(ROOT / "docs" / "design" / "terminal-screenshot.png"))
        browser.close()
    if srv:
        srv.shutdown()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL 10 MODELS PASS IN-BROWSER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
