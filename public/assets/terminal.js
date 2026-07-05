/* FINMODELS TERMINAL — front-end runtime.
 *
 * Boots Pyodide, loads the *actual* Python model files from /py/src (synced
 * byte-for-byte from the repo's src/ by scripts/sync_web_assets.py), and wires
 * them to a Bloomberg-style command-line UI. All pricing/valuation math runs
 * in the browser through the same classes the test suite validates.
 */
"use strict";

/* ------------------------------------------------------------------------ *
 * Model registry: mnemonics, function keys, formulas, parameter schemas.
 * `pct` params are stored as decimals and displayed as percentages.
 * ------------------------------------------------------------------------ */
const MODELS = [
  {
    mn: "DCF", name: "Discounted Cash Flow", cat: "Valuation",
    formula: "EV = Σ FCF_t/(1+r)^t + TV/(1+r)^N",
    params: [
      { id: "base_fcf", label: "BASE FCF", money: true, scale: "M", min: 1, max: 500, step: 1, def: 100 },
      { id: "fcf_growth", label: "FCF GROWTH", min: -0.10, max: 0.25, step: 0.005, def: 0.08, pct: true },
      { id: "years", label: "HORIZON (Y)", min: 3, max: 10, step: 1, def: 5, int: true },
      { id: "discount_rate", label: "WACC", min: 0.04, max: 0.20, step: 0.0025, def: 0.09, pct: true },
      { id: "terminal_growth", label: "TERMINAL G", min: 0.0, max: 0.05, step: 0.0025, def: 0.025, pct: true },
      { id: "net_debt", label: "NET DEBT", money: true, scale: "M", min: -500, max: 2000, step: 10, def: 250 },
      { id: "shares_outstanding", label: "SHARES (M)", min: 10, max: 2000, step: 10, def: 150 },
    ],
  },
  {
    mn: "GG", name: "Gordon Growth (DDM)", cat: "Valuation",
    formula: "P₀ = D₁ / (r − g)",
    params: [
      { id: "dividend", label: "DIVIDEND D₀", money: true, min: 0.1, max: 20, step: 0.1, def: 2.5 },
      { id: "required_return", label: "REQUIRED r", min: 0.02, max: 0.25, step: 0.0025, def: 0.08, pct: true },
      { id: "growth", label: "GROWTH g", min: 0.0, max: 0.10, step: 0.0025, def: 0.04, pct: true },
    ],
  },
  {
    mn: "MPT", name: "Modern Portfolio Theory", cat: "Portfolio",
    formula: "min wᵀΣw  s.t.  wᵀ1 = 1",
    params: [
      { id: "mu1", label: "μ ASSET 1", min: 0, max: 0.25, step: 0.005, def: 0.08, pct: true },
      { id: "mu2", label: "μ ASSET 2", min: 0, max: 0.25, step: 0.005, def: 0.12, pct: true },
      { id: "mu3", label: "μ ASSET 3", min: 0, max: 0.25, step: 0.005, def: 0.15, pct: true },
      { id: "sigma1", label: "σ ASSET 1", min: 0.05, max: 0.60, step: 0.005, def: 0.15, pct: true },
      { id: "sigma2", label: "σ ASSET 2", min: 0.05, max: 0.60, step: 0.005, def: 0.22, pct: true },
      { id: "sigma3", label: "σ ASSET 3", min: 0.05, max: 0.60, step: 0.005, def: 0.30, pct: true },
      { id: "rho", label: "PAIRWISE ρ", min: -0.45, max: 0.90, step: 0.05, def: 0.25 },
      { id: "risk_free_rate", label: "RISK-FREE r", min: 0, max: 0.08, step: 0.0025, def: 0.03, pct: true },
    ],
  },
  {
    mn: "VAR", name: "Value at Risk / CVaR", cat: "Risk",
    formula: "VaR_α = −Q_α(P&L),  CVaR = E[loss | loss > VaR]",
    params: [
      { id: "mu_annual", label: "ANNUAL μ", min: -0.20, max: 0.30, step: 0.005, def: 0.07, pct: true },
      { id: "sigma_annual", label: "ANNUAL σ", min: 0.05, max: 0.80, step: 0.005, def: 0.20, pct: true },
      { id: "confidence", label: "CONFIDENCE", min: 0.90, max: 0.99, step: 0.005, def: 0.95, pct: true },
      { id: "horizon_days", label: "HORIZON (D)", min: 1, max: 30, step: 1, def: 10, int: true },
      { id: "portfolio_value", label: "PORTFOLIO", money: true, scale: "M", min: 0.1, max: 1000, step: 0.1, def: 100 },
      { id: "method", label: "METHOD", select: ["historical", "parametric", "monte_carlo"], def: "historical" },
    ],
  },
  {
    mn: "CAPM", name: "Capital Asset Pricing Model", cat: "Equity / Factor",
    formula: "E[R] = r_f + β (E[R_m] − r_f)",
    params: [
      { id: "risk_free_rate", label: "RISK-FREE r", min: 0, max: 0.08, step: 0.0025, def: 0.042, pct: true },
      { id: "expected_market_return", label: "E[R MARKET]", min: 0.02, max: 0.20, step: 0.0025, def: 0.09, pct: true },
      { id: "beta", label: "BETA β", min: -1, max: 3, step: 0.05, def: 1.15 },
    ],
  },
  {
    mn: "FF3", name: "Fama-French 3-Factor", cat: "Equity / Factor",
    formula: "Rᵢ−R_f = α + b·MKT + s·SMB + h·HML + ε   (OLS on real Ken French history)",
    params: [
      { id: "b_mkt", label: "TRUE b (MKT)", min: -1, max: 2, step: 0.05, def: 1.1 },
      { id: "s_smb", label: "TRUE s (SMB)", min: -1, max: 2, step: 0.05, def: 0.4 },
      { id: "h_hml", label: "TRUE h (HML)", min: -1, max: 2, step: 0.05, def: -0.2 },
      { id: "alpha", label: "TRUE α (MO)", min: -0.01, max: 0.01, step: 0.0005, def: 0.001, pct: true },
      { id: "idio_sigma", label: "IDIO σ (MO)", min: 0, max: 0.05, step: 0.0025, def: 0.02, pct: true },
      { id: "window", label: "WINDOW (MO)", min: 24, max: 360, step: 12, def: 120, int: true },
    ],
  },
  {
    mn: "BSM", name: "Black-Scholes-Merton", cat: "Derivatives",
    formula: "C = S e^{−qT} N(d₁) − K e^{−rT} N(d₂)",
    params: [
      { id: "spot", label: "SPOT S", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "rate", label: "RATE r", min: 0, max: 0.15, step: 0.0025, def: 0.05, pct: true },
      { id: "sigma", label: "VOL σ", min: 0.05, max: 1.0, step: 0.005, def: 0.20, pct: true },
      { id: "maturity", label: "MATURITY T (Y)", min: 0.05, max: 5, step: 0.05, def: 1 },
      { id: "dividend_yield", label: "DIV YIELD q", min: 0, max: 0.08, step: 0.0025, def: 0, pct: true },
      { id: "option_type", label: "TYPE", toggle: ["call", "put"], def: "call" },
    ],
  },
  {
    mn: "CRR", name: "Binomial Tree (CRR)", cat: "Derivatives",
    formula: "p = (e^{(r−q)Δt} − d) / (u − d),  u = e^{σ√Δt}",
    params: [
      { id: "spot", label: "SPOT S", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "rate", label: "RATE r", min: 0, max: 0.15, step: 0.0025, def: 0.05, pct: true },
      { id: "sigma", label: "VOL σ", min: 0.05, max: 1.0, step: 0.005, def: 0.20, pct: true },
      { id: "maturity", label: "MATURITY T (Y)", min: 0.05, max: 5, step: 0.05, def: 1 },
      { id: "dividend_yield", label: "DIV YIELD q", min: 0, max: 0.08, step: 0.0025, def: 0, pct: true },
      { id: "n_steps", label: "TREE STEPS", min: 10, max: 2000, step: 10, def: 500, int: true },
      { id: "option_type", label: "TYPE", toggle: ["call", "put"], def: "call" },
      { id: "exercise", label: "EXERCISE", toggle: ["european", "american"], def: "european" },
    ],
  },
  {
    mn: "MC", name: "Monte Carlo (GBM)", cat: "Derivatives",
    formula: "Ĉ = e^{−rT} · mean[payoff(S_T)],  antithetic variates",
    params: [
      { id: "spot", label: "SPOT S", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "rate", label: "RATE r", min: 0, max: 0.15, step: 0.0025, def: 0.05, pct: true },
      { id: "sigma", label: "VOL σ", min: 0.05, max: 1.0, step: 0.005, def: 0.20, pct: true },
      { id: "maturity", label: "MATURITY T (Y)", min: 0.05, max: 5, step: 0.05, def: 1 },
      { id: "dividend_yield", label: "DIV YIELD q", min: 0, max: 0.08, step: 0.0025, def: 0, pct: true },
      { id: "n_sims", label: "PATHS", min: 10000, max: 500000, step: 10000, def: 100000, int: true },
      { id: "option_type", label: "TYPE", toggle: ["call", "put"], def: "call" },
      { id: "antithetic", label: "ANTITHETIC", toggle: [true, false], def: true },
    ],
  },
  {
    mn: "HES", name: "Heston Stochastic Vol", cat: "Derivatives",
    formula: "dv_t = κ(θ − v_t)dt + ξ√v_t dW_t,  corr(dW^S, dW^v) = ρ",
    params: [
      { id: "spot", label: "SPOT S", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", money: true, min: 1, max: 500, step: 1, def: 100 },
      { id: "rate", label: "RATE r", min: 0, max: 0.15, step: 0.0025, def: 0.03, pct: true },
      { id: "maturity", label: "MATURITY T (Y)", min: 0.1, max: 5, step: 0.05, def: 1 },
      { id: "v0", label: "INITIAL VAR v₀", min: 0.005, max: 0.5, step: 0.005, def: 0.04 },
      { id: "kappa", label: "MEAN-REV κ", min: 0.1, max: 10, step: 0.1, def: 2.0 },
      { id: "theta", label: "LONG-RUN θ", min: 0.005, max: 0.5, step: 0.005, def: 0.04 },
      { id: "xi", label: "VOL-OF-VOL ξ", min: 0.05, max: 1.5, step: 0.05, def: 0.5 },
      { id: "rho", label: "CORR ρ", min: -0.95, max: 0.5, step: 0.05, def: -0.7 },
      { id: "option_type", label: "TYPE", toggle: ["call", "put"], def: "call" },
    ],
  },
];

/* ------------------------------------------------------------------------ *
 * COUNTRIES — the 15 largest equity markets by total market capitalisation.
 * `rf` is a 10-year sovereign-yield risk-free proxy; `erp` the equity risk
 * premium (Damodaran country ratings). Selecting a country repoints every
 * risk-free / discount input and the IB desk's live rate so the terminal
 * works from the chosen market's cost of capital — dissolving the default
 * US-only geographic assumption. The US rate can still refresh LIVE from the
 * Treasury FiscalData API; others use these curated sovereign baselines.
 * ------------------------------------------------------------------------ */
const COUNTRIES = [
  { code: "US", name: "United States", flag: "🇺🇸", market: "NYSE / NASDAQ", ccy: "USD", rf: 0.0425, erp: 0.050, live: true },
  { code: "CN", name: "China",         flag: "🇨🇳", market: "SSE / SZSE",    ccy: "CNY", rf: 0.0230, erp: 0.061 },
  { code: "JP", name: "Japan",         flag: "🇯🇵", market: "JPX (Tokyo)",   ccy: "JPY", rf: 0.0105, erp: 0.056 },
  { code: "IN", name: "India",         flag: "🇮🇳", market: "NSE / BSE",     ccy: "INR", rf: 0.0690, erp: 0.078 },
  { code: "HK", name: "Hong Kong",     flag: "🇭🇰", market: "HKEX",          ccy: "HKD", rf: 0.0350, erp: 0.059 },
  { code: "FR", name: "France",        flag: "🇫🇷", market: "Euronext Paris", ccy: "EUR", rf: 0.0310, erp: 0.055 },
  { code: "GB", name: "United Kingdom", flag: "🇬🇧", market: "LSE",          ccy: "GBP", rf: 0.0410, erp: 0.055 },
  { code: "CA", name: "Canada",        flag: "🇨🇦", market: "TSX",           ccy: "CAD", rf: 0.0335, erp: 0.052 },
  { code: "SA", name: "Saudi Arabia",  flag: "🇸🇦", market: "Tadawul",       ccy: "SAR", rf: 0.0500, erp: 0.066 },
  { code: "DE", name: "Germany",       flag: "🇩🇪", market: "Deutsche Börse", ccy: "EUR", rf: 0.0245, erp: 0.050 },
  { code: "CH", name: "Switzerland",   flag: "🇨🇭", market: "SIX Swiss",     ccy: "CHF", rf: 0.0060, erp: 0.050 },
  { code: "TW", name: "Taiwan",        flag: "🇹🇼", market: "TWSE",          ccy: "TWD", rf: 0.0150, erp: 0.061 },
  { code: "AU", name: "Australia",     flag: "🇦🇺", market: "ASX",           ccy: "AUD", rf: 0.0430, erp: 0.052 },
  { code: "KR", name: "South Korea",   flag: "🇰🇷", market: "KRX",           ccy: "KRW", rf: 0.0290, erp: 0.058 },
  { code: "NL", name: "Netherlands",   flag: "🇳🇱", market: "Euronext Amsterdam", ccy: "EUR", rf: 0.0270, erp: 0.050 },
];
//: Parameter ids that represent a risk-free / short rate across the models.
const RF_PARAM_IDS = new Set(["risk_free_rate", "rate"]);

//: Display symbol per currency — used to relabel every monetary model input
//  (spot, strike, FCF, dividend, portfolio, net debt…) to the selected
//  country's currency. This relabels the unit only; it does not convert the
//  number, since these are illustrative playground inputs the user sets
//  themselves, not live FX-quoted figures.
const CCY_SYMBOL = {
  USD: "$", CNY: "¥", JPY: "¥", INR: "₹", HKD: "HK$", EUR: "€", GBP: "£",
  CAD: "C$", SAR: "SAR ", CHF: "CHF ", TWD: "NT$", AUD: "A$", KRW: "₩",
};
const ccySymbol = () => CCY_SYMBOL[(state.country && state.country.ccy) || "USD"] || "$";

//: Shown only if every live source is unreachable — the tape never goes blank.
const TAPE_FALLBACK = [
  "<b>BSM</b> HULL EX 15.6 CALL <i>4.7594</i>",
  "<b>PARITY</b> C−P−S+Ke⁻ʳᵀ <i>4.5e−16</i>",
  "<b>CRR</b> →BS CONVERGENCE <i>OK</i>",
  "<b>HES</b> ξ→0 BS REDUCTION <i>4e−09</i>",
  "<b>FF3</b> KEN FRENCH 1926–2026 <i>1199 ROWS</i>",
  "<b>RUNTIME</b> CPYTHON·WASM <i>LIVE</i>",
];

/* ------------------------------------------------------------------------ */
const $ = (sel) => document.querySelector(sel);
const state = {
  pyodide: null, runPy: null, micropip: null, current: null, values: {},
  timer: null, seq: 0, view: "model",
  user: null,              // session from login.html (set before boot)
  country: null,           // set in boot() from storage or default US
  scen: { built: null, busy: false },   // scenario & sensitivity engine
  billing: { cfg: null, usage: null },  // Razorpay plans + upload metering
  ib: { pkgsReady: false, fns: null, extracted: null, report: null,
        liveRf: null, rfSource: null, fx: null, fxDate: null,
        period: "auto", mode: "auto", dirty: {}, selected: null },
};

/* ------------------------------------------------------------------------ *
 * Persistence — country choice + saved company analyses (chat history) live
 * in localStorage so a returning visitor keeps their market and past work.
 * ------------------------------------------------------------------------ */
const LS_COUNTRY = "finmodels.country";
const LS_HISTORY = "finmodels.history";           // legacy pre-auth key
const LS_SESSION = "finmodels.session";

/* ------------------------------- auth ---------------------------------- *
 * Sessions are created by login.html (device-local accounts / Google SSO /
 * guest — see assets/auth.js). The terminal only reads the session; without
 * one it gates to the login page. History is namespaced per account.        */
function currentUser() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_SESSION) || "null");
    if (!s || !s.uid) return null;
    if (s.exp && Date.now() > s.exp) { localStorage.removeItem(LS_SESSION); return null; }
    return s;
  } catch { return null; }
}
function signOut() {
  // Revoke the server-side session first when one exists (email-OTP mode).
  try {
    const s = JSON.parse(localStorage.getItem(LS_SESSION) || "null");
    if (s && s.token) {
      fetch("api/auth-logout", {
        method: "POST", keepalive: true,
        headers: { Authorization: "Bearer " + s.token },
      }).catch(() => {});
    }
  } catch { /* best-effort revocation */ }
  try { localStorage.removeItem(LS_SESSION); } catch { /* ignore */ }
  location.replace("login.html");
}

