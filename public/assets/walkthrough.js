/* FINMODELS TERMINAL — first-run product walkthrough.
 *
 * A coach-mark tour that spotlights real UI elements with short instructions.
 * Auto-starts once, right after a brand-new account finishes signing up (the
 * trigger is a one-shot sessionStorage flag set by auth.js at the exact
 * moment an account is CREATED — never on a returning sign-in, and never for
 * the anonymous guest path). Can also be launched any time from the GUIDE
 * tab in the hamburger menu.
 *
 * Skippable at every step. Reaching the end (not skipping) shows a
 * completion message. Either outcome permanently marks the account as
 * onboarded (per browser) so it never auto-launches again.
 */
"use strict";

(function () {
  const SS_TRIGGER = "finmodels.justSignedUp";
  const LS_ONBOARDED_PREFIX = "finmodels.onboarded.";

  function onboardedKey(uid) { return LS_ONBOARDED_PREFIX + uid; }
  function isOnboarded(uid) {
    try { return localStorage.getItem(onboardedKey(uid)) === "1"; }
    catch { return false; }
  }
  function markOnboarded(uid) {
    try { localStorage.setItem(onboardedKey(uid), "1"); }
    catch { /* private mode / quota — best-effort, tour just may reappear */ }
  }

  //: mobileView values line up 1:1 with #main[data-mview] / #mnav button
  //  data-mv values (see setMobileView in terminal.js) — null means "no
  //  panel switch needed" (the element lives in the always-visible cmdbar).
  const STEPS = [
    {
      title: "Welcome to FINMODELS TERMINAL",
      body: "A quick, skippable tour of the terminal — highlighting the pieces you'll use most. Takes about a minute.",
      target: null,
      nextLabel: "START TOUR",
    },
    {
      title: "1 · Pick your market",
      body: "Use the country selector to choose from the 15 largest equity markets. Every rate, and every monetary input across all 12 models, re-anchors to that market's currency.",
      target: "#country-btn",
    },
    {
      title: "2 · Choose a model",
      body: "Click a row here, press its function key (F1–F10), or type its mnemonic in the command bar and hit &lt;GO&gt;. DCF is selected by default.",
      target: '.mrow[data-mn="DCF"]',
      mobileView: "rail",
      advanceOn: { selector: "#rail .body", event: "click" },
    },
    {
      title: "3 · Drive the inputs",
      body: "Every parameter is a slider paired with an editable field. Move either and the model recalculates live — no run button.",
      target: "#inputs",
      mobileView: "inputs",
    },
    {
      title: "4 · Read the output",
      body: "OUTPUT shows the headline results — green for positive, red for negative.",
      target: "#output",
      mobileView: "output",
    },
    {
      title: "5 · Stress-test it",
      body: "The SCEN tab runs the scenario &amp; sensitivity engine — BEAR/BASE/BULL cases, a tornado chart, and a 7×7 sensitivity grid. Every cell is a real model run.",
      target: "#tab-scen",
      mobileView: "viz",
      advanceOn: { selector: "#tab-scen", event: "click" },
    },
    {
      title: "6 · Analyze a real company",
      body: "IB DESK takes a 10-K/10-Q PDF, extracts the financials, fills gaps automatically, and exports a full report. We'll leave it closed for now — open it anytime.",
      target: '.mrow[data-mn="IB"]',
      mobileView: "rail",
    },
    {
      title: "7 · Guide, models & history",
      body: "This menu (the ☰ button) holds this guide, a brief on every model, your saved company history, and your plan. You can restart this tour here anytime.",
      target: "#burger",
    },
    {
      title: "🎉 Congratulations — you've completed the walkthrough!",
      body: "You're ready to go. Pick a model, or open IB DESK to analyze a real filing.",
      target: null,
      nextLabel: "DONE",
      isFinish: true,
    },
  ];

  let els = null;      // overlay DOM refs, created lazily
  let idx = 0;
  let active = false;
  let currentAdvanceCleanup = null;
  let reposition = null;

  function buildOverlay() {
    const root = document.createElement("div");
    root.id = "wt-root";
    root.innerHTML = `
      <div class="wt-mask wt-mask-full" data-p="full"></div>
      <div class="wt-mask" data-p="top"></div>
      <div class="wt-mask" data-p="bottom"></div>
      <div class="wt-mask" data-p="left"></div>
      <div class="wt-mask" data-p="right"></div>
      <div class="wt-ring"></div>
      <div class="wt-card" role="dialog" aria-modal="true">
        <div class="wt-step"></div>
        <h3 class="wt-title"></h3>
        <p class="wt-body"></p>
        <div class="wt-actions">
          <button type="button" class="wt-skip">SKIP</button>
          <div class="wt-nav">
            <button type="button" class="wt-back">BACK</button>
            <button type="button" class="wt-next">NEXT</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(root);
    const e = {
      root,
      full: root.querySelector('[data-p="full"]'),
      top: root.querySelector('[data-p="top"]'),
      bottom: root.querySelector('[data-p="bottom"]'),
      left: root.querySelector('[data-p="left"]'),
      right: root.querySelector('[data-p="right"]'),
      ring: root.querySelector(".wt-ring"),
      card: root.querySelector(".wt-card"),
      stepEl: root.querySelector(".wt-step"),
      titleEl: root.querySelector(".wt-title"),
      bodyEl: root.querySelector(".wt-body"),
      skipBtn: root.querySelector(".wt-skip"),
      backBtn: root.querySelector(".wt-back"),
      nextBtn: root.querySelector(".wt-next"),
    };
    e.skipBtn.onclick = () => end(false);
    e.backBtn.onclick = () => { if (idx > 0) render(idx - 1); };
    e.nextBtn.onclick = () => advance();
    return e;
  }

  function teardownStepListeners() {
    if (currentAdvanceCleanup) { currentAdvanceCleanup(); currentAdvanceCleanup = null; }
    if (reposition) { window.removeEventListener("resize", reposition); window.removeEventListener("scroll", reposition, true); reposition = null; }
  }

  function positionFor(target) {
    const el = target ? document.querySelector(target) : null;
    if (target && !el) return null;   // caller decides: skip vs. treat as centered
    if (!el) return null;
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" });
    const r = el.getBoundingClientRect();
    const pad = 6;
    return {
      top: Math.max(0, r.top - pad), left: Math.max(0, r.left - pad),
      right: Math.min(window.innerWidth, r.right + pad), bottom: Math.min(window.innerHeight, r.bottom + pad),
    };
  }

  function layout(rect) {
    const vw = window.innerWidth, vh = window.innerHeight;
    if (!rect) {
      els.full.style.display = "block";
      els.top.style.display = els.bottom.style.display = els.left.style.display = els.right.style.display = "none";
      els.ring.style.display = "none";
      els.card.style.top = "50%"; els.card.style.left = "50%";
      els.card.style.transform = "translate(-50%, -50%)";
      return;
    }
    els.full.style.display = "none";
    els.ring.style.display = "block";
    const set = (node, t, l, w, h) => {
      node.style.display = "block";
      node.style.top = t + "px"; node.style.left = l + "px";
      node.style.width = Math.max(0, w) + "px"; node.style.height = Math.max(0, h) + "px";
    };
    set(els.top, 0, 0, vw, rect.top);
    set(els.bottom, rect.bottom, 0, vw, vh - rect.bottom);
    set(els.left, rect.top, 0, rect.left, rect.bottom - rect.top);
    set(els.right, rect.top, rect.right, vw - rect.right, rect.bottom - rect.top);
    els.ring.style.top = rect.top + "px"; els.ring.style.left = rect.left + "px";
    els.ring.style.width = (rect.right - rect.left) + "px";
    els.ring.style.height = (rect.bottom - rect.top) + "px";

    // Prefer below the spotlight; flip above if there isn't room; clamp
    // horizontally so the card never runs off either edge.
    els.card.style.transform = "none";
    const cardW = Math.min(360, vw - 24);
    els.card.style.width = cardW + "px";
    let left = Math.min(Math.max(8, rect.left), vw - cardW - 8);
    let top = rect.bottom + 14;
    const approxH = els.card.offsetHeight || 160;
    if (top + approxH > vh - 8) top = Math.max(8, rect.top - approxH - 14);
    els.card.style.left = left + "px";
    els.card.style.top = top + "px";
  }

  function render(i) {
    teardownStepListeners();
    idx = i;
    const step = STEPS[idx];

    if (step.mobileView && typeof window.isPhone === "function" && window.isPhone() && typeof window.setMobileView === "function") {
      window.setMobileView(step.mobileView);
    }

    const pos = positionFor(step.target);
    if (step.target && !pos) {
      // The target vanished (user navigated elsewhere mid-tour) — skip this
      // step rather than draw a spotlight around nothing. Guard against every
      // remaining step also being missing (abort quietly instead of looping).
      if (idx + 1 < STEPS.length) return render(idx + 1);
      return end(false);
    }

    els.stepEl.textContent = `${idx + 1} / ${STEPS.length}`;
    els.titleEl.innerHTML = step.title;
    els.bodyEl.innerHTML = step.body;
    els.backBtn.style.visibility = idx > 0 ? "visible" : "hidden";
    els.nextBtn.textContent = step.nextLabel || "NEXT";
    els.skipBtn.style.display = step.isFinish ? "none" : "";

    // Synchronous on purpose (not requestAnimationFrame): rAF is suspended
    // on a hidden/backgrounded tab in some environments, which would leave
    // the tour's first render of a step stuck invisible until the tab
    // regains focus. Querying .offsetHeight below already forces the reflow
    // this needs, so there's nothing to gain by deferring a frame anyway.
    layout(pos);
    reposition = () => layout(positionFor(step.target));
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);

    if (step.advanceOn) {
      const { selector, event } = step.advanceOn;
      const el = document.querySelector(selector);
      if (el) {
        const handler = () => advance();
        el.addEventListener(event, handler, { once: true });
        currentAdvanceCleanup = () => el.removeEventListener(event, handler);
      }
    }
  }

  function advance() {
    const step = STEPS[idx];
    if (step.isFinish) return end(true);
    if (idx + 1 >= STEPS.length) return end(true);
    render(idx + 1);
  }

  function end(completed) {
    teardownStepListeners();
    active = false;
    // Deliberately NOT window.state.user: state is a top-level `const` in
    // terminal.js, which (unlike a function declaration) never becomes a
    // window property in a classic script — window.state is always
    // undefined. currentUser() is a function declaration, so it IS exposed,
    // and reads the same session terminal.js itself bootstraps from.
    const u = typeof window.currentUser === "function" ? window.currentUser() : null;
    if (u && u.uid) markOnboarded(u.uid);
    if (els && els.root) { els.root.remove(); }
    els = null;
    document.removeEventListener("keydown", onKeydown);
  }

  function onKeydown(e) {
    if (!active) return;
    if (e.key === "Escape") end(false);
  }

  function start() {
    // Idempotent — never stack two tours. Checks live DOM, not just the
    // `active` flag: if the overlay's node was ever removed by something
    // other than end() (a bug, an extension, a stray DOM cleanup), `active`
    // would otherwise stay stuck true forever and silently brick every
    // future start() call for the rest of the page's life with no error.
    if (active && document.getElementById("wt-root")) return;
    active = true;
    els = buildOverlay();
    document.addEventListener("keydown", onKeydown);
    render(0);
  }

  function maybeAutoStart(user) {
    if (!user || user.uid === "guest") return;
    let flagUid = null;
    try {
      flagUid = sessionStorage.getItem(SS_TRIGGER);
      sessionStorage.removeItem(SS_TRIGGER);
    } catch { /* private mode — no auto-tour, still reachable via GUIDE */ }
    if (!flagUid || flagUid !== user.uid) return;
    if (isOnboarded(user.uid)) return;
    start();
  }

  window.FinmodelsWalkthrough = { start, maybeAutoStart };
})();
