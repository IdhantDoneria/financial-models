/* Background, best-effort warm-up of the terminal's runtime dependencies —
 * fired from the login screen so the multi-second Pyodide/scientific-stack
 * download overlaps with the time a human spends filling in the sign-in or
 * signup form, instead of only starting after they land on the terminal.
 *
 * This never runs loadPyodide() inside the terminal's own page — it can't:
 * finishLogin() does a full location.replace("./") navigation, which throws
 * away any JS state (including an in-flight promise) this page built up. The
 * win instead comes from the browser's ordinary HTTP cache: jsdelivr serves
 * Pyodide/its packages from versioned, long-cached URLs, and boot() in
 * terminal.js requests those exact same URLs (via boot-packages.js) moments
 * later — so a cache warmed here is one the real boot gets to reuse.
 *
 * Deliberately conservative about *when* it fires and *who* it fires for:
 *  - Only after the user actually interacts with the page (typing, tapping,
 *    focusing a field) or a short dwell time passes — never on page load —
 *    so a visitor who closes the tab within the first second never pays for
 *    a download they didn't need.
 *  - Skipped entirely on Data Saver or a 2G-class connection.
 *  - Every failure is swallowed silently: a prewarm that doesn't finish (or
 *    doesn't even start) just means boot() does the full job later, exactly
 *    as it did before this file existed. Nothing here is load-bearing.
 */
"use strict";
(function () {
  const conn = navigator.connection || navigator.webkitConnection || navigator.mozConnection;
  if (conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ""))) return;

  let started = false;
  async function prewarm() {
    if (started) return;
    started = true;
    try {
      await new Promise((ok, bad) => {
        const s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/pyodide/v0.28.2/full/pyodide.js";
        s.onload = ok;
        s.onerror = bad;
        document.head.appendChild(s);
      });
      const pyodide = await loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v0.28.2/full/" });
      await pyodide.loadPackage(window.FINMODELS_CORE_PACKAGES || ["numpy", "scipy", "micropip"]);
      const micropip = pyodide.pyimport("micropip");
      try { await micropip.install(window.FINMODELS_PLOTLY_SPEC || "plotly==6.8.0"); }
      catch { await micropip.install("plotly"); }

      // Same-origin static assets boot() also fetches — cheap to warm too,
      // and benefit from the app's own Cache-Control now that it's set
      // explicitly (see vercel.json).
      fetch("py/manifest.json").then((r) => r.json()).then((m) => {
        Promise.all(m.files.map((f) => fetch("py/" + f.path).catch(() => {})));
      }).catch(() => {});
      fetch("py/web_bridge.py").catch(() => {});
      fetch("data/ff_factors.csv").catch(() => {});
    } catch {
      /* best-effort — boot() on the terminal page does the real work regardless */
    }
  }

  const INTERACT_EVENTS = ["pointerdown", "keydown", "focus"];
  function onFirstInteract() { cleanup(); prewarm(); }
  function cleanup() {
    INTERACT_EVENTS.forEach((e) => document.removeEventListener(e, onFirstInteract, { capture: true }));
    clearTimeout(dwellTimer);
  }
  INTERACT_EVENTS.forEach((e) => document.addEventListener(e, onFirstInteract, { capture: true, once: true }));
  const dwellTimer = setTimeout(() => { cleanup(); prewarm(); }, 1000);
})();