//: Server (OTP) sessions are validated against the backend after boot; a
//  revoked/expired token signs the visitor out instead of trusting local
//  state forever. Device-local and guest sessions have no server to ask.
async function validateServerSession() {
  const u = state.user;
  if (!u || u.provider !== "otp" || !u.token) return;
  try {
    const r = await fetch("api/auth-me",
      { headers: { Authorization: "Bearer " + u.token }, signal: AbortSignal.timeout(10000) });
    if (r.status === 401) { signOut(); return; }
    if (r.ok) {
      const j = await r.json();
      if (j.user && j.user.name && j.user.name !== u.name) {   // refresh display name
        u.name = j.user.name;
        localStorage.setItem(LS_SESSION, JSON.stringify(u));
        const who = $("#who");
        if (who) who.innerHTML = `◉ USER <b>${String(u.name).toUpperCase().slice(0, 24)}</b>`;
      }
    }
  } catch { /* offline — keep the local session */ }
}

const histKey = () => "finmodels.history." + (state.user ? state.user.uid : "guest");
function loadHistory() {
  try {
    let list = JSON.parse(localStorage.getItem(histKey()) || "null");
    if (!list) {
      // one-time adoption of pre-auth history into this account
      const legacy = JSON.parse(localStorage.getItem(LS_HISTORY) || "null");
      if (legacy && legacy.length) {
        list = legacy;
        localStorage.setItem(histKey(), JSON.stringify(list));
        localStorage.removeItem(LS_HISTORY);
      }
    }
    return list || [];
  } catch { return []; }
}
function saveHistory(list) {
  try { localStorage.setItem(histKey(), JSON.stringify(list.slice(0, 40))); }
  catch { /* private mode / quota — history is best-effort */ }
}

/* ------------------------------- boot ---------------------------------- */
function bootLog(msg, cls) {
  const el = document.createElement("div");
  el.className = cls || "run";
  el.textContent = msg;
  $("#bootlog").appendChild(el);
  $("#bootlog").scrollTop = 1e9;
  if ($("#bootlog").children.length > 10) $("#bootlog").firstChild.remove();
}
const bootPct = (p) => { $("#bootbar div").style.width = p + "%"; };

async function boot() {
  try {
    bootLog("FINMODELS TERMINAL v2 — session start");
    bootLog("loading CPython 3.13 runtime (WebAssembly)…"); bootPct(8);
    state.pyodide = await loadPyodide({ indexURL: "https://cdn.jsdelivr.net/pyodide/v0.28.2/full/" });
    bootLog("pyodide runtime online", "ok"); bootPct(30);

    bootLog("loading numpy · scipy · pandas…");
    await state.pyodide.loadPackage(["numpy", "scipy", "pandas", "micropip"]);
    bootLog("scientific stack loaded", "ok"); bootPct(58);

    bootLog("installing plotly…");
    const micropip = state.pyodide.pyimport("micropip");
    state.micropip = micropip;
    try { await micropip.install("plotly==6.8.0"); }
    catch { await micropip.install("plotly"); }
    bootLog("plotly installed", "ok"); bootPct(74);

    bootLog("mounting model sources from repository…");
    const manifest = await (await fetch("py/manifest.json")).json();
    const FS = state.pyodide.FS;
    FS.mkdirTree("/app/src");
    FS.mkdirTree("/app/data/cache");
    for (const f of manifest.files) {
      const text = await (await fetch("py/" + f.path)).text();
      FS.mkdirTree("/app/" + f.path.split("/").slice(0, -1).join("/"));
      FS.writeFile("/app/" + f.path, text);
    }
    FS.writeFile("/app/web_bridge.py", await (await fetch("py/web_bridge.py")).text());
    bootLog(`mounted ${manifest.files.length + 1} python sources`, "ok"); bootPct(84);

    bootLog("seeding Fama-French factor history (Ken French library snapshot)…");
    FS.writeFile("/app/data/cache/ff_factors.csv", await (await fetch("data/ff_factors.csv")).text());
    bootLog("factor data 1926→present ready", "ok"); bootPct(90);

    bootLog("importing model package…");
    state.runPy = state.pyodide.runPython(
      "import sys; sys.path.insert(0, '/app')\n" +
      "import web_bridge\n" +
      "web_bridge.run_model"
    );
    bootLog("10 models online — src/ imported unmodified", "ok"); bootPct(100);

    const savedCc = (() => { try { return localStorage.getItem(LS_COUNTRY); } catch { return null; } })();
    state.country = COUNTRIES.find((c) => c.code === savedCc) || COUNTRIES[0];
    applyCountryDefaults(state.country);   // repoint rate defaults before first build
    applyCountryToIB(state.country);       // IB desk rates follow the saved market
                                           // from boot — never default to US

    buildUI();
    document.getElementById("boot").style.display = "none";
    document.getElementById("app").classList.add("ready");
    window.TERMINAL_READY = true;   // e2e hook
    selectModel("DCF");   // land new users on valuation first, not derivatives
    $("#cmd").focus();
  } catch (err) {
    bootLog("BOOT FAILURE: " + err, "err");
    console.error(err);
  }
}

/* ------------------------------ UI build ------------------------------- */
function buildUI() {
  renderTape(TAPE_FALLBACK);   // instant seed; replaced by live data below
  refreshTape();
  setInterval(refreshTape, 60_000);

  const rail = $("#rail .body");
  const fk = $("#fkeys");
  MODELS.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "mrow"; row.dataset.mn = m.mn;
    row.innerHTML = `<span class="mn">${m.mn}</span><span class="nm">${m.name}</span><span class="ct">${m.cat}</span>`;
    row.onclick = () => selectModel(m.mn);
    rail.appendChild(row);

    const btn = document.createElement("button");
    btn.dataset.mn = m.mn;
    btn.innerHTML = `<b>F${i + 1}</b>${m.mn}`;
    btn.onclick = () => selectModel(m.mn);
    fk.appendChild(btn);
  });

  // IB desk — the PDF analyzer view (mnemonic IB / PDF / REPORT).
  const ibRow = document.createElement("div");
  ibRow.className = "mrow ibrow"; ibRow.dataset.mn = "IB";
  ibRow.innerHTML = `<span class="mn">IB</span><span class="nm">PDF Analyzer</span><span class="ct">IB Desk · Reports</span>`;
  ibRow.onclick = selectAnalyzer;
  rail.appendChild(ibRow);
  const ibBtn = document.createElement("button");
  ibBtn.dataset.mn = "IB";
  ibBtn.innerHTML = `<b>⌁</b>IB DESK`;
  ibBtn.onclick = selectAnalyzer;
  fk.appendChild(ibBtn);

  $("#go").onclick = execCommand;
  $("#cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") execCommand(); });
  document.addEventListener("keydown", (e) => {
    const m = /^F(\d+)$/.exec(e.key);
    if (m && +m[1] >= 1 && +m[1] <= 10) { e.preventDefault(); selectModel(MODELS[+m[1] - 1].mn); }
  });

  $("#tab-chart").onclick = () => setTab("chart");
  $("#tab-scen").onclick = () => setTab("scen");
  $("#tab-doc").onclick = () => setTab("doc");

  initMobileNav();
  initMenu();
  initCountry();
  initBilling();

  // signed-in identity chip (SIGN OUT now lives inside the hamburger menu)
  const u = state.user;
  $("#who").innerHTML = `${["google", "otp"].includes(u.provider) ? "◉" : "●"} USER <b>${
    String(u.name || u.uid).toUpperCase().slice(0, 24)}</b>`;
  const mw = $("#menu-who");
  if (mw) mw.innerHTML = `SIGNED IN · <b>${String(u.name || u.uid).toUpperCase().slice(0, 22)}</b>`;

  renderClock();
  setInterval(renderClock, 1000);
  initGeoClock();
}

/* --------------------------- IP-derived local clock ---------------------- *
 * The status-bar clock defaults to UTC; initGeoClock() resolves the
 * visitor's IP (server-side, /api/geo) to a timezone once at boot and
 * renderClock() switches to it from then on — a small "this feels built
 * for me" touch, and the country tally behind /api/geo also gives the
 * operator real visitor-geography data for conversion tracking. */
let clockTz = null;
function renderClock() {
  const now = new Date();
  if (clockTz) {
    try {
      const parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: clockTz, hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false, timeZoneName: "shortOffset",
      }).formatToParts(now);
      const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
      $("#clock").textContent = `${get("hour")}:${get("minute")}:${get("second")} ${get("timeZoneName")}`;
      return;
    } catch { /* unsupported tz string from the geo API — fall back to UTC */ }
  }
  $("#clock").textContent = now.toISOString().slice(0, 19).replace("T", " ") + " UTC";
}

async function initGeoClock() {
  try {
    const r = await fetch("/api/geo", { cache: "no-store", signal: AbortSignal.timeout(6000) });
    const j = await r.json();
    if (j.ok && j.timezone) {
      clockTz = j.timezone;
      const loc = [j.city, j.country].filter(Boolean).join(", ");
      $("#clock").title = `LOCAL TIME${loc ? " · " + loc : ""}${j.flag ? " " + j.flag : ""} — DETECTED FROM YOUR IP ADDRESS`;
    }
  } catch { /* geolocation unreachable — clock stays on UTC */ }
}

/* --------------------------- phone bottom nav --------------------------- */
//: ≤820px the grid collapses to one panel at a time (see terminal.css);
//  the bottom tab bar picks which. Desktop never sets data-mview, so these
//  are no-ops there. Selecting a model auto-jumps to INPUTS and a finished
//  IB report to ANALYTICS, mirroring where a desktop user's eyes would go.
const MOBILE_MQ = window.matchMedia ? window.matchMedia("(max-width: 820px)") : { matches: false };
function isPhone() { return MOBILE_MQ.matches; }

function initMobileNav() {
  const nav = $("#mnav");
  if (!nav) return;
  nav.querySelectorAll("button").forEach((b) => { b.onclick = () => setMobileView(b.dataset.mv); });
  setMobileView("inputs");
  if (isPhone()) $("#cmd").placeholder = "MNEMONIC — DCF · BSM · IB ⏎";
}

function setMobileView(mv) {
  const main = $("#main");
  if (!main) return;
  main.dataset.mview = mv;
  document.querySelectorAll("#mnav button").forEach((b) => b.classList.toggle("on", b.dataset.mv === mv));
  // Plotly sized itself while the viz panel was display:none — re-measure.
  if (mv === "viz") requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
}

/* ------------------------------ live ticker ---------------------------- */
//: One tape entry: LABEL  PRICE  ▲/▼ ±pct%  (green up, red down).
function tapeItemHTML(q) {
  const digits = q.price >= 1000 ? 2 : q.price >= 1 ? 2 : 4;
  const num = q.price.toLocaleString("en-US", { minimumFractionDigits: q.price >= 1000 ? 2 : 0, maximumFractionDigits: digits });
  const val = (q.money ? "$" : "") + num;
  const pct = typeof q.pct === "number" ? q.pct : 0;
  const cls = pct > 0.0001 ? "up" : pct < -0.0001 ? "down" : "flat";
  const arrow = pct > 0.0001 ? "▲" : pct < -0.0001 ? "▼" : "▬";
  const sign = pct >= 0 ? "+" : "";
  return `<b>${q.label}</b> ${val} <i class="${cls}">${arrow} ${sign}${pct.toFixed(2)}%</i>`;
}

//: Paint the marquee. Repeating the sequence keeps the loop seamless; the CSS
//  animation lives on `.inner`, so swapping its children never restarts it.
function renderTape(items) {
  if (!items || !items.length) return;
  const seq = items.join(' <span>·</span> ') + ' <span>·</span> ';
  $("#tape .inner").innerHTML = seq.repeat(3);
}

//: Non-scrolling "AS OF HH:MM:SS UTC" badge pinned to the tape's right edge —
//  lets a user see data recency at a glance instead of just trusting the
//  numbers. Indices only tick during their own exchange's trading hours, so
//  an unchanged price outside that window is correct, not stale; this makes
//  that distinguishable from an actually-frozen feed.
function updateTapeTimestamp(ts) {
  const el = $("#tape-ts");
  if (el) el.textContent = `AS OF ${new Date(ts).toISOString().slice(11, 19)} UTC`;
}

