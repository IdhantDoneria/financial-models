# FINMODELS TERMINAL — Design Specification

**Goal.** The deployed Vercel URL is not a brochure — it is the product. All ten
models execute live in the visitor's browser (Pyodide/WebAssembly runs the
*actual* `src/*.py` files, unmodified). The interface borrows its language from
the two most recognizable professional surfaces in finance:

| Inspiration | What we take |
|---|---|
| **Bloomberg Terminal** | Black canvas, amber command line, four-letter mnemonics + `<GO>`, function-key strip, dense monospace data grids, ticker tape |
| **BlackRock Aladdin** | Disciplined panel architecture, muted analytical palette for content areas, risk-first result presentation, restrained typography hierarchy |

---

## 1 · Color system

| Token | Hex | Role |
|---|---|---|
| `--bg` | `#050608` | Application canvas (near-black, not pure black) |
| `--panel` | `#0b0e12` | Panel background |
| `--panel-alt` | `#10141a` | Input rows, zebra stripes |
| `--border` | `#1d2530` | 1px panel borders / rules |
| `--amber` | `#ffb000` | Command line, selection, primary actions (Bloomberg amber) |
| `--amber-dim` | `#8a6200` | Amber borders / inactive accents |
| `--cyan` | `#53c9e0` | Panel titles, links, secondary data |
| `--green` | `#3dd68c` | Positive values, success states |
| `--red` | `#ff5d5d` | Negative values, errors |
| `--text` | `#c9d4e0` | Body text |
| `--text-dim` | `#5c6b7d` | Labels, captions, units |

Rules: amber is reserved for *command and selection* (never body data);
values are signed-colored (green/red) only where sign is meaningful;
everything else stays in the neutral text ramp — Aladdin's restraint.

## 2 · Typography

- Single family: **IBM Plex Mono** (Google Fonts, swap), fallback `ui-monospace, Menlo, monospace`.
- Scale: 11px labels · 13px body/data · 15px panel titles · 20px command line.
- All-caps with letter-spacing (+0.08em) for panel titles and labels — terminal convention.
- Numerals are the interface: tabular by nature of the mono family, right-aligned in grids.

## 3 · Layout grid

Desktop (≥1100px): fixed viewport app, no page scroll — panels scroll internally.

```
┌──────────────────────────────────────────────────────────────┐
│ LIVE TICKER: oil·gold·silver·BTC/ETH·15 world indices, ±%   │
├──────────────────────────────────────────────────────────────┤
│ CMD BAR:  FINMODELS ▮  BSM ......................... <GO>    │
├──────────────────────────────────────────────────────────────┤
│ F1 DCF │ F2 GG │ F3 MPT │ F4 VAR │ … │ F10 HES   (fn strip)  │
├──────────┬──────────────────────────┬────────────────────────┤
│ MODELS   │ INPUTS                   │ OUTPUT                 │
│ (rail,   │ label · slider · value   │ key ······· value      │
│ 10 rows) │ …                        │ (signed coloring)      │
│          ├──────────────────────────┴────────────────────────┤
│          │ CHART ▏DOC   (tabbed; Plotly dark / KaTeX md)     │
├──────────┴───────────────────────────────────────────────────┤
│ STATUS: PY 3.13 WASM · numpy scipy pandas plotly · 42 ms     │
└──────────────────────────────────────────────────────────────┘
```

Mobile (<1100px): panels stack vertically (rail becomes horizontal chip row);
same components, page scrolls.

## 4 · Interaction model

- **Command line** is the primary navigation: type a mnemonic, press Enter
  (`<GO>`). Unknown mnemonic → amber error line, Bloomberg-style
  (`%INVALID MNEMONIC — F1..F10 or HELP`).
- **Function keys** F1–F10 (real keydown + clickable strip) map to the ten models.
- **Sliders + numeric twins**: every parameter is a slider *and* an editable
  number field, always in sync; changes debounce-recalculate (200 ms) — the
  terminal feels live, no Run button needed (a `RECALC <GO>` action exists for
  explicitness).
- **Boot sequence**: black screen with amber log lines while Pyodide + numpy/
  scipy/pandas/plotly load (~10–25 s first visit, cached after). Progress is
  honest (real stages), styled as a terminal boot log.
- **Errors** from model validation (`ValidationError`) render verbatim in the
  OUTPUT panel in red — the models' own guardrails are part of the pedagogy.

