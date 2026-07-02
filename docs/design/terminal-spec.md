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
│ TICKER TAPE (benchmark stats, scrolling)                     │
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
