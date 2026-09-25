# FINMODELS TERMINAL — boot-performance & UX audit

Status: **findings only, nothing implemented.** No code touched. Review and tell me which items to act on.

Method: read the actual boot code (`public/assets/terminal.js`, `boot-runtime.js`, `boot-packages.js`), then verified it empirically — served `public/` locally, drove headless Chromium through the real login → guest → terminal-boot flow against the live jsdelivr/PyPI CDNs (through this session's network), and captured the real network waterfall + wall-clock time to `TERMINAL_READY`. Screenshots were checked pixel-by-pixel, not eyeballed, before anything got called a bug — one apparent mobile rendering bug turned out to be a headless-Chromium screenshot-stitching artifact, not a real defect, and I dropped it rather than report a false positive.

**Caveat that matters:** this sandbox's network and CPU are almost certainly faster than a median visitor's phone on 4G. Treat every number below as a floor, not an expected user experience.

---

## 1. Model-loading time — measured baseline

| Run | Condition | Wall-clock to `TERMINAL_READY` |
|---|---|---|
| Cold | Empty browser cache — true first-time signup | **12.2s** |
| Warm | Same browser, immediate reload (HTTP cache hot) | **10.2s** |

Repeat-visit speedup: **~17%**. The boot screen's own copy — *"FIRST BOOT DOWNLOADS THE SCIENTIFIC PYTHON STACK (~15S) — CACHED AFTER"* — oversells this: "cached after" reads as "fast after," but two-thirds of the 12s wait is CPU-bound (WASM instantiation, wheel unpacking, `micropip.install`, Python import), not network-bound, so caching the downloads barely touches it.

Total payload: **33.4 MB across 65 requests.** Heaviest 8 assets, all on the critical path (boot doesn't reach `ready` until these resolve):

| Asset | Size | Origin | Needed for the default first model? |
|---|---|---|---|
| `scipy-*.whl` | 12.6 MB | jsdelivr (Pyodide) | **No** |
| `plotly-6.8.0-*.whl` (Python) | 9.4 MB | files.pythonhosted.org | Not until a chart renders |
| `numpy-*.whl` | 2.9 MB | jsdelivr | Yes |
| `pyodide.asm.wasm` | 2.5 MB | jsdelivr | Yes (CPython itself) |
| `python_stdlib.zip` | 2.3 MB | jsdelivr | Yes |
| `libopenblas-*.zip` | 1.8 MB | jsdelivr | Only if scipy/numpy linear algebra used |
| `narwhals-*.whl` | 353 KB | jsdelivr | Plotly dependency |
| `pyodide.asm.js` | 221 KB | jsdelivr | Yes |

## 2. Root causes (verified against the actual model code, not guessed)

**a. `scipy` is loaded for every visitor, but only half the models use it.**
`boot-packages.js` hardcodes `FINMODELS_CORE_PACKAGES = ["numpy", "scipy", "micropip"]`, fetched unconditionally in `boot()`. I checked every file in `src/`: of the 12 real models, only **6** import scipy — `binomial`, `black_scholes`, `monte_carlo`, `reverse_dcf`, `stochastic_volatility`, `var_cvar`. The other 6 — including **DCF, the model `boot()` selects by default** (`selectModel(requestedModel())` → "valuation first") — import only `numpy`. Every visitor pays 12.6 MB for a package most first sessions never call. The codebase already has the right pattern for this: `pandas` is lazy-loaded on first Fama-French use via `ensurePandas()` (a shared-promise guard). `scipy` isn't given the same treatment, for no reason stated in the comments.

**b. Python-side `plotly` (9.4 MB) gates "ready" for everyone, even though the JS-side Plotly already doesn't.**
`index.html` has an explicit, well-reasoned comment: the 1.4 MB *JS* Plotly bundle was pulled out of the boot path because "it dominated first-paint bandwidth... even though nothing is plotted until a model has run" — it's now fetched lazily by `ensurePlotly()` on first chart. The *Python* `plotly` package (installed via `micropip.install` inside `boot()`, `Promise.all`'d against `mountSources`) never got the same fix, and it's 6.7x bigger than the JS bundle it was deferred for. `web_bridge.run_model` calls `.visualize()` on every run, so it's not unnecessary work — it's just not necessary *before the boot screen disappears*. Nothing stops the app from becoming interactive (model selectable, inputs typeable) while this finishes installing in the background.

**c. There is no real-user telemetry.** The "~15s" figure in the boot screen is a guess, not a measurement — I found no analytics call anywhere in `boot()`. Nobody on the team currently knows the actual p50/p95 boot time across real devices and networks. Every recommendation below, including mine, is working from partial information without this.

---

### Recommended methodology

**Direct answer:** lazy-load `scipy` the same way `pandas` already is, and stop blocking `ready` on the Python `plotly` install — both are existing patterns in this codebase, both are low-risk, and together they cut the *common-case* cold payload by up to 22 MB (66%) for a visitor whose first model doesn't need scipy or a chart yet.

- **Perspective A — conservative, ship this week.**
  Add `ensureScipy()` mirroring `ensurePandas()`'s shared-promise guard; call it lazily from the 6 model handlers that need it (or prefetch it in the background right after `ready`, so it's likely warm by the time someone picks Black-Scholes). Move the Python-`plotly` `micropip.install` off the `Promise.all` that gates `bootPct(90)`/`ready`, and instead await it inside `run_model()`'s call to `.visualize()` if it hasn't resolved yet.
  Pros: matches existing code conventions exactly, small diff, no behavior change for any model that already needs these packages, fully reversible.
  Cons: doesn't fix the CPU-bound half of the boot cost (WASM instantiation etc.) — best case this gets a scipy-free, no-chart-yet session from ~12s to roughly 5–6s, not to "instant."

