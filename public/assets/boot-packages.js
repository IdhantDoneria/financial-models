/* Single source of truth for the Pyodide packages the terminal needs at boot
 * (assets/terminal.js) and the ones the login-page background prewarm
 * (assets/boot-prewarm.js) fetches ahead of time. Both call
 * loadPyodide()/loadPackage()/micropip.install() with these exact values —
 * a drift between them (e.g. a version bump applied to one but not the
 * other) would silently defeat the prewarm's HTTP-cache benefit, since
 * jsdelivr/PyPI cache by exact URL. Keeping both call sites pointed at one
 * constant makes that drift structurally impossible instead of "remembered."
 *
 * pandas is deliberately NOT in CORE_PACKAGES: only Fama-French (mnemonic
 * FF3) needs it, so it's loaded lazily on first use instead (see
 * ensurePandas() in terminal.js) rather than paid by every visitor.
 */
"use strict";
window.FINMODELS_CORE_PACKAGES = Object.freeze(["numpy", "scipy", "micropip"]);
window.FINMODELS_PLOTLY_SPEC = "plotly==6.8.0";