async function refreshTape() {
  if (await fetchTapePrimary()) return;    // same-origin /api/quotes (full set)
  await fetchTapeFallback();               // CORS-open CoinGecko (crypto+gold)
}

async function fetchTapePrimary() {
  try {
    const r = await fetch("/api/quotes", { cache: "no-store", signal: AbortSignal.timeout(9000) });
    if (!r.ok) return false;
    const j = await r.json();
    if (!j.quotes || !j.quotes.length) return false;
    renderTape(j.quotes.map(tapeItemHTML));
    updateTapeTimestamp(j.ts || Date.now());
    return true;
  } catch { return false; }
}

async function fetchTapeFallback() {
  // If the serverless feed is unreachable, at least keep crypto + gold live
  // from CoinGecko (keyless, CORS-open), padded with the static build stats.
  try {
    const url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,pax-gold" +
      "&vs_currencies=usd&include_24hr_change=true";
    const r = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(9000) });
    const j = await r.json();
    const items = [];
    const add = (id, label) => { if (j[id]) items.push(tapeItemHTML({ label, money: true, price: j[id].usd, pct: j[id].usd_24h_change || 0 })); };
    add("bitcoin", "BITCOIN"); add("ethereum", "ETHEREUM"); add("pax-gold", "GOLD (PAXG)");
    if (!items.length) return false;
    renderTape(items.concat(TAPE_FALLBACK));
    updateTapeTimestamp(Date.now());
    return true;
  } catch { return false; }
}

function setTab(which) {
  const inIB = state.view === "ib";
  if (which === "scen" && inIB) which = "chart";   // scenarios are per-model
  $("#tab-chart").classList.toggle("on", which === "chart");
  $("#tab-scen").classList.toggle("on", which === "scen");
  $("#tab-doc").classList.toggle("on", which === "doc");
  $("#chart").style.display = which === "chart" && !inIB ? "" : "none";
  $("#report").style.display = which === "chart" && inIB ? "block" : "none";
  $("#scen").style.display = which === "scen" ? "block" : "none";
  $("#doc").style.display = which === "doc" ? "block" : "none";
  if (which === "chart" && !inIB) window.dispatchEvent(new Event("resize"));
  if (which === "scen") buildScenPanel();
}

function execCommand() {
  const raw = $("#cmd").value.trim().toUpperCase().replace(/<GO>$/, "").trim();
  $("#cmd").value = "";
  if (!raw) return;
  if (raw === "HELP") { $("#cmderr").textContent = "MNEMONICS: " + MODELS.map((m) => m.mn).join(" · ") + " · IB (PDF ANALYZER)"; return; }
  if (["IB", "PDF", "REPORT", "ANALYZER"].includes(raw)) { $("#cmderr").textContent = ""; selectAnalyzer(); return; }
  const model = MODELS.find((m) => m.mn === raw || m.name.toUpperCase() === raw);
  if (model) { $("#cmderr").textContent = ""; selectModel(model.mn); }
  else { $("#cmderr").textContent = `%INVALID MNEMONIC '${raw}' — F1..F10 OR HELP`; }
}

/* --------------------------- model selection --------------------------- */
function selectModel(mn) {
  const model = MODELS.find((m) => m.mn === mn);
  state.view = "model";
  if (isPhone()) setMobileView("inputs");
  $("#oerr").style.display = "none";
  $("#output header .title").textContent = "OUTPUT";
  setTab("chart");
  state.current = model;
  state.values = {};
  model.params.forEach((p) => { state.values[p.id] = p.def; });
  $("#tab-scen").style.display = "";       // scenarios apply to model views
  resetScenPanel();

  document.querySelectorAll(".mrow").forEach((r) => r.classList.toggle("active", r.dataset.mn === mn));
  document.querySelectorAll("#fkeys button").forEach((b) => b.classList.toggle("active", b.dataset.mn === mn));
  $("#inputs header .title").textContent = `INPUTS — ${model.name.toUpperCase()}`;
  $("#formula").innerHTML = `ƒ  <b>${model.formula}</b>`;

  const body = $("#pform");
  body.innerHTML = "";
  model.params.forEach((p) => body.appendChild(paramRow(p)));
  scheduleRun(0);
}

function fmtParam(p, v) {
  if (p.select || p.toggle) return String(v);
  if (p.pct) return (v * 100).toFixed(p.step >= 0.005 ? 1 : 2) + "%";
  if (p.int) return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return String(+(+v).toFixed(4));
}

//: Money params show a currency unit suffix ("BASE FCF (₹M)") that reflects
//  the selected country — selectCountry() rebuilds the open model's param
//  panel on every country change, so this always re-reads the current ccy.
function moneyLabelHTML(p) {
  return `<label>${p.label} (${ccySymbol()}${p.scale || ""})</label>`;
}

function paramRow(p) {
  const row = document.createElement("div");
  row.className = "prow";
  const label = p.money ? moneyLabelHTML(p) : `<label>${p.label}</label>`;

  if (p.select) {
    row.innerHTML = label + `<select></select>`;
    const sel = row.querySelector("select");
    p.select.forEach((o) => sel.add(new Option(o.toUpperCase().replace("_", " "), o)));
    sel.value = p.def;
    sel.onchange = () => { state.values[p.id] = sel.value; scheduleRun(0); };
    return row;
  }
  if (p.toggle) {
    row.innerHTML = label + `<div class="tgl"></div>`;
    const box = row.querySelector(".tgl");
    p.toggle.forEach((o) => {
      const b = document.createElement("button");
      b.textContent = String(o).toUpperCase();
      b.classList.toggle("on", o === p.def);
      b.onclick = () => {
        state.values[p.id] = o;
        box.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        scheduleRun(0);
      };
      box.appendChild(b);
    });
    return row;
  }

  row.innerHTML = label +
    `<input type="range" min="${p.min}" max="${p.max}" step="${p.step}" value="${p.def}">` +
    `<input class="val" value="${fmtParam(p, p.def)}">`;
  const slider = row.querySelector("input[type=range]");
  const box = row.querySelector(".val");
  slider.oninput = () => {
    const v = p.int ? Math.round(+slider.value) : +slider.value;
    state.values[p.id] = v;
    box.value = fmtParam(p, v);
    scheduleRun(200);
  };
  box.onchange = () => {
    let v = parseFloat(box.value.replace(/[%,$\s]/g, ""));
    if (Number.isNaN(v)) { box.value = fmtParam(p, state.values[p.id]); return; }
    if (p.pct) v /= 100;
    v = Math.min(p.max, Math.max(p.min, v));
    if (p.int) v = Math.round(v);
    state.values[p.id] = v;
    slider.value = v;
    box.value = fmtParam(p, v);
    scheduleRun(0);
  };
  return row;
}

/* ------------------------------- execution ----------------------------- */
function scheduleRun(delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(runCurrent, delay);
}

async function runCurrent() {
  if (!state.runPy || !state.current) return;
  const seq = ++state.seq;
  const model = state.current;
  $("#ostat").textContent = "CALCULATING…";
  $("#ostat").className = "meta calcing";
  await new Promise((r) => setTimeout(r, 15)); // let the status paint

  let payload;
  try {
    payload = JSON.parse(state.runPy(model.mn, JSON.stringify(state.values)));
  } catch (err) {
    if (seq !== state.seq) return;
    renderError(err); return;
  }
  if (seq !== state.seq) return; // superseded by newer input
  renderResults(model, payload);
  renderChart(model, payload);
  renderDoc(payload);
  $("#ostat").textContent = `${payload.calc_ms} MS`;
  $("#ostat").className = "meta";
  $("#calcms").innerHTML = `CALC <b>${payload.calc_ms} ms</b>`;
}

function renderError(err) {
  const msg = String(err).split("\n").filter((l) => l.includes("Error") || l.includes("error")).slice(-1)[0] || String(err);
  $("#ogrid").innerHTML = "";
  $("#oerr").style.display = "block";
  $("#oerr").textContent = msg;
  $("#ostat").textContent = "ERROR";
  $("#ostat").className = "meta";
}

/* ------------------------------ rendering ------------------------------ */
const PCT_KEY = /rate|return|growth|yield|alpha|confidence|premium|weight|margin|vol$|_vol|sharpe_?$/i;

function fmtValue(key, v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  if (typeof v === "string") return v.toUpperCase();
  if (Array.isArray(v)) {
    if (v.length <= 6 && v.every((x) => typeof x === "number"))
      return v.map((x) => +x.toFixed(3)).join("  ");
    return `SERIES · ${v.length} PTS`;
  }
  if (typeof v !== "number") return String(v);
  if (PCT_KEY.test(key) && Math.abs(v) <= 1.5 && !/sharpe/i.test(key))
    return (v * 100).toFixed(2) + "%";
  const abs = Math.abs(v);
  if (abs >= 1e5) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (abs >= 100) return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return +v.toFixed(4) + "";
}

function renderResults(model, payload) {
  $("#oerr").style.display = "none";
  const grid = $("#ogrid");
  grid.innerHTML = "";
  const addRow = (k, v, xtra) => {
    const tr = document.createElement("tr");
    if (xtra) tr.className = "xtra";
    const vs = fmtValue(k, v);
    const cls = typeof v === "number" && /price|value|var|cvar|return|alpha|pv|ev/i.test(k)
      ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "";
    tr.innerHTML = `<td class="k">${k.replace(/_/g, " ").toUpperCase()}</td><td class="v ${cls}">${vs}</td>`;
    grid.appendChild(tr);
  };
  Object.entries(payload.results).forEach(([k, v]) => addRow(k, v, false));
  Object.entries(payload.extras || {}).forEach(([k, v]) => {
    if (k !== "figure_error") addRow(k, v, true);
  });
  // DCF: a negative per-share value is correct arithmetic, not a fault — it
  // means enterprise value has fallen below net debt (equity holders are
  // underwater). Surface that so it reads as a signal, not a broken number.
  const eq = payload.results.equity_value;
  if (model.mn === "DCF" && typeof eq === "number" && eq < 0) {
    const ev = payload.results.enterprise_value;
    const nd = (typeof ev === "number") ? ev - eq : null;
    const note = document.createElement("tr");
    note.className = "advisory";
    note.innerHTML = `<td colspan="2">⚠ EQUITY UNDERWATER — enterprise value${
      nd !== null ? ` (${fmtValue("ev", ev)})` : ""
    } is below net debt${nd !== null ? ` (${fmtValue("net_debt", nd)})` : ""}, so intrinsic
      equity and per-share value are negative. Raise FCF / growth, or lower WACC / net debt.</td>`;
    grid.appendChild(note);
  }
  $("#output header .title").textContent = `OUTPUT — ${model.mn}`;
}

function renderChart(model, payload) {
  if (!payload.figure) { Plotly.purge("chart"); return; }
  const fig = JSON.parse(payload.figure);
  const layout = fig.layout || {};
  delete layout.template; // models emit the default light template — re-skin
  Object.keys(layout).forEach((k) => {
    if (/^([xy]axis\d*|polar)/.test(k) && layout[k] && typeof layout[k] === "object") {
      Object.assign(layout[k], {
        gridcolor: "#1d2530", zerolinecolor: "#2a3547", linecolor: "#1d2530",
        tickfont: { color: "#5c6b7d", size: 10 },
        title: { ...(layout[k].title || {}), font: { color: "#5c6b7d", size: 11 } },
      });
    }
  });
  Object.assign(layout, {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "IBM Plex Mono, monospace", color: "#c9d4e0", size: 11 },
    title: { ...(layout.title || {}), font: { color: "#53c9e0", size: 13 } },
    legend: { ...(layout.legend || {}), font: { color: "#c9d4e0", size: 10 }, bgcolor: "rgba(0,0,0,0)" },
    margin: { l: 60, r: 24, t: 46, b: 48 },
    autosize: true,
  });
  Plotly.react("chart", fig.data, layout, { responsive: true, displaylogo: false });
}