## 5 · Model mnemonics & input schemas

| Key | Mnemonic | Model | Inputs (slider ranges) |
|---|---|---|---|
| F1 | `DCF` | Discounted Cash Flow | base FCF 1–500 ($M), FCF growth −10–25%, horizon 3–10y, WACC 4–20%, terminal g 0–5%, net debt −500–2000 ($M), shares 10–2000 (M) |
| F2 | `GG` | Gordon Growth (DDM) | D₀ 0.1–20, required return 2–25%, growth 0–10% |
| F3 | `MPT` | Modern Portfolio Theory | μ₁,μ₂,μ₃ 0–25%, σ₁,σ₂,σ₃ 5–60%, pairwise ρ −0.45–0.9 (floor keeps the 3-asset covariance positive-definite), r_f 0–8% |
| F4 | `VAR` | Value at Risk / CVaR | annual μ −20–30%, annual σ 5–80%, confidence 90–99%, horizon 1–30d, portfolio $0.1–1000M, method (hist/param/MC) |
| F5 | `CAPM` | CAPM | r_f 0–8%, E[R_m] 2–20%, β −1–3 |
| F6 | `FF3` | Fama-French 3-Factor | true b/s/h loadings −1–2, α −1–1%/mo, idio σ 0–5%/mo, window 24–360 mo — regression recovers the loadings from real Ken French factor history |
| F7 | `BSM` | Black-Scholes-Merton | S 1–500, K 1–500, r 0–15%, σ 5–100%, T 0.05–5y, q 0–8%, call/put |
| F8 | `CRR` | Binomial Tree | BSM inputs + steps 10–2000 + european/american |
| F9 | `MC` | Monte Carlo (GBM) | BSM inputs + paths 10k–500k + antithetic toggle |
| F10 | `HES` | Heston | S, K, r, T + v₀ 0.005–0.5, κ 0.1–10, θ 0.005–0.5, ξ 0.05–1.5, ρ −0.95–0.5 |

Series-input models (MPT/VAR/FF3) get scalar slider front-ends; the Python
bridge synthesizes the series (covariance assembly, seeded return draws,
factor-history windowing) and feeds the *unchanged* model classes.

## 6 · Panels

1. **MODELS rail** — mnemonic · name · category; selected row amber-barred.
2. **INPUTS** — parameter rows; section header shows the model's formula inline.
3. **OUTPUT** — `calculate()` dict as a two-column grid; floats formatted
   context-aware (%, $, 4-dp greeks); calc-time footer.
4. **CHART** — `visualize()` figure via Plotly.js, re-templated to terminal
   palette (transparent paper, `--border` gridlines, mono font). Series colors
   from the model figures are preserved.
5. **DOC** — `explain()` markdown rendered (marked.js) with KaTeX for LaTeX
   blocks; cyan headings.
6. **STATUS BAR** — runtime state, loaded packages, last calc ms, data
   snapshot date, UTC clock.

## 7 · Technical architecture

```
public/
├── index.html            terminal shell
├── assets/terminal.css   this spec, in CSS
├── assets/terminal.js    boot, command loop, panels, Plotly/KaTeX glue
├── py/                   ← synced copies of src/*.py (scripts/sync_web_assets.py)
│   ├── manifest.json     file list + hash + sync date
│   └── web_bridge.py     registry + param builders + run_model()
└── data/ff_factors.csv   Ken French monthly snapshot (offline-first)
```

- Pyodide v0.28 from jsDelivr; packages: numpy, scipy, pandas (built-ins) +
  plotly via micropip.
- `src/` files are written into the WASM filesystem at `/app/src/` and imported
  as the real package — **zero forked model code**.
- Fama-French: the loader's cache path is pre-seeded with the bundled CSV, so
  `load_factors()` resolves offline (its designed fallback path).
- CI guard: `tests/test_web_assets.py` asserts `public/py/` is in sync with
  `src/` so the deployed models can never drift from the tested ones.

## 8 · IB Desk (mnemonic `IB` / `PDF` / `REPORT`)

