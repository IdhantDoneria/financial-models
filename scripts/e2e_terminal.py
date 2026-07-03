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
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.route(lambda url: "127.0.0.1" not in url and "localhost" not in url,
                   _fulfill_external)
        page.on("console", lambda m: m.type == "error" and print(f"  [console] {m.text[:200]}"))

        print(f"· loading terminal at {base} + booting Pyodide…")
        page.goto(base, timeout=60_000)
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

        failures += run_ib_desk_scenario(page)

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