function renderDoc(payload) {
  const doc = $("#doc");
  doc.innerHTML = marked.parse(payload.explain || "");
  if (window.renderMathInElement) {
    renderMathInElement(doc, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  }
}

window.addEventListener("DOMContentLoaded", () => {
  // Auth gate: no valid session -> the login page. Sessions are created by
  // email+password, Google SSO, or the explicit guest path — all on-device.
  state.user = currentUser();
  if (!state.user) { location.replace("login.html"); return; }
  validateServerSession();   // non-blocking: revoked OTP tokens sign out
  boot();
});

/* ======================================================================== *
 * IB DESK — company PDF analyzer
 * Upload a 10-K/10-Q -> pure-Python extraction (pypdf + pdfminer.six in
 * WASM) -> auto assumptions (live US-Treasury risk-free + IB heuristics) or
 * manual overrides -> run any subset of the 10 models -> download the
 * report as PDF / Google-Docs (.docx) / Excel.
 * ======================================================================== */

const IB_MODELS = [
  "Discounted Cash Flow", "Gordon Growth Model", "Modern Portfolio Theory",
  "Value at Risk / CVaR", "Capital Asset Pricing Model", "Fama-French 3-Factor",
  "Black-Scholes-Merton", "Binomial Tree (CRR)", "Monte Carlo (GBM)",
  "Heston Stochastic Volatility",
];

/* Manual-mode overrides: every knob ManualOverrides supports. Only sliders
 * the user actually moves are sent, so untouched inputs keep the IB-bot
 * (auto) values. */
const IB_OVERRIDES = [
  { id: "risk_free_rate", label: "RISK-FREE r", min: 0, max: 0.08, step: 0.0025, def: 0.0425, pct: true },
  { id: "expected_market_return", label: "E[R MARKET]", min: 0.02, max: 0.20, step: 0.0025, def: 0.0925, pct: true },
  { id: "beta", label: "BETA β", min: -1, max: 3, step: 0.05, def: 1.0 },
  { id: "discount_rate", label: "WACC", min: 0.04, max: 0.25, step: 0.0025, def: 0.09, pct: true },
  { id: "terminal_growth", label: "TERMINAL G", min: 0, max: 0.05, step: 0.0025, def: 0.025, pct: true },
  { id: "dividend_growth", label: "DIV GROWTH", min: 0, max: 0.10, step: 0.0025, def: 0.04, pct: true },
  { id: "volatility", label: "VOL σ", min: 0.05, max: 1.0, step: 0.005, def: 0.25, pct: true },
  { id: "option_maturity", label: "OPTION T (Y)", min: 0.1, max: 5, step: 0.05, def: 1 },
  { id: "strike_ratio", label: "STRIKE / SPOT", min: 0.5, max: 1.5, step: 0.01, def: 1.0 },
  { id: "var_confidence", label: "VAR CONF", min: 0.90, max: 0.99, step: 0.005, def: 0.95, pct: true },
  { id: "var_horizon_days", label: "VAR HORIZON (D)", min: 1, max: 30, step: 1, def: 10, int: true },
  { id: "monte_carlo_paths", label: "MC PATHS", min: 10000, max: 500000, step: 10000, def: 100000, int: true },
  { id: "heston_kappa", label: "HESTON κ", min: 0.1, max: 10, step: 0.1, def: 1.5 },
  { id: "heston_theta", label: "HESTON θ", min: 0.005, max: 0.5, step: 0.005, def: 0.0625 },
  { id: "heston_xi", label: "HESTON ξ", min: 0.05, max: 1.5, step: 0.05, def: 0.3 },
  { id: "heston_rho", label: "HESTON ρ", min: -0.95, max: 0.5, step: 0.05, def: -0.6 },
];

const IB_FIELD_LABELS = {
  company_name: "COMPANY", ticker: "TICKER", fiscal_year: "FISCAL YEAR",
  revenue: "REVENUE", free_cash_flows: "FREE CASH FLOWS", net_income: "NET INCOME",
  total_debt: "TOTAL DEBT", cash_and_equivalents: "CASH & EQUIV",
  net_debt: "NET DEBT", shares_outstanding: "SHARES OUT",
  current_price: "SHARE PRICE", dividend_per_share: "DIVIDEND / SH",
  beta: "BETA", revenue_growth: "REV GROWTH", operating_margin: "OP MARGIN",
  tax_rate: "TAX RATE",
};

/* ------------------------- live market data ---------------------------- */
//: Anchor the IB desk to the selected country: baseline sovereign yield takes
//  effect IMMEDIATELY (so an upload can never run on another market's rate),
//  then /api/rates upgrades it to a live figure — US Treasury FiscalData for
//  the US, FRED/OECD 10Y govt yields elsewhere (free FRED_API_KEY), plus the
//  keyless er-api USD fix for FX context. Any fetch failure keeps the
//  curated Damodaran baseline, clearly labelled as such.
async function applyCountryToIB(c) {
  const seq = (state.ib.rateSeq = (state.ib.rateSeq || 0) + 1);
  state.ib.liveRf = c.rf;
  state.ib.rfSource = `${c.name.toUpperCase()} 10Y SOVEREIGN BASELINE (DAMODARAN)`;
  state.ib.fx = null; state.ib.fxDate = null;
  renderIBContext();
  try {
    const r = await fetch(`api/rates?cc=${c.code}`, { signal: AbortSignal.timeout(9000) });
    if (!r.ok) return;
    const j = await r.json();
    if (seq !== state.ib.rateSeq) return;   // stale — country changed again
    if (typeof j.rf === "number" && j.rf > 0 && j.rf < 0.5) {
      state.ib.liveRf = j.rf;
      state.ib.rfSource = j.rfSource || state.ib.rfSource;
      //: live yield also re-anchors the manual sliders' defaults
      applyCountryDefaults({ ...c, rf: j.rf });
    }
    if (typeof j.fx === "number") { state.ib.fx = j.fx; state.ib.fxDate = j.fxDate; }
    renderIBContext();
  } catch { /* baseline already applied */ }
}

/* --------------------------- view assembly ----------------------------- */
function selectAnalyzer() {
  state.view = "ib";
  state.current = null;
  if (isPhone()) setMobileView("inputs");
  clearTimeout(state.timer);
  if (!state.ib.selected) state.ib.selected = new Set(IB_MODELS);
  document.querySelectorAll(".mrow").forEach((r) => r.classList.toggle("active", r.dataset.mn === "IB"));
  document.querySelectorAll("#fkeys button").forEach((b) => b.classList.toggle("active", b.dataset.mn === "IB"));

  $("#tab-scen").style.display = "none";   // scenario engine is per-model
  $("#inputs header .title").textContent = "IB DESK — COMPANY PDF ANALYZER";
  $("#formula").innerHTML = "ƒ  <b>UPLOAD 10-K / 10-Q → SCRAPE → ASSUME (AUTO IB-BOT | MANUAL) → RUN MODELS → EXPORT</b>";
  $("#output header .title").textContent = "EXTRACTED DATA";
  $("#ostat").textContent = "—"; $("#ostat").className = "meta";
  $("#ogrid").innerHTML = ""; $("#oerr").style.display = "none";

  buildIBForm();
  renderIBContext();
  renderIBReport();
  setTab("chart");
  if (state.ib.liveRf === null) applyCountryToIB(state.country);
  ensureAnalyzerPackages();
}

function buildIBForm() {
  const body = $("#pform");
  body.innerHTML = "";

  // 1 · source document
  body.insertAdjacentHTML("beforeend", `<div class="ibsec">1 · SOURCE DOCUMENT</div>
    <div class="upl">
      <button id="ibupl">⬆ UPLOAD 10-K / 10-Q PDF</button>
      <span class="fname" id="ibfname">no file — any annual or quarterly filing</span>
      <input type="file" id="ibfile" accept="application/pdf,.pdf">
    </div>`);
  $("#ibupl").onclick = () => $("#ibfile").click();
  $("#ibfile").onchange = onIBUpload;

  // 2 · period basis
  body.insertAdjacentHTML("beforeend", `<div class="ibsec">2 · PERIOD BASIS</div>`);
  body.appendChild(ibToggleRow("PERIOD", ["auto", "annual", "quarterly"], state.ib.period,
    (v) => { state.ib.period = v; if (state.ib.file) onIBUpload(); }));
  body.insertAdjacentHTML("beforeend",
    `<div class="ibhint">AUTO detects 10-Q language; quarterly flows are annualised ×4 (stocks untouched).</div>`);

  // 3 · assumption engine
  body.insertAdjacentHTML("beforeend", `<div class="ibsec">3 · ASSUMPTION ENGINE</div>`);
  body.appendChild(ibToggleRow("MODE", ["auto", "manual"], state.ib.mode, (v) => {
    state.ib.mode = v; buildIBForm();
  }));
  if (state.ib.mode === "auto") {
    const c = state.country || COUNTRIES[0];
    body.insertAdjacentHTML("beforeend",
      `<div class="ibhint">IB BOT: CAPM WACC (80/20 equity-debt, +150bp credit spread), terminal g ≤ r_f
       (Gordon constraint), sector-neutral β fallback. Anchored to <b>${c.flag} ${c.name.toUpperCase()}</b>:
       ERP ${(c.erp * 100).toFixed(1)}% (Damodaran country rating), risk-free from the live 10Y
       sovereign yield (US Treasury FiscalData / FRED-OECD), baseline fallback offline.
       Change the market with the country button (top-right).</div>`);
  } else {
    body.insertAdjacentHTML("beforeend",
      `<div class="ibhint">Move a slider to override the bot — untouched inputs keep the auto value.</div>`);
    IB_OVERRIDES.forEach((p) => {
      const row = paramRow({ ...p });                 // reuse the slider factory
      const slider = row.querySelector("input[type=range]");
      const box = row.querySelector(".val");
      if (state.ib.dirty[p.id] !== undefined) {       // restore prior overrides
        slider.value = state.ib.dirty[p.id];
        box.value = fmtParam(p, state.ib.dirty[p.id]);
      }
      slider.oninput = () => {
        const v = p.int ? Math.round(+slider.value) : +slider.value;
        state.ib.dirty[p.id] = v; box.value = fmtParam(p, v);
      };
      box.onchange = () => {
        let v = parseFloat(box.value.replace(/[%,$\s]/g, ""));
        if (Number.isNaN(v)) { box.value = fmtParam(p, state.ib.dirty[p.id] ?? p.def); return; }
        if (p.pct) v /= 100;
        v = Math.min(p.max, Math.max(p.min, v)); if (p.int) v = Math.round(v);
        state.ib.dirty[p.id] = v; slider.value = v; box.value = fmtParam(p, v);
      };
      body.appendChild(row);
    });
  }

  // 4 · models in the report
  body.insertAdjacentHTML("beforeend", `<div class="ibsec">4 · MODELS IN REPORT</div>
    <div class="ckall"><button id="iball">ALL</button><button id="ibnone">NONE</button></div>
    <div class="ckgrid" id="ibck"></div>`);
  const grid = $("#ibck");
  IB_MODELS.forEach((name) => {
    const lab = document.createElement("label");
    lab.className = "ck";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = state.ib.selected.has(name); cb.dataset.model = name;
    cb.onchange = () => { cb.checked ? state.ib.selected.add(name) : state.ib.selected.delete(name); };
    lab.appendChild(cb); lab.appendChild(document.createTextNode(name));
    grid.appendChild(lab);
  });
  $("#iball").onclick = () => { state.ib.selected = new Set(IB_MODELS); grid.querySelectorAll("input").forEach((c) => (c.checked = true)); };
  $("#ibnone").onclick = () => { state.ib.selected.clear(); grid.querySelectorAll("input").forEach((c) => (c.checked = false)); };

  // 5 · run + export
  body.insertAdjacentHTML("beforeend", `<div class="ibsec">5 · RUN & EXPORT</div>
    <div class="ibactions">
      <button id="ibrun" disabled>▶ RUN REPORT &lt;GO&gt;</button>
      <button class="ibexp" id="exp-pdf" disabled>⬇ PDF</button>
      <button class="ibexp" id="exp-docx" disabled>⬇ GOOGLE DOCS (.DOCX)</button>
      <button class="ibexp" id="exp-xlsx" disabled>⬇ EXCEL</button>
    </div>
    <div class="ibhint">The .docx opens directly in Google Docs (File → Open, or drag into
    docs.google.com) with full formatting — no account access required by this site.</div>`);
  $("#ibrun").onclick = runIBReport;
  ["pdf", "docx", "xlsx"].forEach((f) => { $(`#exp-${f}`).onclick = () => exportIB(f); });
  syncIBButtons();
}

function ibToggleRow(label, options, current, onPick) {
  const row = document.createElement("div");
  row.className = "prow";
  row.innerHTML = `<label>${label}</label><div class="tgl"></div>`;
  const box = row.querySelector(".tgl");
  options.forEach((o) => {
    const b = document.createElement("button");
    b.textContent = o.toUpperCase();
    b.classList.toggle("on", o === current);
    b.onclick = () => { box.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); onPick(o); };
    box.appendChild(b);
  });
  return row;
}

function syncIBButtons() {
  if (state.view !== "ib") return;
  $("#ibrun").disabled = !(state.ib.pkgsReady && state.ib.extracted);
  // Exports render from the Python-side report object; a report restored
  // from saved history is display-only until RUN recomputes it in-runtime.
  const exportable = !!state.ib.report && !state.ib.report.restored;
  ["pdf", "docx", "xlsx"].forEach((f) => { $(`#exp-${f}`).disabled = !exportable; });
}

/* -------------------- analyzer runtime dependencies -------------------- */
function ensureAnalyzerPackages() {
  // Single shared promise: concurrent callers (view open + eager upload)
  // all await the same install instead of racing past the guard.
  if (!state.ib.installPromise) {
    state.ib.installPromise = (async () => {
      ibStatus("INSTALLING PDF/EXPORT BACKENDS…");
      await state.pyodide.loadPackage(["lxml"]);      // wheel from the Pyodide dist
      await state.pyodide.runPythonAsync(
        "import micropip\n" +
        "await micropip.install(['pypdf', 'pdfminer.six', 'reportlab', 'openpyxl', 'python-docx'])"
      );
      state.ib.fns = {
        analyze: state.pyodide.runPython("web_bridge.analyze_pdf"),
        run: state.pyodide.runPython("web_bridge.run_report"),
        exportR: state.pyodide.runPython("web_bridge.export_report"),
        restore: state.pyodide.runPython("web_bridge.restore_extraction"),
        override: state.pyodide.runPython("web_bridge.override_field"),
        assumed: state.pyodide.runPython("web_bridge.get_assumed"),
      };
      state.ib.pkgsReady = true;
      ibStatus("READY — UPLOAD A FILING");
      $("#ostat").className = "meta";
      syncIBButtons();
    })().catch((err) => {
      state.ib.installPromise = null;                 // allow retry
      ibStatus("BACKEND INSTALL FAILED — RETRY BY RE-OPENING IB", true);
      console.error(err);
      throw err;
    });
  }
  return state.ib.installPromise;
}

function ibStatus(msg, isError) {
  if (state.view !== "ib") return;
  $("#ostat").textContent = msg;
  $("#ostat").className = "meta" + (isError ? "" : " calcing");
  if (isError) { $("#oerr").style.display = "block"; $("#oerr").textContent = msg; }
}

/* ------------------------------ actions -------------------------------- */
async function onIBUpload() {
  const file = $("#ibfile").files[0];
  if (!file) return;
  // Plan gate: uploads are the metered unit once billing is live. The gate
  // fails open on network hiccups — availability over strict metering.
  const gate = await uploadGate();
  if (!gate.allowed) {
    ibStatus(gate.reason, true);
    $("#ibfile").value = "";
    if (gate.upgrade) openMenuTab("plan");
    return;
  }
  state.ib.file = file;
  $("#ibfname").textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
  try { await ensureAnalyzerPackages(); } catch { return; }
  ibStatus("SCRAPING PDF (PYPDF → PDFMINER CASCADE)…");
  await new Promise((r) => setTimeout(r, 25));
  try {
    const buf = new Uint8Array(await file.arrayBuffer());
    const out = JSON.parse(state.ib.fns.analyze(buf, state.ib.period));
    if (!out.ok) throw new Error(out.error);
    state.ib.extracted = out;
    state.ib.report = null;
    state.ib.userSet = new Set();       // fresh filing — no manual overrides yet
    renderIBExtracted();
    ibStatus(`EXTRACTED · ${out.period.toUpperCase()} BASIS · ${out.backends.join("+") || "no backend"}`);
    $("#ostat").className = "meta";
    if (gate.metered) consumeUpload();   // count only successful analyses
  } catch (err) {
    state.ib.extracted = null;
    ibStatus("EXTRACTION FAILED: " + String(err).slice(0, 160), true);
  }
  syncIBButtons();
}

//: Monetary extraction fields — labelled with the selected market's currency
//  (the filing's own currency; pick the matching country for coherent rates).
const IB_MONEY_FIELDS = new Set([
  "revenue", "free_cash_flows", "net_income", "total_debt",
  "cash_and_equivalents", "net_debt", "current_price", "dividend_per_share",
]);
//: Fields with no direct manual override: text/derived/synthesised.
const IB_NO_OVERRIDE = new Set([
  "company_name", "ticker", "fiscal_year", "net_debt", "free_cash_flows",
]);

