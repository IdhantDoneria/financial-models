/* Resilient bring-up of the Pyodide runtime for the terminal's boot().
 *
 * Why this exists. Pyodide swallows two download failures instead of
 * rejecting cleanly, and boot() used to make exactly one attempt against one
 * host with no way to tell them apart:
 *
 *  1. python_stdlib.zip (or another core file) fails or arrives truncated.
 *     Pyodide logs "Error occurred while installing the standard library",
 *     Python then can't import `encodings`, exits with status 1, and
 *     loadPyodide() rejects with an emscripten ExitStatus. ExitStatus is a
 *     plain class, NOT an Error, so `"BOOT FAILURE: " + err` printed
 *     "[object Object]" and the visitor was stuck at 8% with no clue.
 *  2. pyodide.asm.wasm can't be instantiated (WebAssembly blocked by a
 *     browser security mode, say). Pyodide only console.warn()s
 *     "wasm instantiation failed!" and the promise NEVER settles — a silent
 *     hang at 8%.
 *
 * This file (a) turns any thrown value into readable text, (b) retries a
 * failed bring-up — first the same host with the HTTP cache bypassed (a
 * poisoned/partial cache entry is the classic cause of (1)), then two
 * jsDelivr mirrors that serve byte-identical files — and (c) detects (2) and
 * fails fast with an explicit reason instead of hanging.
 *
 * It has no DOM or Pyodide dependency of its own beyond what is passed in, so
 * scripts/test_boot_runtime.js can exercise it under Node with fakes shaped
 * like the real Pyodide failures.
 */