- **Perspective B — aggressive: shrink or replace the runtime.**
  Options bundled here: (i) self-host a trimmed Pyodide build with unused stdlib modules stripped, on Vercel's own edge instead of jsdelivr; (ii) replace scipy calls in those 6 models with pure-numpy equivalents (e.g. `scipy.stats.norm.cdf` → a numpy/math erf-based implementation) to drop the 12.6 MB dependency entirely.
  Pros: could plausibly get cold boot under 5s even for scipy-needing models; removes a third-party CDN dependency.
  Cons: (ii) is a correctness risk on a *financial model* — swapping scipy's tested routines for hand-rolled numpy equivalents needs the same numerical-accuracy rigor as any pricing-logic change (this is exactly the class of change `deep-build`-style verification exists for); (i) is real infra work, multi-week, and gives up jsdelivr's already-excellent, already-free, immutable multi-edge caching for something you'd now have to maintain.

- **Perspective C — do this first, regardless of A or B: instrument before you optimize further.**
  Send `{stage, ms}` boot-timing events to an endpoint (or even just `navigator.sendBeacon` to a log) so you get real p50/p95 by device class and connection type. My 12.2s/10.2s numbers are from a fast sandbox network and a cloud CPU; the real distribution, especially on mobile, could be 2-5x worse or could be fine — right now nobody knows. This is cheap, low-risk, and turns every future perf decision here from a guess into a measurement.

**Stress test of my own numbers:** don't take 12.2s/10.2s as "the" number — it's a best case. The actual first-time-signup experience for a median visitor is unmeasured and could be materially worse, particularly on mobile. Perspective C exists specifically to close that gap before more engineering effort goes into A or B.

---

## 3. Website friction / UX audit

Ranked by how much it works against a real visitor, most severe first.

**1. The guest path — the product's actual pitch — is the least visible option on the sign-in screen.**
The homepage and login page both sell "zero install, run in your browser, no account needed." But on `/login`, "EXPLORE AS GUEST" sits *below* the sign-in tabs, the email/password form, and a full Google SSO block — a first-time visitor has to scroll past three other options before reaching the one the marketing copy is actually selling. This is friction against the site's own stated value proposition. Concretely: put guest access above or beside the auth forms, not beneath them, or make it the visually primary action with sign-in secondary.

**2. Server-auth signup mixes two authentication models instead of picking one.**
When server auth is live, the email-code (OTP) flow *also* requires setting a password on the same screen ("SET A PASSWORD — MIN 8 · REQUIRED FOR NEW ACCOUNTS"). OTP exists to avoid asking someone to create/remember a password; here it's additive, not alternative — a new user does the email-code round trip *and* invents a password in the same form, for a benefit ("sign in without a code next time") that isn't obviously worth the extra field to someone signing up in a hurry. Consider making the password step optional/deferred ("add a password later from account settings") rather than required at signup.

**3. The Google-SSO-unavailable message is internal engineering language shown to end users.**
`auth.js` deliberately avoids pretending Google sign-in works when it doesn't (reasonable instinct), but the actual copy is: *"GOOGLE SSO NOT CONFIGURED ON THIS DEPLOYMENT — set GOOGLE_CLIENT_ID in Vercel env vars (OAuth web client) to enable."* That's a message written for whoever deploys the app, not for a visitor deciding whether to trust a finance tool with their email. **I can't confirm from here whether this is what real users currently see in production** — that depends on whether `GOOGLE_CLIENT_ID` is set on the live Vercel project, which I have no access to check. Worth a 30-second look at your Vercel env vars; if it's unset, users are seeing this right now. Either way, the fallback copy should read like a product message ("Google sign-in isn't available right now — use email or continue as guest"), not a config diff.

**4. The boot screen sets an expectation the product doesn't currently meet.**
Covered in §1 — "~15S... CACHED AFTER" implies repeat visits are fast; measured repeat-visit improvement is ~17%, because most of the cost is CPU work that caching can't touch. Either the copy should be honest about that, or (better) the underlying fix in §2a/b should land first so the copy becomes true.

**What's done well, for balance:** the paid-model gating (Ind AS 116, Reverse DCF) is disclosed inline everywhere a visitor could reasonably discover the model list — landing page, login page, and the in-app rail all say "Requires a paid plan" up front rather than surprising someone after signup. The boot-failure handling (`boot-runtime.js`) is unusually careful — CDN mirror fallback, cache-bypass retry, WASM-blocked detection with a real explanation instead of a silent hang — better than most sites bother with. Whoever wrote that clearly already cares about this exact problem; the scipy/plotly gap above reads like an oversight, not a lack of attention.

---

## What I'd do next, if you give the go-ahead

1. Ship Perspective A (§2) — lazy scipy, non-blocking Python-plotly install. Small, low-risk, matches existing patterns.
2. Add basic boot-timing telemetry (Perspective C) at the same time — costs almost nothing to add alongside (1) and tells you whether it actually worked for real users.
3. Reorder the login page: guest access promoted above the auth forms; reword the Google-unconfigured fallback; make the OTP password step optional.
4. Separately, and only if you want it: investigate whether `GOOGLE_CLIENT_ID` is actually set in the live Vercel project.

Say which of these (all / some / none, or something else entirely) you want done, and I'll implement and push.
