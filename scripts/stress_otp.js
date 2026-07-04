// Stress + transport test for the OTP backend after the Gmail/Nodemailer switch.
//
// Runs entirely offline — no secrets, no network, no browser:
//   1. Transport selection: configured()/mode() gate correctly across every env
//      combo (unconfigured / dev-echo / gmail / resend), and Gmail wins when
//      several are present.
//   2. Gmail send path with nodemailer MOCKED (a Module._load shim, since the
//      package isn't installed in this repo — Vercel installs it from
//      package.json). Asserts sendOtp builds a correct message (from/to/subject/
//      html+text), reuses one pooled SMTP transport across sends, honours
//      EMAIL_FROM, and surfaces SMTP failures as thrown errors.
//   3. High-volume request->verify loop against the in-memory store: hundreds of
//      distinct users each complete the full passwordless flow; the 60s resend
//      cooldown, 5/hour cap and 5-attempt wrong-code lockout all hold under load.
//
//     node scripts/stress_otp.js

"use strict";

const Module = require("node:module");

// ---- Mock nodemailer -------------------------------------------------------
// email.js lazy-requires "nodemailer" only on a real Gmail send, so this shim
// is exactly what the "gmail" transport path exercises here.
const mock = { transports: 0, sends: [], failNext: null };
const _origLoad = Module._load;
Module._load = function (request) {
  if (request === "nodemailer") {
    return {
      createTransport(options) {
        mock.transports++;
        return {
          options,
          async sendMail(msg) {
            if (mock.failNext) { const e = mock.failNext; mock.failNext = null; throw e; }
            mock.sends.push(msg);
            return { messageId: `<mock-${mock.sends.length}@smtp.gmail.com>` };
          },
        };
      },
    };
  }
  return _origLoad.apply(this, arguments);
};

const EMAIL_PATH = require.resolve("../api/_lib/email.js");
const EMAIL_ENV = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "GMAIL_PASSWORD",
                   "RESEND_API_KEY", "EMAIL_FROM", "AUTH_DEV_MEMORY"];

/** Re-load email.js under a fresh env (its transport is chosen at module load). */
function loadEmail(env) {
  for (const k of EMAIL_ENV) delete process.env[k];
  Object.assign(process.env, env);
  delete require.cache[EMAIL_PATH];
  return require("../api/_lib/email.js");
}

let passed = 0, failed = 0;
function ok(cond, label) {
  if (cond) { passed++; console.log(`  ✓ ${label}`); }
  else { failed++; process.exitCode = 1; console.error(`  ✗ ${label}`); }
}

// ---- 1. transport selection ------------------------------------------------
function transportSelection() {
  console.log("· transport selection (configured / mode) across env combos");

  let e = loadEmail({});
  ok(e.configured() === false && e.mode() === "unconfigured", "no vars → unconfigured (OTP request 503-gated)");

  e = loadEmail({ AUTH_DEV_MEMORY: "1" });
  ok(e.configured() === true && e.mode() === "dev-echo", "AUTH_DEV_MEMORY only → dev-echo (test harness)");

  e = loadEmail({ RESEND_API_KEY: "re_test123" });
  ok(e.configured() === true && e.mode() === "resend", "Resend key only → resend (fallback)");

  e = loadEmail({ GMAIL_USER: "work@gmail.com", GMAIL_APP_PASSWORD: "abcd efgh ijkl mnop" });
  ok(e.configured() === true && e.mode() === "gmail", "Gmail creds → gmail (primary)");

  e = loadEmail({ AUTH_DEV_MEMORY: "1", RESEND_API_KEY: "re_x", GMAIL_USER: "w@gmail.com", GMAIL_APP_PASSWORD: "pw" });
  ok(e.mode() === "gmail", "Gmail takes priority over Resend + dev-echo when all present");

  e = loadEmail({ GMAIL_USER: "w@gmail.com" });
  ok(e.mode() === "unconfigured", "GMAIL_USER without app password → not enabled");
}