"use strict";
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FinmodelsBoot = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  const VERSION = "0.28.2";
  // SRI for pyodide.js at VERSION — identical to the <script> tag in index.html.
  const PYODIDE_JS_SRI = "sha384-TxAJpclxcSefRC2TGQWWyfpQPm6eLSr9fUPEAGETMFdsgI20wbP4X3vQSfGINmpU";
  // Same npm/GitHub-backed content served from different jsDelivr edge hosts.
  const HOSTS = [
    "https://cdn.jsdelivr.net",
    "https://fastly.jsdelivr.net",
    "https://gcore.jsdelivr.net",
  ];
  const ATTEMPTS = [
    { host: HOSTS[0], fresh: false },
    { host: HOSTS[0], fresh: true },    // same host, HTTP cache bypassed
    { host: HOSTS[1], fresh: true },
    { host: HOSTS[2], fresh: true },
  ];
  const RETRY_PAUSE_MS = 800;
  const WASM_WARNING = "wasm instantiation failed!";   // literal from pyodide.js 0.28.2

  const indexURL = (host) => `${host}/pyodide/v${VERSION}/full/`;

  /** Never returns "[object Object]", whatever was thrown. */
  function describeError(err) {
    if (err === null || err === undefined) return "unknown error";
    if (typeof err === "string") return err || "unknown error";
    if (typeof err !== "object" && typeof err !== "function") return String(err);

    const name = typeof err.name === "string" ? err.name : "";
    const message = typeof err.message === "string" ? err.message : "";
    if (err instanceof Error) return message ? `${name || "Error"}: ${message}` : (name || "Error");
    // emscripten's ExitStatus: { name, message, status } but not an Error.
    if (name === "ExitStatus" || (typeof err.status === "number" && message)) {
      return `${message || "runtime exited"} (Python could not start: a core runtime file did not download completely)`;
    }
    if (message) return name ? `${name}: ${message}` : message;
    // e.g. a failed <script> load delivers an Event, not an Error
    if (typeof err.type === "string") {
      const target = err.target && (err.target.src || err.target.href);
      return target ? `${err.type} event for ${target}` : `${err.type} event`;
    }
    try {
      const json = JSON.stringify(err);
      if (json && json !== "{}") return json.slice(0, 300);
    } catch { /* circular etc. */ }
    const ctor = err.constructor && err.constructor.name;
    return `unrecognised error${ctor && ctor !== "Object" ? ` (${ctor})` : ""}`;
  }

  class WasmBlockedError extends Error {
    constructor(detail) {
      super(`WebAssembly could not start in this browser${detail ? ` (${detail})` : ""}`);
      this.name = "WasmBlockedError";
    }
  }

  class BootError extends Error {
    constructor(failures) {
      const last = failures[failures.length - 1];
      super(`runtime failed to start after ${failures.length} attempt${failures.length === 1 ? "" : "s"}; last error: ${last.reason}`);
      this.name = "BootError";
      this.failures = failures;
    }
  }

  /** Pyodide only console.warn()s when the wasm won't instantiate, then hangs
   *  forever. Turn that warning into a rejection we can race against. */
  function watchForWasmFailure(env) {
    const con = env.console;
    const realWarn = con.warn;
    let fail, sawMarker = false, detail = "";
    const failed = new Promise((_, reject) => { fail = reject; });
    failed.catch(() => { /* raced below; never surface as unhandled */ });
    con.warn = function (...args) {
      if (args[0] === WASM_WARNING) {
        sawMarker = true;
        // Pyodide logs the underlying reason on the very next warn(); let it land first.
        setTimeout(() => fail(new WasmBlockedError(detail)), 0);
      } else if (sawMarker && !detail) {
        detail = describeError(args[0]);
      }
      return realWarn.apply(this, args);
    };
    return { failed, stop() { if (con.warn !== realWarn) con.warn = realWarn; } };
  }

  /** For one attempt, make fetches under `prefix` skip the HTTP cache. */
  function bypassCacheFor(env, prefix) {
    const real = env.fetch;
    if (typeof real !== "function") return () => {};
    const wrapper = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || String(input);
      if (url.startsWith(prefix)) return real.call(env, input, Object.assign({}, init, { cache: "reload" }));
      return real.apply(env, arguments);
    };
    env.fetch = wrapper;
    return () => { if (env.fetch === wrapper) env.fetch = real; };
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /**
   * Run `load(indexURL)` (loadPyodide + core packages) until it succeeds.
   * Resolves with whatever `load` resolves with. Rejects with WasmBlockedError
   * straight away (retrying can't help) or BootError once every attempt failed.
   */
  async function bringUpRuntime({ load, log = () => {}, env = root, attempts = ATTEMPTS, pauseMs = RETRY_PAUSE_MS }) {
    const failures = [];
    for (let i = 0; i < attempts.length; i++) {
      const a = attempts[i];
      const url = indexURL(a.host);
      if (i > 0) {
        log(`runtime download failed, retrying (${i + 1}/${attempts.length})${a.host !== attempts[0].host ? " via mirror" : ""}…`);
        await sleep(pauseMs);
      }
      const restoreFetch = a.fresh ? bypassCacheFor(env, url) : () => {};
      const watch = watchForWasmFailure(env);
      try {
        return await Promise.race([load(url), watch.failed]);
      } catch (err) {
        if (err instanceof WasmBlockedError) throw err;
        failures.push({ host: a.host, fresh: !!a.fresh, reason: describeError(err) });
        if (env.console && env.console.warn) env.console.warn(`[boot] attempt ${i + 1} via ${a.host} failed:`, err);
      } finally {
        watch.stop();
        restoreFetch();
      }
    }
    throw new BootError(failures);
  }

  function injectScript(doc, src, integrity) {
    return new Promise((ok, bad) => {
      const s = doc.createElement("script");
      s.src = src;
      s.integrity = integrity;
      s.crossOrigin = "anonymous";
      s.onload = ok;
      s.onerror = () => bad(new Error(`could not load ${src}`));
      doc.head.appendChild(s);
    });
  }

  /** index.html normally provides loadPyodide from the primary host; if that
   *  <script> was blocked or failed, try the same file (same SRI) from mirrors. */
  async function ensurePyodideScript({ env = root, hosts = HOSTS, inject = injectScript } = {}) {
    if (typeof env.loadPyodide === "function") return;
    let last;
    for (const host of hosts) {
      try {
        await inject(env.document, `${indexURL(host)}pyodide.js`, PYODIDE_JS_SRI);
        if (typeof env.loadPyodide === "function") return;
      } catch (err) { last = err; }
    }
    throw last || new Error("pyodide.js could not be loaded");
  }

  return {
    describeError, bringUpRuntime, ensurePyodideScript,
    WasmBlockedError, BootError, ATTEMPTS, HOSTS, indexURL,
  };
});
