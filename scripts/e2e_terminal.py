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


def _fulfill_external(route) -> None:
    url = route.request.url
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


def main() -> int:
    headed = "--headed" in sys.argv
    srv = serve()
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
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.route(lambda url: "127.0.0.1" not in url and "localhost" not in url,
                   _fulfill_external)
        page.on("console", lambda m: m.type == "error" and print(f"  [console] {m.text[:200]}"))

        print("· loading terminal + booting Pyodide (downloads on first run)…")
        page.goto(f"http://127.0.0.1:{PORT}/", timeout=60_000)
        page.wait_for_function("window.TERMINAL_READY === true", timeout=300_000)
        print("· runtime online")

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

        page.screenshot(path=str(ROOT / "docs" / "design" / "terminal-screenshot.png"))
        browser.close()
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
