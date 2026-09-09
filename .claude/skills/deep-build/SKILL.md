---
name: deep-build
description: Enforces a mandatory think-first, verify-hard discipline before and after any non-trivial code change — a bug fix, a new feature, a refactor, anything touching logic that real users or real data will hit. Use this whenever the user asks to fix a bug, build a feature, patch a security issue, or change how a model/pipeline computes something — especially in this repo's extraction/valuation pipeline (src/pipeline/, src/*.py model files) where a plausible-looking fix has repeatedly turned out to have a second bug hiding right next to the first one. Also invoke it explicitly when the user types /deep-build. Do NOT skip this for "just a quick fix" — those are exactly the changes that ship with an undiscovered edge case, cost another round of debugging, and erode trust. This skill is about spending five extra minutes of thinking to save an hour of "wait, it's still broken."
---

# Deep Build

## Why this exists

The pattern that burns time and trust: a fix looks complete, gets shipped, and the *next* prompt — a fresh bug report, a security re-scan, a "check the other models too" — turns up something the fix should have caught the first time. Each round costs the user real usage budget and real waiting, and if this were a live product, real customers hitting a live bug. The fix for that pattern isn't more fixing — it's front-loading the thinking that should have happened before the first line of code, and refusing to call something "done" until it's been actively attacked, not just glanced at.

This skill is not a bureaucratic checklist. It's a sequence of four honest questions, asked in order, each one earning the right to move to the next: **do I actually understand this? what would break it? does my plan survive that? did it actually survive it?**

## Phase 1 — Understand before touching anything

Before writing or editing a single line, get a real, evidence-based picture of what you're changing — not a plausible-sounding guess.

- **Read the actual code path end to end**, not just the function named in the bug report. In this repo that usually means: `src/pipeline/pdf_extractor.py` (what gets scraped) → `src/pipeline/assumptions.py`'s `AutoAssumer.build()` (what fills the gaps) → the model class in `src/*.py` (what actually computes) → `src/pipeline/runner.py` / `api/premium.py` (how it's dispatched and headlined) → `public/assets/terminal.js` / `public/py/web_bridge.py` (how it reaches the user). A fix that only touches one link in that chain is a fix that's guessing about the others.
- **Find the root cause, not the symptom.** If a number is wrong, ask *why* it's wrong before deciding *how* to fix it. "The regex grabbed the wrong number" and "the regex grabbed the wrong number because the real one lives in a multi-column table this pattern was never designed to read" are different bugs with different fixes — the second framing is usually the one that's actually true, and only shows up if you look at the real input, not a description of it.
- **Check what's already there.** Grep for existing tests, existing helper functions, existing comments that explain a prior decision (this repo's comments are unusually good at recording *why*, e.g. `_ARITH_REF_RE`'s comment in `pdf_extractor.py` — read them before assuming something was never considered). Re-solving an already-solved problem, or contradicting a documented decision without knowing why it was made, both waste time.
- **If real data exists, use it, not a synthetic stand-in.** This repo has committed real-filing fixtures at `tests/fixtures/` for exactly this reason — a synthetic "Total revenue $12,450" test string will never surface the footnote-misattribution, multi-column, or OCR-artifact bugs that only show up in an actual filing. When you're about to write a test or verify a fix, ask: is there real data I should be checking this against instead of a clean example I made up?

## Phase 2 — Question it before you build it

This is the phase that gets skipped under time pressure, and it's the one that would have caught most of the bugs found in this repo's fix rounds. Before writing code, actually answer these — in writing, briefly, not just in your head:

- **What's the input space, really?** Not "a filing" — a quarterly results announcement with no balance sheet, a 10-K with 150 pages of boilerplate before the real numbers, OCR-degraded text with dropped commas and swapped characters, a filing in a different currency, a company that's never paid a dividend. If the fix only works on the one example that prompted it, it isn't a fix yet.
- **What would make this fail silently?** A crash is a good outcome — it gets noticed. The dangerous failure mode is a wrong-but-plausible number, a fabricated default that looks like real data, a status that reads "OK" when nothing was actually verified. For every new value your change produces, ask: if this is wrong, will anyone notice, or will it just look like a normal answer?
- **What else reads this value?** A change to `total_debt` doesn't just affect the field that was reported wrong — it flows into `net_debt`, into WACC's cost-of-debt derivation, into HDEBT's `reported_net_debt`. Trace forward, not just backward. Grep for every consumer of whatever you're changing before deciding the change is safe.
- **What's the guard, and is it actually tight enough — or too tight?** A plausibility check that's too loose lets garbage through; one that's too strict rejects genuinely correct real-world values (this repo has hit both: a cost-of-debt guard that first didn't exist, then correctly caught a ~21.5% garbage ratio, then had to be loosened after a *real* company's genuine ~4.15% cost of debt failed a too-strict lower bound). Don't just add a bound — sanity-check it against a real number before deciding it's right.
- **Write the plan down, short.** A few bullet points: what's changing, why this is the actual root cause, what the blast radius is, how you'll verify it. If you can't write this in five lines, you don't understand the change well enough to make it yet.

## Phase 3 — Build it

Now implement, following the plan from Phase 2. If something you discover while building contradicts the plan, that's a signal to go back to Phase 1 or 2, not to quietly improvise around it.

## Phase 4 — Try to break what you just built

A fix that hasn't been attacked hasn't been verified — it's been hoped at. This phase is adversarial on purpose: your job here is to try to prove your own change wrong before someone else does.

- **Run it against real data, not just the case that prompted the fix.** If you fixed something using one real filing, check it against at least one *other* real filing (a different company, a different filing type) to make sure the fix generalizes instead of overfitting to one example. A fix validated against exactly one input is a fix that's still mostly a guess.
- **Deliberately try the inputs that would break it.** The empty case, the missing case, the extreme case, the concurrent case, the case where two of your assumptions are both violated at once. If you added a plausibility guard, try the value that's exactly on its boundary, and the value just outside it.
- **Run the existing test suite — all of it, not just the tests for the thing you touched.** A change that passes its own new test but breaks three existing ones isn't done. In this repo: `pip install -r requirements.txt -r requirements-test.txt` then `pytest tests/ -q`; if you touched anything under `src/`, also run `python scripts/sync_web_assets.py --no-data` and re-run the suite, since `tests/test_web_assets.py` checks the browser copy hasn't drifted from the real source.
- **Add a regression test that would have caught the original bug**, ideally built from the real data that exposed it — not a clean synthetic example that happens to pass. A test that can't fail isn't testing anything.
- **State what you're still not sure about.** Don't imply total coverage you don't have. If there's a known gap, a case you didn't test, or a follow-up worth flagging, say so explicitly rather than letting silence imply everything's covered — that honesty is what actually prevents the next round of "wait, it's still broken."

## When to compress this

Not every change needs the full four phases at full weight — a one-line typo fix or a comment update doesn't need an adversarial verification pass. Use judgment: the depth of this process should scale with the blast radius and subtlety of the change, not apply uniformly to everything. But for anything touching extraction logic, financial calculations, security boundaries, or auth/data handling — the kind of change where "looks right" and "is right" have historically diverged in this repo — run the whole sequence. The cost of the extra five minutes is real; the cost of skipping it and being wrong has already been paid, more than once, in this exact codebase.