function renderIBExtracted() {
  const grid = $("#ogrid");
  grid.innerHTML = "";
  $("#oerr").style.display = "none";
  const out = state.ib.extracted;
  if (!out) { renderIBContext(); return; }
  const assumed = out.assumed || {};
  const userSet = state.ib.userSet || (state.ib.userSet = new Set());

  Object.entries(IB_FIELD_LABELS).forEach(([key, label]) => {
    const value = out.fields[key];
    const isMissing = value === null || value === undefined || (Array.isArray(value) && !value.length);
    const tr = document.createElement("tr");
    tr.dataset.key = key;
    const ccyTag = IB_MONEY_FIELDS.has(key) ? ` <span class="ccytag">${ccySymbol().trim()}</span>` : "";

    let shown;
    if (!isMissing) {
      //: USER = manually entered; click the badge to change or revert to auto.
      shown = userSet.has(key)
        ? `${fmtValue(key, value)} <span class="badge user clickable" data-key="${key}"
             title="manually set — click to edit or revert to auto">USER</span>`
        : `${fmtValue(key, value)} <span class="badge found">PDF</span>`;
    } else {
      //: Transparency: show the exact number the bot will assume, and let the
      //  user click AUTO-ASSUMED to take that field over manually.
      const av = assumed[key];
      const avTxt = av === null || av === undefined ? "—" : fmtValue(key, av);
      const editable = !IB_NO_OVERRIDE.has(key);
      shown = `${avTxt} <span class="badge assumed${editable ? " clickable" : ""}" data-key="${key}"
        title="${editable ? "click to switch off auto-assumption and enter your own value"
                          : key === "net_debt" ? "derived from total debt − cash; edit those instead"
                          : key === "free_cash_flows" ? "synthesised from revenue × margin × growth"
                          : "not used by the models"}">AUTO-ASSUMED</span>`;
    }
    tr.innerHTML = `<td class="k">${label}${ccyTag}</td><td class="v">${shown}</td>`;
    grid.appendChild(tr);
  });
  grid.querySelectorAll(".badge.clickable").forEach((b) => {
    b.onclick = () => openIBFieldEditor(b.dataset.key);
  });
  renderIBContext();
}

//: Inline editor for one extracted field: input prefilled with the current /
//  assumed number, SET commits the manual value, AUTO hands it back to the bot.
function openIBFieldEditor(key) {
  const tr = $(`#ogrid tr[data-key="${key}"]`);
  if (!tr || !state.ib.fns) return;
  const out = state.ib.extracted;
  const cur = out.fields[key] ?? (out.assumed || {})[key];
  const isPct = PCT_KEY.test(key);
  const prefill = cur === null || cur === undefined ? ""
    : isPct && Math.abs(cur) <= 1.5 ? +(cur * 100).toFixed(4) : cur;
  tr.querySelector(".v").innerHTML =
    `<input class="ibov" value="${prefill}" placeholder="${isPct ? "e.g. 4.6 (%)" : "number"}">
     <button class="ibov-set">SET</button><button class="ibov-auto"
       title="revert to the bot's auto-assumption">↺ AUTO</button>`;
  const input = tr.querySelector(".ibov");
  const commit = (revert) => {
    let v = null;
    if (!revert) {
      v = parseFloat(String(input.value).replace(/[%,$\s]/g, ""));
      if (Number.isNaN(v)) { input.focus(); return; }
      if (isPct && Math.abs(v) > 1.5) v /= 100;   // "4.6" typed for 4.6%
    }
    commitIBField(key, v);
  };
  tr.querySelector(".ibov-set").onclick = () => commit(false);
  tr.querySelector(".ibov-auto").onclick = () => commit(true);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commit(false);
    if (e.key === "Escape") renderIBExtracted();
  });
  input.focus(); input.select();
}

async function commitIBField(key, valueOrNull) {
  try {
    const res = JSON.parse(state.ib.fns.override(key, valueOrNull));
    if (!res.ok) { ibStatus(res.error, true); return; }
    state.ib.extracted.fields = res.fields;
    state.ib.extracted.assumed = res.assumed;
    if (valueOrNull === null) state.ib.userSet.delete(key);
    else state.ib.userSet.add(key);
    state.ib.report = null;                     // assumptions changed
    renderIBExtracted();
    syncIBButtons();
    ibStatus(valueOrNull === null
      ? `${IB_FIELD_LABELS[key] || key} BACK TO AUTO — RE-RUN THE REPORT`
      : `${IB_FIELD_LABELS[key] || key} SET MANUALLY — RE-RUN THE REPORT`);
    $("#ostat").className = "meta";
  } catch (err) {
    ibStatus("OVERRIDE FAILED: " + String(err).slice(0, 120), true);
  }
}

function renderIBContext() {
  if (state.view !== "ib") return;
  const grid = $("#ogrid");
  const c = state.country || COUNTRIES[0];
  let ctx = $("#ibctx");
  if (!ctx) { ctx = document.createElement("tr"); ctx.id = "ibctx"; }
  const rf = state.ib.liveRf;
  ctx.innerHTML = `<td class="k">RISK-FREE (${c.code})</td><td class="v">${
    rf !== null && rf !== undefined ? (rf * 100).toFixed(3) + "%" : "—"
  } <span class="badge live">${state.ib.rfSource || "FETCHING…"}</span></td>`;
  grid.appendChild(ctx);

  let mkt = $("#ibmkt");
  if (!mkt) { mkt = document.createElement("tr"); mkt.id = "ibmkt"; }
  const fx = state.ib.fx && c.ccy !== "USD"
    ? ` · FX 1 USD = ${state.ib.fx.toFixed(3)} ${c.ccy}` : "";
  mkt.innerHTML = `<td class="k">MARKET / ERP</td><td class="v">${c.flag} ${
    c.name.toUpperCase()} · ${c.ccy} · ERP ${(c.erp * 100).toFixed(1)}%${fx}
    <span class="badge live">ALL ASSUMPTIONS ANCHOR HERE</span></td>`;
  grid.appendChild(mkt);
}

async function runIBReport() {
  if (!state.ib.extracted) return;
  if (!state.ib.selected.size) { ibStatus("SELECT AT LEAST ONE MODEL", true); return; }
  ibStatus(`RUNNING ${state.ib.selected.size} MODELS…`);
  await new Promise((r) => setTimeout(r, 25));
  try {
    const c = state.country || COUNTRIES[0];
    const payload = {
      mode: state.ib.mode,
      selected: [...state.ib.selected],
      live_rf: state.ib.liveRf,
      rf_source: state.ib.rfSource,
      erp: c.erp,                       // country equity risk premium (Damodaran)
      country: c.name, country_code: c.code,
      currency: c.ccy, currency_symbol: ccySymbol(),
      fx_per_usd: state.ib.fx,
      overrides: state.ib.mode === "manual" ? state.ib.dirty : {},
    };
    const out = JSON.parse(state.ib.fns.run(JSON.stringify(payload)));
    if (!out.ok) throw new Error(out.error);
    state.ib.report = out;
    renderIBReport();
    recordHistory();          // persist this company's analysis to menu history
    setTab("chart");
    if (isPhone()) setMobileView("viz");   // the report renders in ANALYTICS
    const nErr = Object.keys(out.errors).length;
    ibStatus(`REPORT READY · ${out.summary.length} MODELS${nErr ? ` · ${nErr} FAILED` : ""}`);
    $("#ostat").className = "meta";
  } catch (err) {
    ibStatus("RUN FAILED: " + String(err).slice(0, 160), true);
  }
  syncIBButtons();
}

function renderIBReport() {
  const el = $("#report");
  const out = state.ib.report;
  if (!out) {
    el.innerHTML = `<h2>REPORT</h2><p style="color:var(--text-dim);font-size:12px">
      Upload a filing, choose the assumption engine and models, then ▶ RUN REPORT.
      The result lands here with PDF / Google-Docs / Excel downloads.</p>`;
    renderIBDoc();
    return;
  }
  const company = state.ib.extracted?.fields?.company_name || "UPLOADED COMPANY";
  let html = `<h2>REPORT — ${company.toUpperCase()} · ${out.mode.toUpperCase()} MODE</h2>
    <table><tr><th>MODEL</th><th>HEADLINE RESULT</th><th>STATUS</th></tr>`;
  out.summary.forEach((row) => {
    const ok = !String(row.Status).toLowerCase().includes("error");
    html += `<tr class="${ok ? "" : "err"}"><td>${row.Model}</td>
      <td class="num">${row["Headline result"]}</td>
      <td class="${ok ? "stat-ok" : "stat-err"}">${row.Status}</td></tr>`;
  });
  html += "</table><h2>ASSUMPTIONS (MARKET CONTEXT)</h2><table>";
  Object.entries(out.market_context).forEach(([key, value]) => {
    html += `<tr><td class="k">${key.replace(/_/g, " ").toUpperCase()}</td>
      <td class="num">${fmtValue(key, value)}</td></tr>`;
  });
  html += `</table><p style="color:var(--text-dim);font-size:11px">RISK-FREE SOURCE: ${out.rf_source}</p>`;
  el.innerHTML = html;
  renderIBDoc();
}

function renderIBDoc() {
  if (state.view !== "ib") return;
  const out = state.ib.report;
  const doc = $("#doc");
  if (!out) { doc.innerHTML = "<p>Run a report to see the assumption rationale.</p>"; return; }
  let md = "## Assumption rationale (IB bot audit trail)\n\n| Assumption | Basis |\n|---|---|\n";
  Object.entries(out.rationale).forEach(([key, text]) => { md += `| ${key} | ${text} |\n`; });
  if (Object.keys(out.errors).length) {
    md += "\n## Models not run\n\n";
    Object.entries(out.errors).forEach(([name, err]) => { md += `- **${name}**: ${err}\n`; });
  }
  doc.innerHTML = marked.parse(md);
}

async function exportIB(fmt) {
  ibStatus(`RENDERING ${fmt.toUpperCase()}…`);
  await new Promise((r) => setTimeout(r, 25));
  try {
    const out = JSON.parse(state.ib.fns.exportR(fmt));
    if (!out.ok) throw new Error(out.error);
    const bytes = Uint8Array.from(atob(out.b64), (c) => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: out.mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = out.filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    ibStatus(`SAVED ${out.filename}`);
    $("#ostat").className = "meta";
  } catch (err) {
    ibStatus("EXPORT FAILED: " + String(err).slice(0, 160), true);
  }
}

/* ======================================================================== *
 * COUNTRY SELECTOR — repoint the cost of capital to the chosen market.
 * ======================================================================== */
function applyCountryDefaults(c) {
  // Rewrite the default of every risk-free/rate input, and derive a market
  // return (rf + ERP) for CAPM, so a freshly opened model starts from the
  // selected country's numbers. Clamp into each param's slider range.
  const clamp = (p, v) => Math.min(p.max, Math.max(p.min, v));
  MODELS.forEach((m) => m.params.forEach((p) => {
    if (RF_PARAM_IDS.has(p.id)) p.def = clamp(p, c.rf);
    if (p.id === "expected_market_return") p.def = clamp(p, c.rf + c.erp);
  }));
  // IB desk auto-mode: the market return baseline follows the country too.
  const erpOv = IB_OVERRIDES.find((o) => o.id === "expected_market_return");
  if (erpOv) erpOv.def = Math.min(erpOv.max, Math.max(erpOv.min, c.rf + c.erp));
  const rfOv = IB_OVERRIDES.find((o) => o.id === "risk_free_rate");
  if (rfOv) rfOv.def = Math.min(rfOv.max, Math.max(rfOv.min, c.rf));
}

function initCountry() {
  const drop = $("#country-drop");
  drop.innerHTML = `<div class="cdhead">SELECT MARKET — RISK-FREE &amp; COST OF CAPITAL FOLLOW</div>`;
  COUNTRIES.forEach((c) => {
    const row = document.createElement("div");
    row.className = "crow"; row.dataset.code = c.code;
    row.innerHTML = `<span class="cflag">${c.flag}</span>
      <span><div class="cname">${c.name}</div><div class="cmkt">${c.market} · ${c.ccy}</div></span>
      <span class="crf">${(c.rf * 100).toFixed(2)}%<small>${c.live ? "10Y · LIVE" : "10Y SOV"}</small></span>`;
    row.onclick = () => { selectCountry(c.code); closeCountry(); };
    drop.appendChild(row);
  });
  $("#country-btn").onclick = (e) => {
    e.stopPropagation();
    drop.classList.contains("on") ? closeCountry() : openCountry();
  };
  document.addEventListener("click", (e) => {
    if (drop.classList.contains("on") && !drop.contains(e.target) && e.target.id !== "country-btn")
      closeCountry();
  });
  syncCountryUI();
}
function openCountry() {
  const b = $("#country-btn").getBoundingClientRect();
  const drop = $("#country-drop");
  drop.style.top = (b.bottom + 6) + "px";
  drop.style.right = Math.max(12, window.innerWidth - b.right) + "px";
  drop.classList.add("on");
  syncCountryUI();
}
const closeCountry = () => $("#country-drop").classList.remove("on");

function syncCountryUI() {
  const c = state.country; if (!c) return;
  $("#country-btn .ccode").textContent = c.code;
  $("#cstatus").innerHTML = `MARKET <b>${c.flag} ${c.name.toUpperCase()} · ${c.market}</b>`;
  document.querySelectorAll(".crow").forEach((r) => r.classList.toggle("on", r.dataset.code === c.code));
}

function selectCountry(code) {
  const c = COUNTRIES.find((x) => x.code === code);
  if (!c) return;
  state.country = c;
  try { localStorage.setItem(LS_COUNTRY, code); } catch { /* best-effort */ }
  applyCountryDefaults(c);
  syncCountryUI();
  // Repoint the IB desk to this market: baseline instantly, live yield + FX
  // from /api/rates async. Every market now gets its own rate — never US-only.
  applyCountryToIB(c);
  // A report computed under the previous market's rates is now stale.
  if (state.ib.report && !state.ib.report.restored) {
    state.ib.report = null;
    if (state.view === "ib") { renderIBReport(); ibStatus("MARKET CHANGED — RE-RUN THE REPORT"); }
  }
  // Re-render whatever is on screen so the new defaults take effect.
  if (state.view === "model" && state.current) selectModel(state.current.mn);
  else if (state.view === "ib" && state.ib.mode === "manual") buildIBForm();
}

/* ======================================================================== *
 * HAMBURGER MENU — usage guide, model briefs, saved company history.
 * ======================================================================== */
const MODEL_BRIEFS = [
  { mn: "DCF", nm: "Discounted Cash Flow", cat: "Valuation",
    desc: "Projects a company's future free cash flows and discounts them to today at the WACC, adding a terminal value, to estimate intrinsic enterprise and per-share value.",
    use: "Best when you have a view on cash-flow growth and want a fundamentals-based fair value for a profitable, cash-generative company." },
  { mn: "GG", nm: "Gordon Growth (DDM)", cat: "Valuation",
    desc: "Values a stock as its next dividend divided by the gap between required return and a constant perpetual dividend growth rate.",
    use: "Best for stable, mature dividend payers (utilities, consumer staples, REITs) where payouts grow at a steady rate." },
  { mn: "MPT", nm: "Modern Portfolio Theory", cat: "Portfolio",
    desc: "Finds the mix of assets that minimises risk for a given return, tracing the efficient frontier and the tangency (max-Sharpe) portfolio.",
    use: "Best when you are allocating across several assets and want the risk-optimal weights rather than valuing a single security." },
  { mn: "VAR", nm: "Value at Risk / CVaR", cat: "Risk",
    desc: "Estimates the loss a portfolio could suffer over a horizon at a confidence level (VaR) and the average loss beyond it (CVaR / expected shortfall).",
    use: "Best for sizing downside risk and setting risk limits — 'how much could I lose on a bad day?' — not for valuation." },
  { mn: "CAPM", nm: "Capital Asset Pricing Model", cat: "Equity / Factor",
    desc: "Prices an asset's expected return as the risk-free rate plus beta times the market risk premium.",
    use: "Best for a quick cost-of-equity or required return when you know the stock's beta and a market premium." },
  { mn: "FF3", nm: "Fama-French 3-Factor", cat: "Equity / Factor",
    desc: "Extends CAPM with size (SMB) and value (HML) factors, regressing returns on real Ken French factor history to recover exposures and alpha.",
    use: "Best when CAPM feels too simple and you want to know how much return comes from size / value tilts versus true skill (alpha)." },
  { mn: "BSM", nm: "Black-Scholes-Merton", cat: "Derivatives",
    desc: "Closed-form price for a European option under constant volatility, with the full Greeks.",
    use: "Best for fast, exact pricing and hedging of vanilla European options; the industry baseline for option value and sensitivities." },
  { mn: "CRR", nm: "Binomial Tree (CRR)", cat: "Derivatives",
    desc: "Prices options on a recombining up/down price tree, converging to Black-Scholes as steps increase.",
    use: "Best when you need American-style early exercise or discrete dividends that the closed-form Black-Scholes can't handle." },
  { mn: "MC", nm: "Monte Carlo (GBM)", cat: "Derivatives",
    desc: "Simulates thousands of price paths and averages the discounted payoff, using antithetic variates to cut variance.",
    use: "Best for path-dependent or exotic payoffs, or when you want a flexible engine that handles almost any payoff structure." },
  { mn: "HES", nm: "Heston Stochastic Vol", cat: "Derivatives",
    desc: "Prices options with volatility that is itself random and mean-reverting, capturing the volatility smile/skew real markets show.",
    use: "Best when constant-volatility models misprice — deep in/out-of-the-money options and markets with a pronounced skew." },
];

function initMenu() {
  $("#burger").onclick = openMenu;
  $("#menu-close").onclick = closeMenu;
  $("#menu-backdrop").onclick = closeMenu;
  $("#menu-signout").onclick = (e) => { e.preventDefault(); signOut(); };
  document.querySelectorAll(".mtab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".mtab").forEach((x) => x.classList.toggle("on", x === t));
      renderMenuTab(t.dataset.tab);
    };
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeMenu(); closeCountry(); }
  });
  renderMenuTab("guide");
}
function openMenu() { $("#menu").classList.add("on"); $("#menu-backdrop").classList.add("on"); }
function closeMenu() { $("#menu").classList.remove("on"); $("#menu-backdrop").classList.remove("on"); }