// ---- 2. gmail send path (nodemailer mocked) --------------------------------
async function gmailSendPath() {
  console.log("· gmail send path (nodemailer mocked)");
  mock.transports = 0; mock.sends = []; mock.failNext = null;

  const e = loadEmail({ GMAIL_USER: "finmodels.auth@gmail.com", GMAIL_APP_PASSWORD: "app-pass-16char" });

  const r1 = await e.sendOtp("client@example.com", "123456");
  ok(r1 && typeof r1.id === "string" && !r1.devEcho, `sendOtp returns a message id (${r1.id})`);

  const msg = mock.sends[0];
  ok(msg.to === "client@example.com", "recipient is the client's address");
  ok(msg.from.includes("finmodels.auth@gmail.com"), "From defaults to the Gmail account");
  ok(msg.subject.startsWith("123456"), "subject leads with the code");
  ok(/123456/.test(msg.html) && /123456/.test(msg.text), "code present in both HTML and text parts");

  await e.sendOtp("second@example.com", "654321");
  ok(mock.transports === 1 && mock.sends.length === 2, "single pooled SMTP transport reused across sends");

  mock.failNext = new Error("Invalid login: 535 Authentication failed");
  let threw = false;
  try { await e.sendOtp("x@example.com", "000000"); }
  catch (err) { threw = /gmail send failed/.test(err.message); }
  ok(threw, "SMTP auth failure surfaces as a thrown error (not a silent success)");

  const e2 = loadEmail({ GMAIL_USER: "acct@gmail.com", GMAIL_APP_PASSWORD: "pw", EMAIL_FROM: "FINMODELS <acct@gmail.com>" });
  mock.sends = [];
  await e2.sendOtp("z@example.com", "222333");
  ok(mock.sends[0].from === "FINMODELS <acct@gmail.com>", "EMAIL_FROM override is used verbatim");
}

// ---- 3. high-volume request -> verify (real handlers, memory store) --------
async function volumeFlow() {
  console.log("· high-volume request → verify (in-memory store, dev-echo mailer)");

  for (const k of EMAIL_ENV) delete process.env[k];
  process.env.AUTH_DEV_MEMORY = "1";
  for (const key of Object.keys(require.cache)) {
    if (/api[\\/](?:_lib|_handlers)[\\/]/.test(key)) delete require.cache[key];
  }
  const requestOtp = require("../api/_handlers/auth-request-otp.js");
  const verifyOtp = require("../api/_handlers/auth-verify-otp.js");
  const store = require("../api/_lib/store.js");

  const call = (h, body, token) => {
    const req = { method: "POST", body, headers: token ? { authorization: `Bearer ${token}` } : {} };
    const res = { code: 0, out: null, setHeader() {}, status(c) { this.code = c; return this; }, json(o) { this.out = o; } };
    return Promise.resolve(h(req, res)).then(() => res);
  };
  const other = (c) => (c === "000000" ? "111111" : "000000");

  const N = 400;
  const t0 = Date.now();
  let logins = 0; const tokens = new Set();
  for (let i = 0; i < N; i++) {
    const addr = `user${i}@example.com`;
    let r = await call(requestOtp, { email: addr });
    if (r.code !== 200 || !/^\d{6}$/.test(r.out.devCode || "")) { ok(false, `code issued for #${i}`); return; }
    const code = r.out.devCode;
    r = await call(verifyOtp, { email: addr, code: other(code) });
    if (r.code !== 401) { ok(false, `wrong code rejected for #${i}`); return; }
    r = await call(verifyOtp, { email: addr, code, name: `User ${i}` });
    if (r.code === 200 && r.out.token) { logins++; tokens.add(r.out.token); }
  }
  const ms = Date.now() - t0;
  ok(logins === N, `${logins}/${N} full request→verify→session flows succeeded`);
  ok(tokens.size === N, `${tokens.size} unique session tokens issued (no collisions)`);
  console.log(`    ${N} request+verify cycles in ${ms} ms (~${Math.round(N / (ms / 1000))}/s)`);

  // hourly cap: 5 sends land, the 6th is capped
  const burst = "burst@example.com";
  let sent = 0, capped = false;
  for (let i = 0; i < 8; i++) {
    const r = await call(requestOtp, { email: burst });
    if (r.code === 200) { sent++; await store.del(`otp:cd:${burst}`); }   // age out the 60s cooldown
    else if (r.code === 429 && /HOUR/.test(r.out.error)) { capped = true; break; }
  }
  ok(sent === 5 && capped, `hourly cap stops sends after 5 (sent=${sent}, capped=${capped})`);

  // 60s resend cooldown blocks an immediate second send
  const r2 = await call(requestOtp, { email: "cooldown@example.com" });
  const r3 = await call(requestOtp, { email: "cooldown@example.com" });
  ok(r2.code === 200 && r3.code === 429 && /WAIT 60S/.test(r3.out.error), "60s resend cooldown blocks immediate resend");

  // 5 wrong attempts burn the code even under load
  const la = "lock@example.com";
  await call(requestOtp, { email: la });
  await store.del(`otp:cd:${la}`);
  let rr = await call(requestOtp, { email: la });
  const good = rr.out.devCode;
  for (let i = 0; i < 5; i++) rr = await call(verifyOtp, { email: la, code: other(good) });
  rr = await call(verifyOtp, { email: la, code: good });
  ok(rr.code !== 200, "code burned after 5 wrong attempts (correct code then refused)");
}

(async () => {
  transportSelection();
  await gmailSendPath();
  await volumeFlow();
  console.log(`\n${failed ? `FAILURES: ${failed} check(s) above` : `ALL ${passed} STRESS CHECKS PASS`}`);
})().catch((e) => { console.error("HARNESS ERROR:", e); process.exit(1); });