The analyzer view reuses the panel grid: INPUTS hosts a 5-step form
(① upload 10-K/10-Q · ② period basis AUTO/ANNUAL/QUARTERLY with ×4 flow
annualisation · ③ assumption engine AUTO/MANUAL — auto scrapes the live
risk-free from the US Treasury FiscalData API, manual exposes all 16
`ManualOverrides` sliders with dirty-tracking so untouched knobs keep bot
values · ④ model checkboxes · ⑤ run + export). EXTRACTED DATA replaces
OUTPUT, with per-field `PDF` / `AUTO-ASSUMED` / `LIVE` badges. The CHART tab
becomes the report summary; DOC shows the assumption-rationale audit trail.
Exports (PDF / Google-Docs .docx / Excel) are rendered by the same
`src/pipeline` exporters inside WASM and downloaded as blobs. The analyzer's
pure-Python backends (pypdf, pdfminer.six, reportlab, openpyxl, python-docx)
micropip-install lazily on first open, keeping the main boot fast.

## 9 · Live ticker tape

The top marquee streams **real market data**, not build stats: WTI crude, Brent,
gold, silver, Bitcoin, Ethereum, and **15 of the world's most valued equity
indices** (S&P 500, Nasdaq, Dow, FTSE 100, DAX, CAC 40, Euro Stoxx 50, Nikkei
225, Hang Seng, Shanghai, Nifty 50, TSX, ASX 200, KOSPI, Taiwan) — each with its
last price and signed intraday % change (green ▲ / red ▼). Hovering pauses the
scroll.

Data path: browsers can't reach Yahoo/Stooq directly (no CORS headers) and no
keyless CORS-open source spans indices + energy + metals, so a **same-origin
Vercel serverless function** (`api/quotes.js`) fetches Yahoo Finance server-side
(no key) and returns a compact JSON array. It is CDN-cached
(`s-maxage=60, stale-while-revalidate=300`) so Yahoo is hit ~once/minute
regardless of traffic. The front-end polls `/api/quotes` every 60 s; if it is
ever unreachable it falls back to CoinGecko (keyless, CORS-open) for live
crypto + gold, padded with static engine stats, so the tape never goes blank.

## 10 · Authentication (`login.html`)

The terminal is gated by a login page styled as a split panel: brand/feature
rail on the left, auth card on the right. Two modes, auto-selected from
`/api/auth-config`:

**Server mode — passwordless email OTP.** Active when the deployment has a
store (Upstash Redis via `KV_REST_API_URL`/`KV_REST_API_TOKEN`) and a mailer
(`RESEND_API_KEY`). Flow: email → `POST /api/auth-request-otp` emails a
6-digit code (only its salted SHA-256 hash is stored, 10-min TTL, 60 s resend
cooldown, 5 sends/hour) → `POST /api/auth-verify-otp` (timing-safe compare,
max 5 attempts, single-use) upserts the user profile server-side (email,
name, createdAt, lastLoginAt, loginCount) and issues a 30-day revocable
bearer session. The terminal validates the token via `GET /api/auth-me` on
boot and revokes it via `POST /api/auth-logout` on sign-out. Shared logic
lives in `api/_lib/` (store/email/auth — underscore-prefixed, so not exposed
as routes). Verified by `scripts/test_auth_api.js` (in-process, 16 checks)
and `scripts/e2e_auth_otp.py` (browser flow against the full-stack local
server `scripts/dev_auth_server.js`).

**Device-local mode** (no backend configured) offers:

- **Email + password** — device-local accounts. Passwords are hashed with
  PBKDF2-SHA256 (Web Crypto, per-account random salt, 150k iterations) and
  stored in `localStorage`; nothing is ever transmitted, matching the
  product's zero-server-data architecture. Signup validates name/email/
  password (min 8 chars, live strength meter, confirm field); sign-in
  supports show/hide password and "keep me signed in" (30 days vs 12 hours).
- **Google** — real Google Identity Services. The login page reads the public
  OAuth client id from `/api/auth-config` (a serverless function exposing the
  `GOOGLE_CLIENT_ID` env var); when configured, the official GIS button
  renders and the returned ID-token's email/name become the account. When not
  configured, the button declares itself disabled with setup instructions
  rather than failing silently.
- **Guest** — one click, no account, full functionality.

The terminal reads the session (`finmodels.session`) at boot and redirects to
`login.html` without one. The status bar shows the signed-in identity with a
SIGN OUT action. **Saved analyses are namespaced per account**
(`finmodels.history.<uid>`), with one-time adoption of pre-auth history.
Restored analyses rehydrate the Python-side extraction
(`web_bridge.restore_extraction`) so a saved company can be re-run and
exported without re-uploading the PDF; until re-run, the snapshot is
display-only and exports stay gated.