function renderMenuTab(tab) {
  const body = $("#menu-body");
  if (tab === "guide") return renderGuide(body);
  if (tab === "models") return renderBriefs(body);
  if (tab === "history") return renderHistoryTab(body);
  if (tab === "plan") return renderPlanTab(body);
}

//: Open the drawer directly on a named tab (plan chip, upgrade prompts).
function openMenuTab(tab) {
  document.querySelectorAll(".mtab").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  renderMenuTab(tab);
  openMenu();
}

function renderGuide(body) {
  const steps = [
    ["1", "<b>Pick your market.</b> Use the country button (top-right) to choose from the 15 largest equity markets. The risk-free rate, market return and cost of capital across every model instantly re-anchor to that country, and every monetary input (spot, strike, FCF, dividend, portfolio, net debt) relabels to that market's currency — no more US-only, dollar-only assumptions."],
    ["2", "<b>Choose a model.</b> Click a row in the MODELS rail, press its function key (F1–F10), or type its mnemonic in the amber command bar and hit &lt;GO&gt; (e.g. type <b>BSM</b> ⏎). See the MODELS tab here for what each one is best for."],
    ["3", "<b>Drive the inputs.</b> Every parameter is a slider paired with an editable field. Move either and the model recalculates live — no run button. Percentages are entered as percentages (e.g. 5 = 5%)."],
    ["4", "<b>Read the output.</b> OUTPUT shows the headline results (green = positive, red = negative). The ANALYTICS panel has a CHART tab (interactive Plotly) and a DOC tab explaining the math with formulas."],
    ["5", "<b>Stress-test it (SCEN tab).</b> Open SCEN in the ANALYTICS panel to run the scenario &amp; sensitivity engine: save (or ⚡ auto-seed) BEAR / BASE / BULL assumption sets and compare them side-by-side, run a tornado chart ranking which input moves your answer most, and sweep any two inputs in a colour-coded 7×7 sensitivity grid. Every cell is a real in-runtime model run — no approximations."],
    ["6", "<b>Analyse a real company (IB DESK).</b> Type <b>IB</b> ⏎ or click IB DESK. Upload a 10-K/10-Q PDF — it scrapes the financials, fills any gaps automatically (auto IB-bot mode) or lets you set them by hand (manual mode), runs the models you tick, and exports a report as PDF / Google-Docs / Excel."],
    ["7", "<b>Keep your work.</b> Every company you run through the IB desk is saved to HISTORY here — reopen or remove any past analysis. Your market choice and history persist on this device."],
    ["8", "<b>Your account.</b> Sign in with email, Google, or explore as a guest (bottom-right shows who's signed in; SIGN OUT is at the bottom of this menu). History is kept per account, and credentials never leave this device — passwords are hashed locally, there's no server database."],
  ];
  body.innerHTML = `<h3>HOW TO USE THIS TERMINAL</h3>` +
    steps.map(([n, t]) => `<div class="guide-step"><div class="num">${n}</div><div class="txt">${t}</div></div>`).join("") +
    `<h3>WHICH MODEL SHOULD I USE?</h3>
     <div class="guide-step"><div class="txt" style="color:var(--text-dim)">
       Open the <b style="color:var(--cyan)">MODELS</b> tab for a plain-English brief on each technique and the situation it fits best —
       so you can match the tool to your question (value a company, size risk, price an option, allocate a portfolio).
     </div></div>
     <h3>SUPPORT</h3>
     <div class="guide-step"><div class="txt" style="color:var(--text-dim)">
       For assistance, or to request complimentary access on behalf of an individual or
       organisation, please write to <a href="mailto:finmodels10@gmail.com" style="color:var(--cyan)">finmodels10@gmail.com</a>.
       To reach the founder directly, write to <a href="mailto:doneriaidhant@gmail.com" style="color:var(--cyan)">doneriaidhant@gmail.com</a>.
       All enquiries are reviewed personally and answered within <b style="color:var(--text)">48 business hours</b>.
     </div></div>`;
}

function renderBriefs(body) {
  body.innerHTML = `<h3>THE 10 MODELS — WHAT EACH IS BEST FOR</h3>` +
    MODEL_BRIEFS.map((b) => `
      <div class="brief">
        <div class="bh"><span class="bmn">${b.mn}</span><span class="bnm">${b.nm}</span><span class="bcat">${b.cat}</span></div>
        <div class="bdesc">${b.desc}</div>
        <div class="buse"><b>Best for:</b> ${b.use}</div>
        <button class="bopen" data-mn="${b.mn}">OPEN ${b.mn} →</button>
      </div>`).join("");
  body.querySelectorAll(".bopen").forEach((btn) => {
    btn.onclick = () => { selectModel(btn.dataset.mn); closeMenu(); };
  });
}

function renderHistoryTab(body) {
  const list = loadHistory();
  if (!list.length) {
    body.innerHTML = `<h3>SAVED COMPANY ANALYSES</h3>
      <p class="hist-empty">No saved analyses yet.<br><br>
      Run a company through the <b style="color:var(--cyan)">IB DESK</b> (type <b style="color:var(--amber)">IB</b> ⏎,
      upload a 10-K/10-Q and press RUN REPORT) and it will be saved here automatically —
      so you can reopen the results for any company you've analysed.</p>`;
    return;
  }
  const who = state.user ? String(state.user.name || state.user.uid).toUpperCase() : "GUEST";
  body.innerHTML = `<h3>SAVED COMPANY ANALYSES · ${list.length} — ${who}</h3>
    <div class="hist-actions"><button id="histclear">CLEAR ALL</button></div>` +
    list.map((h, i) => `
      <div class="hist-item">
        <div class="hmeta">
          <div class="hco">${(h.company || "UNTITLED").toUpperCase()}</div>
          <div class="hsub">${h.mode ? h.mode.toUpperCase() + " · " : ""}${h.nModels} MODELS · ${h.country || ""} · ${new Date(h.ts).toLocaleString()}</div>
        </div>
        <button class="hload" data-i="${i}">OPEN</button>
        <button class="hdel" data-i="${i}">✕</button>
      </div>`).join("");
  body.querySelector("#histclear").onclick = () => { saveHistory([]); renderHistoryTab(body); };
  body.querySelectorAll(".hload").forEach((b) => { b.onclick = () => loadHistoryItem(+b.dataset.i); });
  body.querySelectorAll(".hdel").forEach((b) => {
    b.onclick = () => { const l = loadHistory(); l.splice(+b.dataset.i, 1); saveHistory(l); renderHistoryTab(body); };
  });
}

//: Snapshot the just-run IB analysis into history (most-recent first, deduped
//  by company name so re-running a company updates its entry).
function recordHistory() {
  if (!state.ib.report || !state.ib.extracted) return;
  const company = state.ib.extracted.fields.company_name || "Untitled company";
  const entry = {
    company, ts: Date.now(), mode: state.ib.report.mode,
    country: state.country ? state.country.code : "",
    nModels: state.ib.report.summary.length,
    extracted: state.ib.extracted, report: state.ib.report,
  };
  const list = loadHistory().filter((h) => (h.company || "").toLowerCase() !== company.toLowerCase());
  list.unshift(entry);
  saveHistory(list);
}

async function loadHistoryItem(i) {
  const h = loadHistory()[i];
  if (!h) return;
  closeMenu();
  if (h.report && h.report.mode) state.ib.mode = h.report.mode;   // before form build
  selectAnalyzer();                 // switch to the IB desk view
  state.ib.extracted = h.extracted;
  state.ib.report = { ...h.report, restored: true };  // display-only until re-run
  state.ib.userSet = new Set();     // snapshot restore — overrides start clean
  renderIBExtracted();
  renderIBReport();
  setTab("chart");
  syncIBButtons();
  // Rehydrate the Python-side extraction so ▶ RUN (and then exports) work
  // without re-uploading the PDF — the report shown meanwhile is the snapshot.
  try {
    await ensureAnalyzerPackages();
    const out = JSON.parse(state.ib.fns.restore(
      JSON.stringify(h.extracted.fields), h.extracted.period || "annual"));
    if (!out.ok) throw new Error(out.error);
    //: Older snapshots pre-date the assumed-value preview — backfill it so the
    //  AUTO-ASSUMED rows show their numbers (and become editable) here too.
    if (!state.ib.extracted.assumed) {
      try {
        const ap = JSON.parse(state.ib.fns.assumed());
        if (ap.ok) { state.ib.extracted.assumed = ap.assumed; renderIBExtracted(); }
      } catch { /* preview only — snapshot still renders */ }
    }
    ibStatus(`RESTORED · ${(h.company || "").toUpperCase()} — SNAPSHOT SHOWN · PRESS ▶ RUN TO RECOMPUTE & EXPORT`);
    $("#ostat").className = "meta";
  } catch (err) {
    ibStatus("RESTORE INCOMPLETE — RE-UPLOAD THE FILING TO RE-RUN (" +
             String(err).slice(0, 80) + ")", true);
  }
  syncIBButtons();
}

/* ======================================================================== *
 * SCENARIO & SENSITIVITY ENGINE — bear/base/bull assumption sets compared
 * side-by-side, a tornado chart ranking which inputs move the headline
 * output most, and a two-way sensitivity grid. Every number comes from
 * re-running the *actual* Python model in-runtime with perturbed inputs —
 * no linearisation, no approximation.
 * ======================================================================== */

//: The single output each model is judged by when stressing assumptions.
//  `worse` says which direction of that metric is the adverse (bear) one.
const SCEN_HEADLINE = {
  DCF:  { key: "price_per_share", label: "VALUE / SHARE",   worse: "down" },
  GG:   { key: "price",           label: "INTRINSIC PRICE", worse: "down" },
  MPT:  { key: "tangency_sharpe", label: "TANGENCY SHARPE", worse: "down" },
  VAR:  { key: "var",             label: "VALUE AT RISK",   worse: "up" },
  CAPM: { key: "expected_return", label: "EXPECTED RETURN", worse: "down" },
  FF3:  { key: "alpha",           label: "ALPHA (MO)",      worse: "down" },
  BSM:  { key: "price",           label: "OPTION PRICE",    worse: "down" },
  CRR:  { key: "price",           label: "OPTION PRICE",    worse: "down" },
  MC:   { key: "price",           label: "OPTION PRICE",    worse: "down" },
  HES:  { key: "price",           label: "OPTION PRICE",    worse: "down" },
};

//: Inputs excluded from stressing: numerical-precision knobs and measurement
//  settings, not economic drivers (shocking MC path count is meaningless).
const SCEN_EXCLUDE = new Set(["n_sims", "n_steps", "window", "confidence", "horizon_days"]);

const LS_SCEN = () => "finmodels.scenarios." + (state.user ? state.user.uid : "guest");
const SLOTS = ["bear", "base", "bull"];

//: Economic inputs eligible for scenario shifts / tornado / grid scans.
const scenParams = (model) =>
  model.params.filter((p) => !p.select && !p.toggle && !SCEN_EXCLUDE.has(p.id));

const scenClamp = (p, v) => {
  v = Math.min(p.max, Math.max(p.min, v));
  return p.int ? Math.round(v) : v;
};

//: Relative shift with an absolute floor so near-zero inputs still move.
function scenShift(p, v, frac, dir) {
  const dv = Math.max(Math.abs(v) * frac, (p.max - p.min) * frac * 0.25);
  return scenClamp(p, v + dir * dv);
}

