// Regression test for the "BOOT FAILURE: [object Object]" dead-end at 8%.
//
// What actually happened: a core Pyodide file (python_stdlib.zip) failed to
// download in a visitor's browser. Pyodide swallows that, Python then exits
// with status 1, and loadPyodide() rejects with an emscripten ExitStatus —
// a plain class that is not an Error — so boot() printed "[object Object]"
// and never retried. A sibling failure (wasm can't instantiate) is worse: the
// promise never settles at all. The shapes below were captured from the live
// site with the corresponding request blocked (see boot-runtime.js).
//
// Layers:
//   1. describeError never yields "[object Object]" for any thrown shape.
//   2. bringUpRuntime retries (cache-bypassed, then mirrors), restores the
//      globals it patches, aborts fast on the wasm hang, and reports every
//      failure when it gives up.
//   3. Static drift guards: the SRI/version in boot-runtime.js match
//      index.html, the mirrors are permitted by the CSP, and boot() still
//      routes through the module.
//
//     node scripts/test_boot_runtime.js

const fs = require("fs");
const path = require("path");
const Boot = require("../public/assets/boot-runtime.js");

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

// Exactly the live shape: not an Error, no toString, name/message/status own props.
class ExitStatus {
  constructor(status) { this.name = "ExitStatus"; this.message = `Program terminated with exit(${status})`; this.status = status; }
}

function makeEnv() {
  const env = {
    fetchCalls: [],
    warns: [],
    console: { warn: (...a) => env.warns.push(a) },
  };
  env.fetch = async (input, init) => { env.fetchCalls.push({ url: String(input), init }); return {}; };
  return env;
}
const opts = (env, load, extra = {}) => ({ load, env, pauseMs: 0, ...extra });