## 11 · Global chrome — menu & market selector

Two persistent controls sit in the command bar, framing every view.

**Hamburger menu (left).** A slide-in drawer with three tabs:

- **GUIDE** — a numbered, step-by-step walk-through of the whole terminal
  (pick market → choose model → drive inputs → read output → IB desk → history),
  written for a first-time visitor.
- **MODELS** — a plain-English brief for each of the ten techniques: one line
  on *what it does* and a green **Best for** line on *which need it fits*, so a
  user can match tool to question (value a company, size risk, price an option,
  allocate). Each brief has an **OPEN →** button that jumps straight into that
  model.
- **HISTORY** — saved company analyses. Every IB-desk run is auto-snapshotted
  (deduped by company, most-recent first) to `localStorage`; each entry can be
  reopened (rehydrating the extracted data + report) or deleted.

**Market selector (right).** A dropdown of the **15 largest equity markets by
total capitalisation** (US, China, Japan, India, Hong Kong, France, UK, Canada,
Saudi Arabia, Germany, Switzerland, Taiwan, Australia, South Korea,
Netherlands). Each carries a 10-year sovereign-yield risk-free proxy `rf` and a
Damodaran equity-risk-premium `erp`. Selecting a market repoints the default of
**every risk-free / short-rate input** (`risk_free_rate`, `rate`) and derives
each model's market return as `rf + erp` — dissolving the built-in US-only
assumption. The US rate can still refresh **live** from the Treasury FiscalData
API; the other fourteen use curated sovereign baselines, and the IB desk's live
risk-free follows the chosen market too. The choice persists in `localStorage`
and is shown in the status bar.

## 12 · Scenario & sensitivity engine (`SCEN` tab)

A third analytics tab (CHART · **SCEN** · DOC), shown for the ten model views and
hidden on the IB desk. Everything it displays comes from re-running the actual
Python model in Pyodide with perturbed inputs — no closed-form deltas.

**Headline metric.** Each mnemonic maps to one output the engine stresses
(`SCEN_HEADLINE`): `price_per_share` (DCF), `price` (GG/BSM/CRR/MC/HES),
`tangency_sharpe` (MPT), `var` (VAR), `expected_return` (CAPM), `alpha` (FF3).
A `worse` flag records the adverse direction (`up` for VaR, `down` otherwise) so
bear/bull construction and the colour semantics stay direction-aware.

**Eligible inputs.** Numeric slider params minus `SCEN_EXCLUDE`
(`n_sims`, `n_steps`, `window`, `confidence`, `horizon_days`) — precision and
measurement settings are not economic drivers. Shifts are relative with an
absolute floor (`max(|v|·f, (max−min)·f/4)`), clamped to slider ranges,
integer-rounded for `int` params.

**Section 1 — scenarios.** Three slots (BEAR/BASE/BULL) stored per account and
per model in `localStorage` (`finmodels.scenarios.<uid>`). `SET <slot> =
CURRENT` freezes the live inputs; `⚡ AUTO-SEED` probes each input at ±10 % to
learn its impact sign, then shifts every input ±12 % in the adverse (bear) or
favourable (bull) direction, with base = current. `▶ COMPARE` renders a table:
headline row (amber, with signed % deltas vs base coloured by favourability),
differing assumptions, then remaining scalar outputs (dimmed).

**Section 2 — tornado.** One-at-a-time ±5/10/20 % shocks; Plotly horizontal
overlay bars based at the base headline, sorted by swing, green/red per effect
direction, dotted amber base line, biggest driver echoed in the status chip.

**Section 3 — two-way grid.** Two param selects (defaults ranked by a
preference list — WACC × terminal g for DCF, σ × spot for options), ±10/20/30 %
span, 7 × 7 linspace, cells shaded red→green across the observed range
(inverted when `worse: "up"`), centre cell (current inputs) outlined amber.

Long scans yield to the event loop between runs and stream progress into
`#scen-stat`; all engine buttons disable while a scan is in flight. Switching
model resets the panel (`resetScenPanel`, purging the tornado plot).
E2E coverage: `run_scenario_engine_scenario` in `scripts/e2e_terminal.py`
(auto-seed → bear<base<bull compare → tornado → grid → per-model rebuild).