function loadScenStore() {
  try { return JSON.parse(localStorage.getItem(LS_SCEN()) || "{}"); } catch { return {}; }
}
function saveScenSlot(mn, slot, values) {
  const store = loadScenStore();
  store[mn] = store[mn] || {};
  store[mn][slot] = { ...values };
  try { localStorage.setItem(LS_SCEN(), JSON.stringify(store)); } catch { /* quota */ }
}
const scenSlots = (mn) => loadScenStore()[mn] || {};

//: One in-runtime model evaluation -> headline number (or null on failure).
function scenEval(mn, values) {
  try {
    const out = JSON.parse(state.runPy(mn, JSON.stringify(values)));
    const v = out.results[SCEN_HEADLINE[mn].key];
    return typeof v === "number" ? v : null;
  } catch { return null; }
}

//: Full evaluation for the comparison table (headline + scalar results).
function scenEvalFull(mn, values) {
  try { return JSON.parse(state.runPy(mn, JSON.stringify(values))).results; }
  catch { return null; }
}

const scenYield = () => new Promise((r) => setTimeout(r, 0));

function scenStat(msg, busy) {
  const el = $("#scen-stat");
  if (el) { el.textContent = msg; el.classList.toggle("busy", !!busy); }
}

function scenButtons(disabled) {
  state.scen.busy = disabled;
  document.querySelectorAll("#scen button").forEach((b) => (b.disabled = disabled));
}

/* ------------------------------ panel ---------------------------------- */
function resetScenPanel() {
  state.scen.built = null;
  const scen = $("#scen");
  if (!scen) return;
  const tornado = document.getElementById("scen-tornado");
  if (tornado) Plotly.purge(tornado);   // release the old chart before wiping
  scen.innerHTML = "";
}

function buildScenPanel() {
  const model = state.current;
  if (!model || state.scen.built === model.mn) return;
  state.scen.built = model.mn;
  const H = SCEN_HEADLINE[model.mn];
  const ps = scenParams(model);
  const opts = (sel) => ps.map((p) =>
    `<option value="${p.id}"${p.id === sel ? " selected" : ""}>${p.label}</option>`).join("");
  // Sensible default axes: the two highest-impact inputs by convention —
  // discounting/vol style knobs first when present.
  const prefer = ["discount_rate", "terminal_growth", "sigma", "spot", "required_return",
                  "growth", "sigma_annual", "mu_annual", "beta", "expected_market_return",
                  "xi", "v0", "rho"];
  const ranked = [...ps].sort((a, b) =>
    (prefer.indexOf(a.id) + 1 || 99) - (prefer.indexOf(b.id) + 1 || 99));
  const defX = (ranked[0] || ps[0]).id;
  const defY = (ranked[1] || ps[1] || ps[0]).id;

  $("#scen").innerHTML = `
    <div class="scen-head">SCENARIO &amp; SENSITIVITY — ${model.name.toUpperCase()}
      <span id="scen-stat">HEADLINE METRIC: ${H.label}</span></div>

    <div class="scen-sec">1 · SCENARIOS — BEAR / BASE / BULL</div>
    <div class="scen-row">
      ${SLOTS.map((s) => `<button class="scen-set" data-slot="${s}">SET ${s.toUpperCase()} = CURRENT</button>`).join("")}
      <button id="scen-auto">⚡ AUTO-SEED ±12%</button>
      <button id="scen-compare" class="scen-go">▶ COMPARE</button>
    </div>
    <div class="scen-chips">${SLOTS.map((s) =>
      `<span class="chip ${s}" data-slot="${s}">${s.toUpperCase()} <b>—</b></span>`).join("")}</div>
    <div class="scen-hint">AUTO-SEED probes each input's direction of impact in-runtime, then
      builds BEAR/BULL by shifting every economic input ±12% the adverse/favourable way
      (BASE = your current inputs). Or freeze any hand-tuned set into a slot.</div>
    <div id="scen-cmp"></div>

    <div class="scen-sec">2 · TORNADO — WHAT MOVES ${H.label}</div>
    <div class="scen-row">
      <label>SHOCK</label>
      <select id="scen-tshock"><option value="0.05">±5%</option>
        <option value="0.10" selected>±10%</option><option value="0.20">±20%</option></select>
      <button id="scen-trun" class="scen-go">▶ RUN TORNADO</button>
    </div>
    <div class="scen-hint">Each input is shocked one-at-a-time (others held at current values);
      bars are ranked by how far they swing the headline. The longest bar is the assumption
      your answer lives or dies on.</div>
    <div id="scen-tornado"></div>

    <div class="scen-sec">3 · TWO-WAY SENSITIVITY GRID (7 × 7)</div>
    <div class="scen-row">
      <label>X</label><select id="scen-gx">${opts(defX)}</select>
      <label>Y</label><select id="scen-gy">${opts(defY)}</select>
      <label>SPAN</label>
      <select id="scen-gspan"><option value="0.10">±10%</option>
        <option value="0.20" selected>±20%</option><option value="0.30">±30%</option></select>
      <button id="scen-grun" class="scen-go">▶ RUN GRID</button>
    </div>
    <div id="scen-grid"></div>`;

  document.querySelectorAll(".scen-set").forEach((b) => {
    b.onclick = () => {
      saveScenSlot(model.mn, b.dataset.slot, state.values);
      syncScenChips();
      scenStat(`${b.dataset.slot.toUpperCase()} SAVED FROM CURRENT INPUTS`);
    };
  });
  $("#scen-auto").onclick = autoSeedScenarios;
  $("#scen-compare").onclick = runScenCompare;
  $("#scen-trun").onclick = runTornado;
  $("#scen-grun").onclick = runSensitivityGrid;
  syncScenChips();
}

function syncScenChips() {
  const slots = scenSlots(state.current.mn);
  document.querySelectorAll(".scen-chips .chip").forEach((c) => {
    const set = !!slots[c.dataset.slot];
    c.classList.toggle("set", set);
    c.querySelector("b").textContent = set ? "SET" : "—";
  });
}

/* --------------------------- auto-seed --------------------------------- */
async function autoSeedScenarios() {
  const model = state.current, mn = model.mn, H = SCEN_HEADLINE[mn];
  const ps = scenParams(model);
  scenButtons(true);
  const bear = { ...state.values }, bull = { ...state.values };
  for (let i = 0; i < ps.length; i++) {
    const p = ps[i], v = state.values[p.id];
    scenStat(`PROBING IMPACT DIRECTION ${i + 1}/${ps.length} — ${p.label}…`, true);
    await scenYield();
    const hLo = scenEval(mn, { ...state.values, [p.id]: scenShift(p, v, 0.10, -1) });
    const hHi = scenEval(mn, { ...state.values, [p.id]: scenShift(p, v, 0.10, +1) });
    if (hLo === null || hHi === null || hLo === hHi) continue;   // flat/failed: leave at base
    // Direction that makes the headline WORSE goes into BEAR, opposite into BULL.
    const upIsWorse = H.worse === "up" ? hHi > hLo : hHi < hLo;
    bear[p.id] = scenShift(p, v, 0.12, upIsWorse ? +1 : -1);
    bull[p.id] = scenShift(p, v, 0.12, upIsWorse ? -1 : +1);
  }
  saveScenSlot(mn, "bear", bear);
  saveScenSlot(mn, "base", { ...state.values });
  saveScenSlot(mn, "bull", bull);
  syncScenChips();
  scenButtons(false);
  scenStat("BEAR / BASE / BULL SEEDED — PRESS ▶ COMPARE");
}

/* --------------------------- comparison -------------------------------- */
async function runScenCompare() {
  const model = state.current, mn = model.mn, H = SCEN_HEADLINE[mn];
  const slots = scenSlots(mn);
  // Any unset slot falls back to the current inputs so COMPARE always works.
  const sets = SLOTS.map((s) => ({ slot: s, values: slots[s] || { ...state.values } }));
  scenButtons(true);
  const runs = [];
  for (let i = 0; i < sets.length; i++) {
    scenStat(`RUNNING ${sets[i].slot.toUpperCase()} SCENARIO…`, true);
    await scenYield();
    runs.push(scenEvalFull(mn, sets[i].values));
  }
  scenButtons(false);
  if (runs.some((r) => !r)) { scenStat("SCENARIO RUN FAILED — CHECK INPUTS", false); return; }

  const ps = scenParams(model);
  const baseH = runs[1][H.key];
  const fmtD = (h) => {
    if (typeof h !== "number" || typeof baseH !== "number" || !baseH) return "";
    const d = (h / baseH - 1) * 100;
    const cls = (H.worse === "up" ? -d : d) >= 0 ? "pos" : "neg";
    return `<span class="delta ${cls}">${d >= 0 ? "+" : ""}${d.toFixed(1)}%</span>`;
  };
  let html = `<table class="scen-table"><tr><th></th>
    ${sets.map((s) => `<th class="${s.slot}">${s.slot.toUpperCase()}</th>`).join("")}</tr>`;
  // headline first — the number the scenarios exist to move
  html += `<tr class="headline"><td class="k">${H.label}</td>${runs.map((r) =>
    `<td class="v">${fmtValue(H.key, r[H.key])} ${fmtD(r[H.key])}</td>`).join("")}</tr>`;
  // assumptions that differ across the three sets
  ps.forEach((p) => {
    const vals = sets.map((s) => s.values[p.id]);
    if (new Set(vals.map((v) => fmtParam(p, v))).size === 1) return;
    html += `<tr><td class="k">${p.label}</td>${vals.map((v) =>
      `<td class="v">${fmtParam(p, v)}</td>`).join("")}</tr>`;
  });
  // remaining scalar outputs for context
  Object.keys(runs[1]).forEach((k) => {
    if (k === H.key) return;
    if (!runs.every((r) => typeof r[k] === "number")) return;
    html += `<tr class="xtra"><td class="k">${k.replace(/_/g, " ").toUpperCase()}</td>${
      runs.map((r) => `<td class="v">${fmtValue(k, r[k])}</td>`).join("")}</tr>`;
  });
  html += "</table>";
  $("#scen-cmp").innerHTML = html;
  scenStat(`COMPARED — BEAR ${fmtValue(H.key, runs[0][H.key])} · BASE ${
    fmtValue(H.key, baseH)} · BULL ${fmtValue(H.key, runs[2][H.key])}`);
}

/* ----------------------------- tornado --------------------------------- */
async function runTornado() {
  const model = state.current, mn = model.mn, H = SCEN_HEADLINE[mn];
  const ps = scenParams(model);
  const shock = parseFloat($("#scen-tshock").value);
  scenButtons(true);
  scenStat("BASE RUN…", true);
  await scenYield();
  const baseH = scenEval(mn, state.values);
  if (baseH === null) { scenButtons(false); scenStat("BASE RUN FAILED", false); return; }

  const bars = [];
  for (let i = 0; i < ps.length; i++) {
    const p = ps[i], v = state.values[p.id];
    scenStat(`SHOCKING ${i + 1}/${ps.length} — ${p.label}…`, true);
    await scenYield();
    const lo = scenShift(p, v, shock, -1), hi = scenShift(p, v, shock, +1);
    const hLo = scenEval(mn, { ...state.values, [p.id]: lo });
    const hHi = scenEval(mn, { ...state.values, [p.id]: hi });
    if (hLo === null || hHi === null) continue;
    bars.push({ label: p.label, lo: hLo - baseH, hi: hHi - baseH,
                span: Math.max(Math.abs(hLo - baseH), Math.abs(hHi - baseH)) });
  }
  scenButtons(false);
  bars.sort((a, b) => b.span - a.span);
  const y = bars.map((b) => b.label);
  const mk = (xs, name) => ({
    type: "bar", orientation: "h", y, x: xs, base: baseH, name,
    marker: { color: xs.map((d) => (H.worse === "up" ? -d : d) >= 0 ? "#2fbf71" : "#e05252") },
    hovertemplate: "%{y}: " + H.label + " %{x:+.4f}<extra>" + name + "</extra>",
  });
  Plotly.react("scen-tornado", [
    mk(bars.map((b) => b.lo), `−${shock * 100}% SHOCK`),
    mk(bars.map((b) => b.hi), `+${shock * 100}% SHOCK`),
  ], {
    barmode: "overlay", showlegend: false,
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "IBM Plex Mono, monospace", color: "#c9d4e0", size: 10 },
    title: { text: `TORNADO — Δ ${H.label} PER ±${shock * 100}% INPUT SHOCK`,
             font: { color: "#53c9e0", size: 12 } },
    xaxis: { gridcolor: "#1d2530", zerolinecolor: "#2a3547",
             tickfont: { color: "#5c6b7d", size: 10 } },
    yaxis: { autorange: "reversed", tickfont: { color: "#c9d4e0", size: 10 } },
    shapes: [{ type: "line", x0: baseH, x1: baseH, yref: "paper", y0: 0, y1: 1,
               line: { color: "#ffb000", width: 1, dash: "dot" } }],
    margin: { l: 130, r: 20, t: 40, b: 30 }, height: Math.max(220, 40 + bars.length * 34),
  }, { responsive: true, displaylogo: false });
  const top = bars[0];
  scenStat(top ? `BIGGEST DRIVER: ${top.label} (SWING ${fmtValue(H.key, top.span)})`
              : "NO MOVABLE INPUTS");
}

