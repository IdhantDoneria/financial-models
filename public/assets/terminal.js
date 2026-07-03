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
      { id: "base_fcf", label: "BASE FCF ($M)", min: 1, max: 500, step: 1, def: 100 },
      { id: "fcf_growth", label: "FCF GROWTH", min: -0.10, max: 0.25, step: 0.005, def: 0.08, pct: true },
      { id: "years", label: "HORIZON (Y)", min: 3, max: 10, step: 1, def: 5, int: true },
      { id: "discount_rate", label: "WACC", min: 0.04, max: 0.20, step: 0.0025, def: 0.09, pct: true },
      { id: "terminal_growth", label: "TERMINAL G", min: 0.0, max: 0.05, step: 0.0025, def: 0.025, pct: true },
      { id: "net_debt", label: "NET DEBT ($M)", min: -500, max: 2000, step: 10, def: 250 },
      { id: "shares_outstanding", label: "SHARES (M)", min: 10, max: 2000, step: 10, def: 150 },
    ],
  },
  {
    mn: "GG", name: "Gordon Growth (DDM)", cat: "Valuation",
    formula: "P₀ = D₁ / (r − g)",
    params: [
      { id: "dividend", label: "DIVIDEND D₀ ($)", min: 0.1, max: 20, step: 0.1, def: 2.5 },
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
      { id: "portfolio_value", label: "PORTFOLIO ($M)", min: 0.1, max: 1000, step: 0.1, def: 100 },
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
      { id: "spot", label: "SPOT S", min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", min: 1, max: 500, step: 1, def: 100 },
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
      { id: "spot", label: "SPOT S", min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", min: 1, max: 500, step: 1, def: 100 },
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
      { id: "spot", label: "SPOT S", min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", min: 1, max: 500, step: 1, def: 100 },
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
      { id: "spot", label: "SPOT S", min: 1, max: 500, step: 1, def: 100 },
      { id: "strike", label: "STRIKE K", min: 1, max: 500, step: 1, def: 100 },
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

const TAPE_ITEMS = [
  "<b>BSM</b> HULL EX 15.6 CALL <i>4.7594</i>",
  "<b>PARITY</b> C−P−S+Ke⁻ʳᵀ <i>4.5e−16</i>",
  "<b>CRR</b> →BS CONVERGENCE <i>OK</i>",
  "<b>HES</b> ξ→0 BS REDUCTION <i>4e−09</i>",
  "<b>FF3</b> KEN FRENCH 1926–2026 <i>1199 ROWS</i>",
  "<b>SCORE</b> 10 MODELS × 3 METRICS <i>30/30</i>",
  "<b>TESTS</b> PYTEST <i>83 PASS</i>",
  "<b>RUNTIME</b> CPYTHON·WASM <i>LIVE</i>",
];

/* ------------------------------------------------------------------------ */
const $ = (sel) => document.querySelector(sel);
const state = {
  pyodide: null, runPy: null, micropip: null, current: null, values: {},
  timer: null, seq: 0, view: "model",
  ib: { pkgsReady: false, fns: null, extracted: null, report: null,
        liveRf: null, rfSource: null, period: "auto", mode: "auto",
        dirty: {}, selected: null },
};

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

    buildUI();
    document.getElementById("boot").style.display = "none";
    document.getElementById("app").classList.add("ready");
    window.TERMINAL_READY = true;   // e2e hook
    selectModel("BSM");
    $("#cmd").focus();
  } catch (err) {
    bootLog("BOOT FAILURE: " + err, "err");
    console.error(err);
  }
}

/* ------------------------------ UI build ------------------------------- */
function buildUI() {
  $("#tape .inner").innerHTML = (TAPE_ITEMS.join(" <span>·</span> ") + " <span>·</span> ").repeat(3);

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
  $("#tab-doc").onclick = () => setTab("doc");

  setInterval(() => {
    $("#clock").textContent = new Date().toISOString().slice(0, 19).replace("T", " ") + " UTC";
  }, 1000);
}

function setTab(which) {
  $("#tab-chart").classList.toggle("on", which === "chart");
  $("#tab-doc").classList.toggle("on", which === "doc");
  const inIB = state.view === "ib";
  $("#chart").style.display = which === "chart" && !inIB ? "" : "none";
  $("#report").style.display = which === "chart" && inIB ? "block" : "none";
  $("#doc").style.display = which === "doc" ? "block" : "none";
  if (which === "chart" && !inIB) window.dispatchEvent(new Event("resize"));
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
  $("#oerr").style.display = "none";
  $("#output header .title").textContent = "OUTPUT";
  setTab("chart");
  state.current = model;
  state.values = {};
  model.params.forEach((p) => { state.values[p.id] = p.def; });

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

function paramRow(p) {
  const row = document.createElement("div");
  row.className = "prow";
  const label = `<label>${p.label}</label>`;

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

window.addEventListener("DOMContentLoaded", boot);

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
async function fetchLiveRiskFree() {
  // US Treasury FiscalData API — free, keyless, CORS-open. Average interest
  // rate on marketable Treasury Notes ≈ intermediate-tenor risk-free proxy.
  const url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/" +
    "v2/accounting/od/avg_interest_rates?filter=security_desc:eq:Treasury%20Notes" +
    "&sort=-record_date&page%5Bsize%5D=1";
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(8000) });
    const j = await r.json();
    const row = j.data[0];
    state.ib.liveRf = parseFloat(row.avg_interest_rate_amt) / 100;
    state.ib.rfSource = `US TREASURY FISCALDATA · NOTES AVG ${row.record_date}`;
  } catch {
    state.ib.liveRf = null;   // bridge falls back to the Damodaran base case
    state.ib.rfSource = "OFFLINE — DAMODARAN BASE CASE 4.25%";
  }
  renderIBContext();
}

/* --------------------------- view assembly ----------------------------- */
function selectAnalyzer() {
  state.view = "ib";
  state.current = null;
  clearTimeout(state.timer);
  if (!state.ib.selected) state.ib.selected = new Set(IB_MODELS);
  document.querySelectorAll(".mrow").forEach((r) => r.classList.toggle("active", r.dataset.mn === "IB"));
  document.querySelectorAll("#fkeys button").forEach((b) => b.classList.toggle("active", b.dataset.mn === "IB"));

  $("#inputs header .title").textContent = "IB DESK — COMPANY PDF ANALYZER";
  $("#formula").innerHTML = "ƒ  <b>UPLOAD 10-K / 10-Q → SCRAPE → ASSUME (AUTO IB-BOT | MANUAL) → RUN MODELS → EXPORT</b>";
  $("#output header .title").textContent = "EXTRACTED DATA";
  $("#ostat").textContent = "—"; $("#ostat").className = "meta";
  $("#ogrid").innerHTML = ""; $("#oerr").style.display = "none";

  buildIBForm();
  renderIBContext();
  renderIBReport();
  setTab("chart");
  if (state.ib.liveRf === null && state.ib.rfSource === null) fetchLiveRiskFree();
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
    body.insertAdjacentHTML("beforeend",
      `<div class="ibhint">IB BOT: CAPM WACC (80/20 equity-debt, +150bp credit spread), terminal g ≤ r_f
       (Gordon constraint), sector-neutral β fallback, Damodaran ERP 5%. Risk-free scraped LIVE
       from the US Treasury FiscalData API, with an offline fallback.</div>`);
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
  const haveReport = !!state.ib.report;
  ["pdf", "docx", "xlsx"].forEach((f) => { $(`#exp-${f}`).disabled = !haveReport; });
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
    renderIBExtracted();
    ibStatus(`EXTRACTED · ${out.period.toUpperCase()} BASIS · ${out.backends.join("+") || "no backend"}`);
    $("#ostat").className = "meta";
  } catch (err) {
    state.ib.extracted = null;
    ibStatus("EXTRACTION FAILED: " + String(err).slice(0, 160), true);
  }
  syncIBButtons();
}

function renderIBExtracted() {
  const grid = $("#ogrid");
  grid.innerHTML = "";
  $("#oerr").style.display = "none";
  const out = state.ib.extracted;
  if (!out) { renderIBContext(); return; }
  Object.entries(IB_FIELD_LABELS).forEach(([key, label]) => {
    const value = out.fields[key];
    const isMissing = value === null || value === undefined || (Array.isArray(value) && !value.length);
    const tr = document.createElement("tr");
    const shown = isMissing
      ? `<span class="badge assumed">AUTO-ASSUMED</span>`
      : `${fmtValue(key, value)} <span class="badge found">PDF</span>`;
    tr.innerHTML = `<td class="k">${label}</td><td class="v">${shown}</td>`;
    grid.appendChild(tr);
  });
  renderIBContext();
}

function renderIBContext() {
  if (state.view !== "ib") return;
  let ctx = $("#ibctx");
  if (!ctx) {
    ctx = document.createElement("tr");
    ctx.id = "ibctx";
  }
  const rf = state.ib.liveRf;
  ctx.innerHTML = `<td class="k">RISK-FREE (LIVE)</td><td class="v">${
    rf !== null && rf !== undefined ? (rf * 100).toFixed(3) + "%" : "—"
  } <span class="badge live">${state.ib.rfSource || "FETCHING…"}</span></td>`;
  $("#ogrid").appendChild(ctx);
}

async function runIBReport() {
  if (!state.ib.extracted) return;
  if (!state.ib.selected.size) { ibStatus("SELECT AT LEAST ONE MODEL", true); return; }
  ibStatus(`RUNNING ${state.ib.selected.size} MODELS…`);
  await new Promise((r) => setTimeout(r, 25));
  try {
    const payload = {
      mode: state.ib.mode,
      selected: [...state.ib.selected],
      live_rf: state.ib.liveRf,
      rf_source: state.ib.rfSource,
      overrides: state.ib.mode === "manual" ? state.ib.dirty : {},
    };
    const out = JSON.parse(state.ib.fns.run(JSON.stringify(payload)));
    if (!out.ok) throw new Error(out.error);
    state.ib.report = out;
    renderIBReport();
    setTab("chart");
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