(async () => {
  console.log("· describeError never prints [object Object]");
  const exit = new ExitStatus(1);
  check("sanity: ExitStatus really stringifies to [object Object]", String(exit) === "[object Object]");
  const d = Boot.describeError(exit);
  check("ExitStatus is described with its exit code", d.includes("exit(1)") && !d.includes("[object"), d);
  check("ExitStatus hint points at the incomplete download", /did not download completely/.test(d), d);
  check("Error keeps name and message", Boot.describeError(new TypeError("Failed to fetch")) === "TypeError: Failed to fetch");
  check("string passes through", Boot.describeError("boom") === "boom");
  check("null/undefined are safe", Boot.describeError(null) === "unknown error" && Boot.describeError(undefined) === "unknown error");
  check("Event-like (failed <script>) names type and target",
    Boot.describeError({ type: "error", target: { src: "https://x/y.js" } }) === "error event for https://x/y.js");
  check("plain object is JSON-ified", Boot.describeError({ code: 7 }) === '{"code":7}');
  const circ = {}; circ.self = circ;
  const dc = Boot.describeError(circ);
  check("circular object falls back without throwing or [object Object]", !dc.includes("[object") && dc.length > 0, dc);
  check("empty object is still readable", !Boot.describeError({}).includes("[object"));

  console.log("· bringUpRuntime: recovers from the real failure");
  {
    const env = makeEnv();
    const seen = [];
    const result = await Boot.bringUpRuntime(opts(env, async (url) => {
      seen.push(url);
      await env.fetch(url + "python_stdlib.zip");
      if (seen.length === 1) throw new ExitStatus(1);      // attempt 1: the live failure
      return "PYODIDE";
    }));
    check("resolves with the loader's value on attempt 2", result === "PYODIDE");
    check("attempt 1 goes to the primary host", seen[0] === "https://cdn.jsdelivr.net/pyodide/v0.28.2/full/", seen[0]);
    check("attempt 2 retries the SAME host", seen[1] === seen[0], seen[1]);
    check("attempt 1 does not bypass the HTTP cache", env.fetchCalls[0].init === undefined || env.fetchCalls[0].init.cache === undefined);
    check("attempt 2 bypasses the HTTP cache (cache: reload)", env.fetchCalls[1].init && env.fetchCalls[1].init.cache === "reload");
    check("fetch is restored after a successful run", env.fetch !== undefined && env.fetchCalls.length === 2 && (await env.fetch("z"), env.fetchCalls[2].init === undefined));
  }

  console.log("· bringUpRuntime: walks the mirrors, then reports everything");
  {
    const env = makeEnv();
    const logs = [], hosts = [];
    let err;
    try {
      await Boot.bringUpRuntime(opts(env, async (url) => { hosts.push(url); throw new ExitStatus(1); }, { log: (m) => logs.push(m) }));
    } catch (e) { err = e; }
    check("gives up with a BootError", err instanceof Boot.BootError, String(err));
    check("all four attempts were made", hosts.length === 4, String(hosts.length));
    check("attempts cover primary x2 then both mirrors",
      hosts[2].startsWith("https://fastly.jsdelivr.net/") && hosts[3].startsWith("https://gcore.jsdelivr.net/"), hosts.join(" | "));
    check("BootError lists every failure, readable", err.failures.length === 4 && err.failures.every((f) => !f.reason.includes("[object")));
    check("BootError message is readable", !err.message.includes("[object") && /4 attempts/.test(err.message), err.message);
    check("progress was logged for each retry", logs.length === 3, String(logs.length));
    check("fetch left unpatched after failure", (await env.fetch("z"), env.fetchCalls[env.fetchCalls.length - 1].init === undefined));
  }

  console.log("· bringUpRuntime: wasm-instantiation hang fails fast");
  {
    const env = makeEnv();
    const realWarn = env.console.warn;
    let calls = 0, err;
    const t0 = Date.now();
    try {
      await Boot.bringUpRuntime(opts(env, (url) => {
        calls++;
        // What pyodide.js 0.28.2 does: two warns, then the promise never settles.
        env.console.warn("wasm instantiation failed!");
        env.console.warn(new Error("simulated: wasm blocked"));
        return new Promise(() => {});
      }));
    } catch (e) { err = e; }
    check("rejects with WasmBlockedError instead of hanging", err instanceof Boot.WasmBlockedError, String(err));
    check("carries the underlying reason", err && /wasm blocked/.test(err.message), err && err.message);
    check("does not pointlessly retry other hosts", calls === 1, String(calls));
    check("returns quickly", Date.now() - t0 < 1000);
    check("console.warn is restored", env.console.warn === realWarn);
    check("both warnings still reached the original console", env.warns.length >= 2);
  }

  console.log("· ensurePyodideScript");
  {
    const env = { document: {}, loadPyodide: () => {} };
    let injected = 0;
    await Boot.ensurePyodideScript({ env, inject: async () => { injected++; } });
    check("no-op when loadPyodide already exists", injected === 0);

    const env2 = { document: {} };
    const tried = [];
    await Boot.ensurePyodideScript({
      env: env2,
      inject: async (doc, src, integrity) => {
        tried.push(src);
        if (src.startsWith("https://cdn.jsdelivr.net")) throw new Error("blocked");
        env2.loadPyodide = () => {};
        check("SRI is passed on the fallback script", integrity.startsWith("sha384-"));
      },
    });
    check("falls back to the next mirror when the primary script is blocked",
      tried.length === 2 && tried[1].startsWith("https://fastly.jsdelivr.net/") && tried[1].endsWith("/pyodide.js"), tried.join(" | "));

    let threw = false;
    try { await Boot.ensurePyodideScript({ env: { document: {} }, inject: async () => { throw new Error("nope"); } }); }
    catch { threw = true; }
    check("throws when no host works", threw);
  }

  console.log("· static drift guards");
  const read = (p) => fs.readFileSync(path.join(__dirname, "..", p), "utf8");
  const html = read("public/index.html");
  const term = read("public/assets/terminal.js");
  const runtime = read("public/assets/boot-runtime.js");
  const vercel = JSON.parse(read("vercel.json"));
  const csp = vercel.headers.flatMap((h) => h.headers).find((h) => h.key === "Content-Security-Policy").value;
  const directive = (name) => (csp.split(";").map((s) => s.trim()).find((s) => s.startsWith(name + " ")) || "");

  const tag = html.match(/<script[^>]*pyodide\/v([\d.]+)\/full\/pyodide\.js"[^>]*integrity="([^"]+)"/);
  check("index.html pins a Pyodide script with SRI", !!tag);
  check("boot-runtime.js VERSION matches index.html", !!tag && runtime.includes(`const VERSION = "${tag[1]}"`), tag && tag[1]);
  check("boot-runtime.js SRI matches index.html", !!tag && runtime.includes(`"${tag[2]}"`));
  check("terminal.js still pins the same Pyodide version as boot-prewarm/boot-runtime",
    !/pyodide\/v[\d.]+\/full\//.test(term) || term.match(/pyodide\/v([\d.]+)\/full\//)[1] === (tag && tag[1]));
  check("index.html loads boot-runtime.js before terminal.js",
    html.indexOf("assets/boot-runtime.js") > -1 && html.indexOf("assets/boot-runtime.js") < html.indexOf("assets/terminal.js"));
  check("boot() no longer stringifies the raw error", !/"BOOT FAILURE: " \+ err\b/.test(term));
  check("boot() routes runtime bring-up through FinmodelsBoot", /FinmodelsBoot\.bringUpRuntime\(/.test(term) && /FinmodelsBoot\.describeError\(/.test(term));
  for (const host of Boot.HOSTS) {
    check(`CSP script-src allows ${host}`, directive("script-src").includes(host + " ") || directive("script-src").endsWith(host));
    check(`CSP connect-src allows ${host}`, directive("connect-src").split(" ").includes(host));
  }

  console.log(`\n${passed} passed · ${failed} failed`);
  if (failed > 0) process.exit(1);
})().catch((e) => { console.error(e); process.exit(1); });
