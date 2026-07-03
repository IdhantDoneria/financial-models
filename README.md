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
| **Server** | None. Zero backend, zero build step — plain static hosting |

Mnemonics: `DCF` · `GG` · `MPT` · `VAR` · `CAPM` · `FF3` · `BSM` · `CRR` · `MC` · `HES`
(also `HELP`, and `IB` for the PDF analyzer below). Every slider change re-runs the real
Python model in ~0–15 ms once booted.

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

**End-to-end verification:** `python scripts/e2e_terminal.py` drives headless Chromium
through the full boot and asserts all ten models produce numeric output and charts
in-browser (run manually; needs network for the first CDN fetch).

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
