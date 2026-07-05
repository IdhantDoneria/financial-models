# 📈 Financial Models

**Ten canonical models of quantitative finance — mathematically validated, pedagogically documented, and production-hardened — in one integrated Python + Jupyter codebase.**

> **▶ Run them live: [financial-models-six.vercel.app](https://financial-models-six.vercel.app)** — a
> Bloomberg-terminal-style interface where all ten models execute **in your browser**
> (the actual `src/*.py` files, running on CPython compiled to WebAssembly — no server).
> Type a mnemonic (`BSM`, `DCF`, `HES`, …) and press `<GO>`.

Each model is a self-contained class with a common interface (`calculate()` · `explain()` · `visualize()`), literature-sourced numerical benchmarks, and an automated scorer that grades it on three metrics. **All ten models score 10/10 on all three metrics**, and the full suite is covered by 88 passing tests.

---

## Table of contents
- [Overview](#overview)
- [The ten models](#the-ten-models)
- [Metric scores](#metric-scores)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Testing & scoring](#testing--scoring)
- [Live data](#live-data)
- [Deployment](#deployment)
- [References](#references)
- [License](#license)

## Overview

This project targets finance professionals, researchers, and students who need reference
implementations that are simultaneously:

- **Rigorous** — validated against academic literature (Hull's option tables, closed-form
  identities, live Fama-French factor data) to machine precision where a closed form exists.
- **Transparent** — every public method carries a Google-style docstring; formulas are stated
  in LaTeX in `explain()` and echoed as inline comments; each model ships an interactive chart.
- **Robust** — strict input validation, structured logging, full type hints, custom exceptions,
  and an extensible base-class architecture (add a model by subclassing and implementing three
  methods).

## The ten models

| # | Model | Category | Core formula | Reference |
|---|-------|----------|--------------|-----------|
| 1 | **Discounted Cash Flow** | Valuation | EV = Σ FCFₜ/(1+r)ᵗ + PV(TV) | Damodaran, *Investment Valuation* |
| 2 | **Gordon Growth (DDM)** | Valuation | P₀ = D₁/(r−g) | Gordon (1959) |
| 3 | **Modern Portfolio Theory** | Portfolio | min wᵀΣw s.t. wᵀ1=1 | Markowitz (1952) |
| 4 | **Value at Risk / CVaR** | Risk | VaRₐ = −(μ + z_α σ)·V | Jorion, *Value at Risk* |
| 5 | **CAPM** | Equity / Factor | E[R] = r_f + β(E[Rₘ]−r_f) | Sharpe (1964), Lintner (1965) |
| 6 | **Fama-French 3-Factor** | Equity / Factor | Rᵢ−R_f = α + b·MKT + s·SMB + h·HML | Fama & French (1993) |
| 7 | **Black-Scholes-Merton** | Derivatives | C = S·N(d₁) − K·e⁻ʳᵀ·N(d₂) | Black & Scholes (1973), Merton (1973) |
| 8 | **Binomial Tree (CRR)** | Derivatives | p = (e^{(r−q)Δt}−d)/(u−d) | Cox, Ross & Rubinstein (1979) |
| 9 | **Monte Carlo (GBM)** | Derivatives | Ĉ = e⁻ʳᵀ·E[max(Sₜ−K,0)] | Boyle (1977) |
| 10 | **Heston Stochastic Volatility** | Derivatives | dvₜ = κ(θ−vₜ)dt + ξ√vₜ dWₜ | Heston (1993) |

Each model exposes a `reference_benchmarks()` classmethod returning literature/identity checks
(e.g. Black-Scholes reproduces Hull Example 15.6 to a relative error of `8.9e-05`; put-call
parity holds to `4.5e-16`; the binomial tree converges to Black-Scholes; Heston reduces to
Black-Scholes as ξ→0 with relative error `4e-09`).

## Metric scores

Scores are **computed by `src/scorer.py`**, not asserted — via AST analysis (docstring
coverage, comment density, type-hint coverage) plus runtime probes (running each model's
benchmarks, rendering its explanation and figure).

| Model | Pedagogical clarity | Numerical accuracy | Production readiness | Total |
|-------|:---:|:---:|:---:|:---:|
| Discounted Cash Flow | 10 | 10 | 10 | **10.0** |
| Gordon Growth (DDM) | 10 | 10 | 10 | **10.0** |
| Modern Portfolio Theory | 10 | 10 | 10 | **10.0** |
| Value at Risk / CVaR | 10 | 10 | 10 | **10.0** |
| CAPM | 10 | 10 | 10 | **10.0** |
| Fama-French 3-Factor | 10 | 10 | 10 | **10.0** |
| Black-Scholes-Merton | 10 | 10 | 10 | **10.0** |
| Binomial Tree (CRR) | 10 | 10 | 10 | **10.0** |
| Monte Carlo (GBM) | 10 | 10 | 10 | **10.0** |
| Heston Stochastic Volatility | 10 | 10 | 10 | **10.0** |

> **Refinement cycles used: 0 of 3.** The design-for-quality approach (shared validated base
> class, benchmark-driven development) reached 10/10 on the first scoring pass, so no refactor
> cycles were required.

## Installation

Requires **Python 3.10+**.

```bash
git clone https://github.com/idhantdoneria/financial-models.git
cd financial-models
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## Quick start

**Use a model directly:**

```python
from src import BlackScholesModel

option = BlackScholesModel(spot=42, strike=40, rate=0.10, sigma=0.20,
                           maturity=0.5, option_type="call")
print(option.calculate()["price"])   # 4.759422  (matches Hull Example 15.6)
option.visualize().show()            # interactive Plotly value-vs-spot chart
print(option.explain())              # Markdown derivation + worked example
```

**Score every model:**

```python
from src import ALL_MODELS, score_all
print(score_all(ALL_MODELS)[["clarity", "accuracy", "production", "total"]])
```

**Launch the interactive notebook** (dropdown navigation + live sliders):

```bash
jupyter notebook notebooks/financial_models.ipynb
```

## Architecture

```
financial-models/
├── notebooks/
│   └── financial_models.ipynb     # interactive UI (ipywidgets nav + live panels)
├── src/
│   ├── base_model.py              # abstract BaseFinancialModel + validators + Benchmark
│   ├── dcf.py  mpt.py  capm.py  monte_carlo.py  black_scholes.py
│   ├── gordon_growth.py  fama_french.py  var_cvar.py
│   ├── stochastic_volatility.py  binomial.py
│   └── scorer.py                  # automated 3-metric scoring engine
├── tests/
│   ├── conftest.py                # representative instance factory
│   ├── test_models.py             # 75 tests: accuracy · interface · robustness · scoring
│   ├── test_web_assets.py         # guard: browser terminal runs the tested sources
│   └── pipeline/test_pipeline.py  # 8 tests: PDF analyzer end-to-end
├── scripts/build_notebook.py      # regenerates the notebook from source
├── scripts/sync_web_assets.py     # syncs src/ + FF data into public/ for the terminal
├── scripts/e2e_terminal.py        # headless-Chromium check: all 10 models in-browser
├── public/
│   ├── index.html · assets/       # FINMODELS terminal (Bloomberg-style, Pyodide)
│   ├── py/                        # synced model sources + web_bridge.py + manifest
│   ├── data/ff_factors.csv        # bundled Ken French factor snapshot
│   └── about.html                 # static project overview page
├── docs/design/terminal-spec.md   # terminal design specification
├── docs/design/mockup.html        # design-first UI wireframe
├── requirements.txt · vercel.json · .gitignore · LICENSE
```

Every model inherits `BaseFinancialModel`, which supplies the logger and a family of
`_require_*` validators. **Model logic is strictly separated from UI logic** — the notebook
widgets only call the public interface.

## 📄 Company PDF Analyzer

Upload a company financial PDF (10-K, 10-Q, annual report, investor deck) and the
pipeline scrapes the numbers, applies assumptions, runs any subset of the ten
models, and hands you a downloadable report.

**Pipeline** — `src/pipeline/`:

| Stage | Module | What it does |
|---|---|---|
| 1 · Extract | `pdf_extractor.py` | Cascade of **PyMuPDF → pdfplumber → pypdf → pdfminer.six**, then regex heuristics scrape revenue, FCFs, debt, cash, shares, price, β, growth, margin, tax rate. |
| 2 · Assume | `assumptions.py` | **Auto** — IB-style heuristic (WACC via CAPM, terminal g ≤ risk-free rate, sector-neutral β, Damodaran-style defaults). **Manual** — read-through of user overrides from the notebook sliders. |
| 3 · Run | `runner.py` | Instantiates each selected model and collects results into an `AnalysisReport`. |
| 4 · Export | `exporters.py` | **PDF** (reportlab, multi-page), **Excel** (openpyxl, one sheet per model), **Google Docs** (googleapiclient; [3-step setup](docs/google_docs_setup.md)). |

**Notebook UI** (in `notebooks/financial_models.ipynb` → section 5):
① `FileUpload` widget → ② extracted-data preview → ③ model checkboxes with select-all/clear-all → ④ **Auto** / **Manual** toggle (sliders for `r_f`, β, WACC, terminal *g*, σ, option T, VaR confidence/horizon, MC paths, strike/spot ratio, dividend growth) → ⑤ **Run** → ⑥ **⬇ PDF / ⬇ Excel / ⬇ Google Doc** buttons.

Tested end-to-end: synthetic 10-K → extract → run all 10 models → export PDF+XLSX
(see `tests/pipeline/test_pipeline.py`).

## Testing & scoring

```bash
pytest tests/ -q          # 88 tests
```

The suite validates: every model's benchmarks (numerical accuracy), the
`calculate`/`explain`/`visualize` interface contract, edge-case rejection
(`ValidationError` on negative spot, zero volatility, discount rate ≤ growth, etc.), and
asserts the full 10/10 scorecard. Tests also run in CI via GitHub Actions
(`.github/workflows/tests.yml`).

## Live data

The Fama-French model fetches monthly factor data **live** from
[Kenneth French's data library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html):

```python
from src import FamaFrenchModel
factors = FamaFrenchModel.load_factors()   # downloads, parses %→decimal, caches locally
```

The loader caches to `data/cache/` to avoid repeated downloads, falls back to the cache on
network failure, and raises a clear `ModelError` if neither source is available.

## 🖥️ FINMODELS Terminal (the deployed site)

The Vercel deployment is not a static showcase — it is the product. All ten models run
**live in the browser**:

| | |
|---|---|
| **Runtime** | [Pyodide](https://pyodide.org) — CPython 3.13 compiled to WebAssembly, with numpy · scipy · pandas · plotly |
| **Model code** | The *actual* `src/*.py` files, synced byte-for-byte into `public/py/src/` by `scripts/sync_web_assets.py`; a CI guard (`tests/test_web_assets.py`) fails the build if they ever drift from the tested sources |
| **UI** | Bloomberg-terminal-inspired: amber command line with mnemonics + `<GO>`, F1–F10 function keys, model rail, live-recalculating sliders, signed-color output grid, Plotly dark charts, KaTeX-rendered model derivations (design spec: `docs/design/terminal-spec.md`) |
| **Data** | Fama-French factors from a bundled Ken French snapshot (1926 → present), refreshed by the sync script |
| **Live ticker** | A top marquee streaming real prices — WTI crude · Brent · gold · silver · Bitcoin · Ethereum + **15 of the world's most valued indices** (S&P 500, Nasdaq, Dow, FTSE 100, DAX, CAC 40, Euro Stoxx 50, Nikkei, Hang Seng, Shanghai, Nifty 50, TSX, ASX 200, KOSPI, Taiwan) with signed intraday % change. Fed by a same-origin serverless function (`api/quotes.js`, Yahoo server-side, keyless, CDN-cached ~1×/min), with a CoinGecko crypto/gold fallback so it never blanks |
| **Server** | One tiny keyless serverless function for the ticker feed; everything else is plain static hosting with zero build step |

Mnemonics: `DCF` · `GG` · `MPT` · `VAR` · `CAPM` · `FF3` · `BSM` · `CRR` · `MC` · `HES`
(also `HELP`, and `IB` for the PDF analyzer below). Every slider change re-runs the real
Python model in ~0–15 ms once booted.

### 📊 Scenario & sensitivity engine (SCEN tab)

Every model carries a **SCEN** tab in the analytics panel — a stress-testing desk where
every number is a real in-runtime model run, not a linearisation:

- **Bear / base / bull scenarios** — freeze any hand-tuned assumption set into a named
  slot, or **⚡ auto-seed**: the engine probes each economic input's direction of impact
  (two runs per input) and builds bear/bull by shifting every input ±12 % the
  adverse/favourable way. **▶ COMPARE** runs all three and shows a side-by-side table —
  the model's headline metric with % deltas vs base, the assumptions that differ, and
  the remaining outputs for context. Slots persist per account + per model.
- **Tornado chart** — each input is shocked ±5/10/20 % one-at-a-time (others held at
  current values); horizontal bars rank how far each swings the headline metric, so the
  assumption the answer lives or dies on is the top bar. Rendered in Plotly with the
  base value as a dotted anchor line.
- **Two-way sensitivity grid** — pick any two inputs (defaults to the classic pair, e.g.
  WACC × terminal growth for DCF) and a ±10/20/30 % span: a 7 × 7 colour-coded grid of
  headline values, current inputs outlined at the centre, green = favourable / red =
  adverse (direction-aware: for VaR *lower* is green).

Each model is judged by its natural headline: value/share (DCF), intrinsic price (GG),
tangency Sharpe (MPT), VaR (VAR — where *up* is adverse), expected return (CAPM),
alpha (FF3), and option price (BSM/CRR/MC/HES). Numerical-precision knobs (MC paths,
tree steps, regression window, confidence) are excluded from stressing — they are
settings, not economic drivers.

### ⌁ IB Desk — company PDF analyzer, in the browser

Type **`IB <GO>`** (or click IB DESK): upload any **10-K or 10-Q PDF** and the terminal runs
the full analysis pipeline *client-side*:

1. **Extract** — `src/pipeline/pdf_extractor.py` (pypdf + pdfminer.six in WASM) scrapes
   revenue, FCFs, net income, debt, cash, shares, price, β, growth, margins, tax rate.
   Quarterly filings are auto-detected from their own language and flow figures are
   annualised ×4 (period basis can also be forced ANNUAL/QUARTERLY).
2. **Assume** — every missing figure is filled:
   **AUTO** (IB bot): CAPM WACC (80/20 equity-debt + 150 bp credit spread), terminal
   g ≤ r_f, sector-neutral β, Damodaran 5% ERP — with the **risk-free rate scraped live
   from the free US Treasury FiscalData API** (keyless, CORS-open) and a documented
   offline fallback. **MANUAL**: 16 override sliders; untouched sliders keep bot values.
3. **Run** — checkboxes select which of the ten models enter the report (ALL/NONE).
4. **Export** — download the report as **PDF** (reportlab), **Google Docs** (a .docx built
   with python-docx that Google Docs opens natively), or **Excel** (openpyxl) — all
   rendered inside the browser, nothing uploaded anywhere.

Every stage runs the same `src/pipeline/` package the pytest suite validates.

### 🔐 Sign in — email OTP (server), Google, or guest

The terminal opens with a login page ([`/login`](https://financial-models-six.vercel.app/login)). It has **two auth modes**, switched automatically by what the deployment has configured:

**Server mode — passwordless email OTP** (when a database + mailer are attached):
enter your email → receive a 6-digit code by email → verify → signed in. First successful
verify *is* the signup. All user details (email, name, created/last-login timestamps,
login count) are stored server-side, sessions are 30-day revocable bearer tokens, and the
terminal re-validates the session against `/api/auth-me` on every boot.

**Device-local mode** (out of the box, no credentials needed): email + password accounts
hashed with PBKDF2-SHA256 (Web Crypto, per-account salt, 150k iterations), stored only in
your browser.

Both modes also offer **Continue with Google** (real Google Identity Services, enabled by
`GOOGLE_CLIENT_ID`) and **Explore as guest**.

#### Enabling the server backend (10 minutes, free tiers)

The backend is fully coded and tested — it activates on env vars alone:

| Step | What | Env vars |
|---|---|---|
| 1 · Database | Vercel dashboard → *Storage / Marketplace* → add **Upstash for Redis** (free). The integration injects the vars automatically. | `KV_REST_API_URL`, `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL`/`_TOKEN`) |
| 2 · Email | Create a dedicated Gmail "work" account for sending codes → enable **2-Step Verification** → *Google Account ▸ Security ▸ App passwords* → generate a 16-char **App password** (a normal login password will not work over SMTP). Delivery uses **Nodemailer** over Gmail SMTP. Free Gmail sends ~500 msgs/day (Workspace ~2000). | `GMAIL_USER` (the address), `GMAIL_APP_PASSWORD` (the 16-char app password), optional `EMAIL_FROM` |
| 2 · Email (fallback) | Optional — if you'd rather use [resend.com](https://resend.com), leave the Gmail vars unset and provide a Resend key instead. Gmail takes priority when both are present. | `RESEND_API_KEY`, optional `EMAIL_FROM` |
| 3 · Optional | Random string to pepper OTP hashes. | `AUTH_SECRET` |
| 4 · Optional | Live 10Y sovereign yields for non-US markets on `/api/rates` (US Treasury FiscalData is keyless; other markets use FRED/OECD long-term government bond yields). Free key from [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html). Without it, non-US markets use the curated Damodaran baselines. FX context comes keyless from open.er-api.com. | `FRED_API_KEY` |

Redeploy after setting the vars — `/api/auth-config` flips `serverAuth: true` and the login
page switches to OTP mode by itself. Endpoints: `auth-request-otp` (6-digit code, salted
hash only, 10-min TTL, 60s resend cooldown, 5 sends/hour), `auth-verify-otp` (timing-safe
compare, 5 attempts, single-use, upserts the profile, issues the session), `auth-me`,
`auth-logout` (revocation). Test locally without any credentials:
`node scripts/test_auth_api.js` (16 backend checks),
`node scripts/stress_otp.js` (mailer transport selection + Gmail send path with
Nodemailer mocked + high-volume request→verify load) and
`python scripts/e2e_auth_otp.py` (full browser flow against
`scripts/dev_auth_server.js`).

The status bar shows who's signed in with a SIGN OUT action, and **saved company analyses
are kept per account**. Reopening a saved analysis rehydrates the extraction inside the
Python runtime, so any past company can be re-run and re-exported without re-uploading its
PDF.

### 💳 Plans & payments — Razorpay

The terminal has a built-in monetisation layer. The metered unit is an **upload**
(one IB-desk PDF analysis); model runs and the SCEN engine are never metered.

| Plan | Price | Uploads / month |
|---|---|---|
| **FREE** | ₹0 | 5 |
| **ANALYST PRO** | **₹299 / mo** | 50 |
| **DESK UNLIMITED** | ~~₹599~~ **₹499 / mo** (SAVE ₹100) | Unlimited |

Paid plans are **30-day passes** bought through Razorpay Checkout (UPI · cards ·
netbanking · wallets) — renewing or upgrading early credits the unused days. The
**MENU ▸ PLAN** tab shows the live usage meter, current plan and upgrade cards; the
status bar carries a plan chip (e.g. `PLAN ANALYST PRO · 12/50`). Plans attach to
**email-OTP accounts** (the server identity), so uploads require signing in with email
once billing is live.

**Security model:** amounts are authoritative **server-side only** (`api/_lib/billing.js`)
— the client never chooses what it pays; every payment is verified with Razorpay's
documented `HMAC-SHA256(order_id|payment_id, key_secret)` timing-safe check; orders are
pinned to the buying account and single-use; and a signed **webhook**
(`api/billing-webhook.js`, `payment.captured`) activates the plan even if the buyer's tab
dies before the client-side verify. Until Razorpay is configured, `billing: false` — the
terminal stays free and unmetered with an explicit offline state in the PLAN tab.

#### Enabling payments (needs the database from the auth setup above)

| Step | What | Env vars |
|---|---|---|
| 1 · Keys | [dashboard.razorpay.com](https://dashboard.razorpay.com) → *Settings → API Keys* → generate (test keys first, live after KYC). | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` |
| 2 · Webhook | *Settings → Webhooks* → add `https://<your-domain>/api/billing-webhook`, subscribe to `payment.captured`, set a secret. | `RAZORPAY_WEBHOOK_SECRET` |

Redeploy — `/api/billing-config` flips `billing: true`, the PLAN tab goes live and the
free-tier metering starts. Endpoints: `billing-config` (public catalogue),
`billing-order` (server-side order creation), `billing-verify` (signature check +
activation), `billing-webhook` (capture fallback), `usage` (GET entitlement / POST
consume, `402` when the month's allowance is spent). Test locally with zero credentials:
`node scripts/test_billing_api.js` (24 checks: catalogue, metering, 402, forged/hijacked/
replayed payments, upgrade day-carry, webhook, expiry) and `python scripts/e2e_billing.py`
(real-browser purchase through the dev-fake gateway).

#### 🎁 Founders promo — first 20 users free

The **first 20 email accounts automatically receive one month of DESK UNLIMITED free**,
claimed atomically at sign-up (a Redis counter is the arbiter, so two simultaneous
sign-ups can never share a slot). The login page advertises the remaining slots
(`🎁 FOUNDERS OFFER — N SLOTS LEFT`), the winner's PLAN tab opens itself once with the
**FOUNDER PASS #N** banner, and the admin desk shows who claimed which slot.

#### 🔑 Password sign-in (cloud accounts)

On the email-code screen, users can optionally **set a password** (min 8 chars). It is
stored server-side as an scrypt hash — the password itself never persists — and from
then on the login page's **PASSWORD** tab signs them in directly, no code round-trip.
Wrong-password attempts are rate-limited (10 per 15 min per address); "forgot password"
is just the EMAIL CODE tab again (verifying a fresh code lets a new password be set).
Everything about an account — profile, sign-in history, password hash, plan, usage —
lives in the cloud store.

#### ⚙ Admin desk — `/admin`

The operator's console at **`https://<your-domain>/admin`** (set an `ADMIN_KEY` env var
in Vercel, then enter it on the page — it is checked server-side, timing-safe, on every
request). It shows **every account**: email, name, founder slot, whether a password is
set, plan + how they got it (FOUNDER / GRANT / CHECKOUT), expiry, uploads this month,
sign-in count, last seen, joined. And it is the **manual grant mechanism**: type any
email, choose a plan and a duration (7–365 days) → that person gets free premium — it
works even before they sign up (the pass is waiting at first sign-in), re-granting
stacks days, and REVOKE ends a plan instantly. Backed by `/api/admin`; tested by
`node scripts/test_admin_founders.js` (26 checks) and `python scripts/e2e_founders_admin.py`.

### ☰ Menu & 🌐 market selector

Two persistent controls frame every view:

- **Hamburger menu** (top-left) — a **GUIDE** tab with a step-by-step walk-through of the
  whole terminal, a **MODELS** tab briefing each of the ten techniques in plain English
  with a *Best for* line so you can match the tool to your need (and jump straight in), and
  a **HISTORY** tab that auto-saves every company you analyse on the IB desk (reopen or
  delete any past analysis; persists in `localStorage`).
- **Market selector** (top-right) — pick from the **15 largest equity markets by total
  capitalisation** (US, China, Japan, India, Hong Kong, France, UK, Canada, Saudi Arabia,
  Germany, Switzerland, Taiwan, Australia, South Korea, Netherlands). The **risk-free rate,
  market return and cost of capital across every model** instantly re-anchor to that
  country's 10-year sovereign yield + equity-risk-premium, removing the US-only default.
  The US rate still refreshes live from the Treasury API; the rest use curated sovereign
  baselines, and the IB desk's live rate follows your chosen market too.

**End-to-end verification:** `python scripts/e2e_terminal.py` drives headless Chromium
through the complete user journey: the auth lifecycle (gate redirect, signup, identity
chip, sign-out, wrong-password rejection, login, guest), full boot, all ten models
producing numeric output and charts, the menu, the market selector (verifying the
risk-free rate follows the chosen country), the IB desk, and reopening a saved analysis
(snapshot shown, exports gated until re-run, then export verified). Run manually; needs
network for the first CDN fetch.

The static about page lives at [`/about`](https://financial-models-six.vercel.app/about).

## Deployment

- Configured for Vercel via `vercel.json` (static `public/` output, clean URLs, security
  headers). Import the repo into Vercel — zero build steps.
- **The interactive notebook** is best run locally with Jupyter (a live Python kernel is
  required for the `ipywidgets` panels). It also renders read-only on GitHub / nbviewer
  with all Plotly charts embedded.

## References

- Black, F. & Scholes, M. (1973). *The Pricing of Options and Corporate Liabilities.* JPE.
- Merton, R. C. (1973). *Theory of Rational Option Pricing.* Bell Journal of Economics.
- Cox, J., Ross, S. & Rubinstein, M. (1979). *Option Pricing: A Simplified Approach.* JFE.
- Boyle, P. (1977). *Options: A Monte Carlo Approach.* JFE.
- Heston, S. (1993). *A Closed-Form Solution for Options with Stochastic Volatility.* RFS.
- Markowitz, H. (1952). *Portfolio Selection.* Journal of Finance.
- Sharpe, W. (1964); Lintner, J. (1965). *CAPM.*
- Fama, E. & French, K. (1993). *Common Risk Factors in the Returns on Stocks and Bonds.* JFE.
- Gordon, M. (1959). *Dividends, Earnings, and Stock Prices.* RES.
- Hull, J. (2018). *Options, Futures, and Other Derivatives*, 10th ed.
- Damodaran, A. *Investment Valuation.* Wiley. · Jorion, P. *Value at Risk.* McGraw-Hill.

## License

[MIT](LICENSE) © 2026 Financial Models contributors