/* ------------------------ two-way sensitivity --------------------------- */
async function runSensitivityGrid() {
  const model = state.current, mn = model.mn, H = SCEN_HEADLINE[mn];
  const ps = scenParams(model);
  const px = ps.find((p) => p.id === $("#scen-gx").value);
  const py = ps.find((p) => p.id === $("#scen-gy").value);
  if (!px || !py) return;
  if (px.id === py.id) { scenStat("PICK TWO DIFFERENT INPUTS", false); return; }
  const span = parseFloat($("#scen-gspan").value);
  const N = 7, mid = (N - 1) / 2;
  const axis = (p) => {
    const v = state.values[p.id];
    return Array.from({ length: N }, (_, i) =>
      scenShift(p, v, (span * Math.abs(i - mid)) / mid || 0, Math.sign(i - mid) || 1));
  };
  const xs = axis(px), ys = axis(py);

  scenButtons(true);
  const rows = [];
  let done = 0, min = Infinity, max = -Infinity;
  for (const yv of ys) {
    const row = [];
    for (const xv of xs) {
      row.push(scenEval(mn, { ...state.values, [px.id]: xv, [py.id]: yv }));
      if (++done % N === 0) { scenStat(`GRID ${done}/${N * N}…`, true); await scenYield(); }
    }
    rows.push(row);
    row.forEach((h) => { if (h !== null) { min = Math.min(min, h); max = Math.max(max, h); } });
  }
  scenButtons(false);
  const base = rows[mid][mid];
  //: green = favourable end of the metric, red = adverse (flipped for VaR).
  const shade = (h) => {
    if (h === null || max === min) return "";
    let t = (h - min) / (max - min);
    if (H.worse === "up") t = 1 - t;
    const g = Math.round(40 + t * 120), r = Math.round(160 - t * 120);
    return `background:rgba(${r},${g},60,0.28)`;
  };
  let html = `<div class="scen-gtitle">${H.label} — ${py.label} (ROWS) × ${px.label} (COLS)
      · CENTRE = CURRENT (${fmtValue(H.key, base)})</div>
    <table class="scen-table grid"><tr><th>${py.label} \\ ${px.label}</th>${
      xs.map((x, i) => `<th class="${i === mid ? "cur" : ""}">${fmtParam(px, x)}</th>`).join("")}</tr>`;
  rows.forEach((row, j) => {
    html += `<tr><th class="${j === mid ? "cur" : ""}">${fmtParam(py, ys[j])}</th>${
      row.map((h, i) => `<td class="v ${i === mid && j === mid ? "centre" : ""}" style="${shade(h)}">${
        h === null ? "—" : fmtValue(H.key, h)}</td>`).join("")}</tr>`;
  });
  html += "</table>";
  $("#scen-grid").innerHTML = html;
  scenStat(`GRID DONE — ${H.label} RANGES ${fmtValue(H.key, min)} → ${fmtValue(H.key, max)}`);
}

/* ======================================================================== *
 * BILLING — Razorpay plans, upload metering, PLAN tab.
 * FREE: 5 uploads/mo · ANALYST PRO ₹299/mo: 50 uploads · DESK UNLIMITED
 * ₹499/mo (MRP ₹599): unlimited. "Upload" = one IB-desk PDF analysis.
 * Amounts are authoritative server-side (api/_lib/billing.js); checkout is
 * Razorpay's hosted modal; payment proof is verified server-side. Until
 * RAZORPAY_* env vars exist, billing reports offline and nothing is gated.
 * ======================================================================== */

async function getBillingCfg() {
  if (state.billing.cfg) return state.billing.cfg;
  try {
    const r = await fetch("api/billing-config", { signal: AbortSignal.timeout(8000) });
    if (r.ok) state.billing.cfg = await r.json();
  } catch { /* offline -> treat as unconfigured */ }
  return state.billing.cfg;
}

async function refreshUsage() {
  const u = state.user;
  if (!u || u.provider !== "otp" || !u.token) return null;
  try {
    const r = await fetch("api/usage",
      { headers: { Authorization: "Bearer " + u.token }, signal: AbortSignal.timeout(8000) });
    const j = await r.json();
    if (r.ok && j.ok) { state.billing.usage = j; syncPlanChip(); return j; }
  } catch { /* keep last known usage */ }
  return null;
}

async function initBilling() {
  $("#planchip").onclick = () => openMenuTab("plan");
  await getBillingCfg();
  syncPlanChip();
  await refreshUsage();
  // Founders reveal: a just-signed-up winner gets shown their free month
  // once — the PLAN tab opens itself with the FOUNDER PASS banner.
  try {
    if (sessionStorage.getItem("finmodels.founderToast")) {
      sessionStorage.removeItem("finmodels.founderToast");
      openMenuTab("plan");
    }
  } catch { /* storage unavailable */ }
}

function syncPlanChip() {
  const el = $("#planchip");
  if (!el) return;
  const cfg = state.billing.cfg;
  if (!cfg || !cfg.billing) { el.innerHTML = `PLAN <b>FREE</b>`; return; }
  const us = state.billing.usage;
  if (!us || !us.metered) { el.innerHTML = `PLAN <b>FREE · SIGN IN</b>`; return; }
  const lim = us.limit === null ? "∞" : us.limit;
  el.innerHTML = `PLAN <b class="${us.plan !== "free" ? "paid" : ""}">${us.planName} · ${us.used}/${lim}</b>`;
}

/* ----------------------------- upload gate ------------------------------ */
async function uploadGate() {
  const cfg = await getBillingCfg();
  if (!cfg || !cfg.billing) return { allowed: true, metered: false };
  const u = state.user;
  if (!u || u.provider !== "otp" || !u.token) {
    return { allowed: false, upgrade: true,
      reason: "UPLOADS NEED AN EMAIL ACCOUNT — SIGN OUT & SIGN IN WITH EMAIL (FREE PLAN: 5/MO)" };
  }
  const us = await refreshUsage();
  if (!us) return { allowed: true, metered: false };   // fail-open on hiccup
  if (us.limit !== null && us.used >= us.limit) {
    return { allowed: false, upgrade: true,
      reason: `MONTHLY UPLOAD LIMIT REACHED (${us.used}/${us.limit}) — UPGRADE IN MENU ▸ PLAN` };
  }
  return { allowed: true, metered: true };
}

async function consumeUpload() {
  const u = state.user;
  if (!u || !u.token) return;
  try {
    const r = await fetch("api/usage",
      { method: "POST", headers: { Authorization: "Bearer " + u.token } });
    const j = await r.json();
    if (j && typeof j.used === "number") { state.billing.usage = j; syncPlanChip(); }
  } catch { /* metering is best-effort */ }
}

/* ------------------------------ PLAN tab -------------------------------- */
function planPriceHTML(p) {
  if (p.contact) return `<div class="pprice">CUSTOM <small>TAILORED TO YOUR DESK</small></div>`;
  if (!p.priceInr) return `<div class="pprice">₹0 <small>FOREVER</small></div>`;
  const strike = p.mrpInr ? `<s>₹${p.mrpInr}</s> ` : "";
  const save = p.mrpInr ? `<span class="psave">SAVE ₹${p.mrpInr - p.priceInr}</span>` : "";
  return `<div class="pprice">${strike}₹${p.priceInr} <small>/ MO</small> ${save}</div>`;
}

async function renderPlanTab(body) {
  body.innerHTML = `<h3>PLANS &amp; USAGE</h3><p class="hist-empty">LOADING…</p>`;
  const cfg = await getBillingCfg();
  const us = cfg && cfg.billing ? await refreshUsage() : null;
  const u = state.user;
  const isOtp = u && u.provider === "otp" && u.token;
  const current = us ? us.plan : "free";

  let head = "";
  if (!cfg || !cfg.billing) {
    head = `<div class="pnote">BILLING OFFLINE — every feature is currently free and unmetered.
      Paid plans activate when the operator connects Razorpay (see README).</div>`;
  } else if (!isOtp) {
    head = `<div class="pnote warn">Plans attach to email accounts. You're browsing as
      <b>${(u && u.provider ? u.provider : "guest").toUpperCase()}</b> — SIGN OUT and sign back in
      with <b>EMAIL ME A CODE</b> to use the free tier (5 uploads/mo) or subscribe.</div>`;
  } else if (us) {
    const lim = us.limit === null ? "∞" : us.limit;
    const pctUsed = us.limit === null ? 0 : Math.min(100, (us.used / us.limit) * 100);
    // free access won through the founders promo or gifted by the operator
    // announces itself right on the plan screen.
    let gift = "";
    if (us.via === "founder") {
      gift = `<div class="pnote gift">🎁 <b>FOUNDER PASS${us.founderNo ? " #" + us.founderNo : ""}</b>
        — you're one of the first 20 users: <b>1 MONTH OF ${us.planName} FREE</b>, active until
        ${us.expiresAt ? new Date(us.expiresAt).toLocaleDateString() : "—"}.</div>`;
    } else if (us.via === "grant") {
      gift = `<div class="pnote gift">🎁 <b>COMPLIMENTARY ACCESS</b> — ${us.planName} granted
        free of charge, active until ${us.expiresAt ? new Date(us.expiresAt).toLocaleDateString() : "—"}.</div>`;
    }
    head = gift + `<div class="pusage">
      <div class="purow"><span>SIGNED IN AS</span><b>${String(u.name || u.uid).toUpperCase().slice(0, 28)}</b></div>
      <div class="purow"><span>CURRENT PLAN</span><b class="${current !== "free" ? "paid" : ""}">${us.planName}</b></div>
      <div class="purow"><span>UPLOADS THIS MONTH (${us.month || ""})</span><b>${us.used} / ${lim}</b></div>
      ${us.limit !== null ? `<div class="pmeterbar"><div style="width:${pctUsed}%"></div></div>` : ""}
      ${us.expiresAt ? `<div class="purow"><span>PLAN RENEWS/EXPIRES</span><b>${new Date(us.expiresAt).toLocaleDateString()}</b></div>` : ""}
    </div>`;
    if (current === "free" && cfg && typeof cfg.foundersLeft === "number" && cfg.foundersLeft > 0) {
      head += `<div class="pnote gift">🎁 ${cfg.foundersLeft} OF 20 FOUNDER SLOTS LEFT —
        the first 20 email accounts get 1 month of DESK UNLIMITED free, automatically.</div>`;
    }
  }

  const plans = (cfg && cfg.plans) || [
    { id: "free", name: "FREE", priceInr: 0, uploads: 5, blurb: "5 company uploads / month · all 10 models · SCEN engine" },
    { id: "pro", name: "ANALYST PRO", priceInr: 299, uploads: 50, blurb: "50 company uploads / month · everything in FREE" },
    { id: "unlimited", name: "DESK UNLIMITED", priceInr: 499, mrpInr: 599, uploads: null, blurb: "Unlimited uploads · everything in PRO" },
    { id: "enterprise", name: "ENTERPRISE", priceInr: 0, uploads: null, contact: true, seats: 20,
      blurb: "Unrestricted access to the entire platform with unlimited analyses, guaranteed priority compute during peak traffic, provisioning for up to 20 team members, and early access to new capabilities ahead of general release — with dedicated onboarding and priority support." },
  ];
  const salesEmail = (cfg && cfg.contactEmail) || "sales@finmodels.app";
  const canBuy = cfg && cfg.billing && isOtp;
  body.innerHTML = `<h3>PLANS &amp; USAGE</h3>${head}
    <div class="pcards">${plans.map((p) => `
      <div class="pcard ${p.id} ${current === p.id ? "cur" : ""}">
        ${p.id === "unlimited" ? `<div class="pflag">BEST VALUE</div>` : ""}
        ${p.contact ? `<div class="pflag teams">FOR TEAMS</div>` : ""}
        <div class="pname">${p.name}</div>
        ${planPriceHTML(p)}
        <div class="pquota">${p.uploads === null ? "UNLIMITED" : p.uploads} UPLOADS${p.uploads === null ? "" : " / MO"}</div>
        ${p.seats ? `<div class="pquota seats">UP TO ${p.seats} SEATS</div>` : ""}
        <div class="pblurb">${p.blurb}</div>
        ${p.contact
          ? `<a class="pbuy contact" href="mailto:${salesEmail}?subject=${encodeURIComponent("Enterprise enquiry — FINMODELS TERMINAL")}">CONTACT SALES</a>`
          : p.id === "free"
            ? `<button class="pbuy" disabled>${current === "free" ? "CURRENT PLAN" : "INCLUDED"}</button>`
            : `<button class="pbuy" data-plan="${p.id}" ${canBuy && current !== p.id ? "" : "disabled"}>
               ${current === p.id ? "CURRENT PLAN" : cfg && cfg.billing ? `UPGRADE — ₹${p.priceInr}` : "OFFLINE"}</button>`}
      </div>`).join("")}</div>
    <div class="pnote" id="pmsg">Paid plans are 30-day passes — renewing or upgrading early credits your
      unused days. Payments are processed by Razorpay (UPI · cards · netbanking · wallets); this site
      never sees card details. An upload = one IB-desk PDF analysis; model runs and the SCEN engine
      are never metered.</div>`;
  body.querySelectorAll(".pbuy[data-plan]").forEach((b) => {
    b.onclick = () => startCheckout(b.dataset.plan);
  });
}

/* ------------------------------ checkout -------------------------------- */
function loadCheckoutJs() {
  return new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve();
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = resolve;
    s.onerror = () => reject(new Error("Razorpay checkout.js failed to load"));
    document.head.appendChild(s);
  });
}

function planMsg(text, isErr) {
  const el = $("#pmsg");
  if (el) { el.textContent = text; el.classList.toggle("warn", !!isErr); }
}

//: Dev-fake gateway only (local harness): sign the order like Razorpay would.
async function devFakeSignature(msg) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode("devsecret"),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function startCheckout(plan) {
  const cfg = await getBillingCfg();
  const u = state.user;
  if (!cfg || !cfg.billing || !u || !u.token) return;
  planMsg("CREATING ORDER…");
  let order;
  try {
    const r = await fetch("api/billing-order", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + u.token },
      body: JSON.stringify({ plan }),
    });
    order = await r.json();
    if (!r.ok || !order.ok) throw new Error(order.error || `HTTP ${r.status}`);
  } catch (err) {
    planMsg("ORDER FAILED: " + String(err.message || err).slice(0, 120), true);
    return;
  }

  const finalize = async (resp) => {
    planMsg("VERIFYING PAYMENT…");
    try {
      const r = await fetch("api/billing-verify", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + u.token },
        body: JSON.stringify(resp),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      await refreshUsage();
      await renderPlanTab($("#menu-body"));   // rebuild first, then the receipt
      planMsg(`✔ ${String(j.plan).toUpperCase()} ACTIVE — VALID UNTIL ${
        new Date(j.subscription.expiresAt).toLocaleDateString()}`);
    } catch (err) {
      planMsg("VERIFY FAILED: " + String(err.message || err).slice(0, 120) +
              " — if you were charged, the plan activates via webhook shortly", true);
    }
  };

  if (cfg.devFake) {   // local harness: complete the purchase without the modal
    const paymentId = "pay_dev" + Math.random().toString(36).slice(2, 12);
    finalize({
      razorpay_order_id: order.orderId,
      razorpay_payment_id: paymentId,
      razorpay_signature: await devFakeSignature(`${order.orderId}|${paymentId}`),
    });
    return;
  }

  try { await loadCheckoutJs(); }
  catch (err) { planMsg(String(err.message || err), true); return; }
  planMsg("OPENING RAZORPAY CHECKOUT…");
  new Razorpay({
    key: order.keyId,
    order_id: order.orderId,
    amount: order.amount,
    currency: order.currency,
    name: "FINMODELS TERMINAL",
    description: `${order.planName} — 30-DAY PASS`,
    prefill: { email: u.uid, name: u.name || "" },
    theme: { color: "#ffb000" },
    handler: finalize,
    modal: { ondismiss: () => planMsg("PAYMENT CANCELLED — no charge made") },
  }).open();
}
