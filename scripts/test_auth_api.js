// Integration test for the OTP auth backend (api/auth-*.js).
//
// Runs the real handlers in-process against the in-memory store
// (AUTH_DEV_MEMORY=1) with the email transport in echo mode, covering the
// whole lifecycle: request -> rate limits -> wrong-code lockout -> verify ->
// session -> /me -> logout -> revoked. No network, no browser.
//
//     node scripts/test_auth_api.js

process.env.AUTH_DEV_MEMORY = "1";
delete process.env.RESEND_API_KEY;

const assert = require("node:assert");
const requestOtp = require("../api/_handlers/auth-request-otp.js");
const verifyOtp = require("../api/_handlers/auth-verify-otp.js");
const me = require("../api/_handlers/auth-me.js");
const logout = require("../api/_handlers/auth-logout.js");
const config = require("../api/_handlers/auth-config.js");

function call(handler, { method = "POST", body, token } = {}) {
  const req = { method, body, headers: token ? { authorization: `Bearer ${token}` } : {} };
  const res = {
    headers: {}, code: 0, out: null,
    setHeader(k, v) { this.headers[k] = v; },
    status(c) { this.code = c; return this; },
    json(o) { this.out = o; },
  };
  return Promise.resolve(handler(req, res)).then(() => res);
}

let passed = 0;
function ok(cond, label) {
  if (!cond) { console.error(`  ✗ ${label}`); process.exitCode = 1; }
  else { console.log(`  ✓ ${label}`); passed++; }
}

(async () => {
  console.log("· auth-config reports dev backend live");
  let r = await call(config, { method: "GET" });
  ok(r.out.serverAuth === true && r.out.storage === "memory-dev" && r.out.email === "dev-echo",
     `serverAuth on (storage=${r.out.storage}, email=${r.out.email})`);

  console.log("· request-otp validation + issuance");
  r = await call(requestOtp, { body: { email: "not-an-email" } });
  ok(r.code === 400, "rejects malformed email (400)");
  r = await call(requestOtp, { method: "GET" });
  ok(r.code === 405, "rejects non-POST (405)");
  r = await call(requestOtp, { body: { email: "VP@Example.com" } });
  ok(r.code === 200 && r.out.sent && /^\d{6}$/.test(r.out.devCode),
     `issues a 6-digit code (dev echo ${r.out.devCode})`);
  const code = r.out.devCode;

  console.log("· resend cooldown + hourly cap");
  r = await call(requestOtp, { body: { email: "vp@example.com" } });
  ok(r.code === 429 && /WAIT 60S/.test(r.out.error), "60s resend cooldown enforced (429)");

  console.log("· verify: wrong-code attempts then lockout");
  r = await call(verifyOtp, { body: { email: "vp@example.com", code: "000000" } });
  const wrong = code === "000000" ? "111111" : "000000";
  for (let i = r.out.error.includes("ATTEMPT") ? 2 : 1; i <= 4; i++)
    r = await call(verifyOtp, { body: { email: "vp@example.com", code: wrong } });
  ok(r.code === 401 && /1 ATTEMPT LEFT/.test(r.out.error), `attempt countdown (${r.out.error})`);
  r = await call(verifyOtp, { body: { email: "vp@example.com", code: wrong } });
  ok(r.code === 401 || r.code === 429, "5th wrong attempt locks the code");
  r = await call(verifyOtp, { body: { email: "vp@example.com", code } });
  ok(r.code !== 200, "correct code refused after lockout (code burned)");

  console.log("· password is compulsory for a brand-new account");
  r = await call(requestOtp, { body: { email: "nopw@example.com" } });
  r = await call(verifyOtp, { body: { email: "nopw@example.com", code: r.out.devCode } });
  ok(r.code === 400 && /PASSWORD IS REQUIRED/.test(r.out.error),
     `first signup without a password is rejected (${r.out.error})`);

  console.log("· fresh code -> successful login = signup");
  // separate address (the first one is inside its send cooldown)
  r = await call(requestOtp, { body: { email: "md@example.com" } });
  const code2 = r.out.devCode;
  r = await call(verifyOtp, { body: { email: "md@example.com", code: code2,
    name: "Morgan Delaney", password: "hunter2!secure" } });
  ok(r.code === 200 && r.out.token && r.out.user.loginCount === 1 && r.out.user.name === "Morgan Delaney"
     && r.out.passwordSet === true,
     `login ok — token issued, profile stored (loginCount=${r.out.user.loginCount})`);
  const token = r.out.token;
  r = await call(verifyOtp, { body: { email: "md@example.com", code: code2 } });
  ok(r.code === 400, "code is single-use (replay refused)");

  console.log("· /me with the bearer token");
  r = await call(me, { method: "GET", token });
  ok(r.code === 200 && r.out.user.email === "md@example.com", "session validates, profile returned");
  r = await call(me, { method: "GET", token: "forged-token" });
  ok(r.code === 401, "forged token rejected (401)");
  r = await call(me, { method: "GET" });
  ok(r.code === 401, "missing token rejected (401)");

  console.log("· logout revokes the session");
  r = await call(logout, { token });
  ok(r.code === 200, "logout ok");
  r = await call(me, { method: "GET", token });
  ok(r.code === 401, "token dead after logout");

  console.log("· second login increments loginCount");
  await new Promise((s) => setTimeout(s, 10));
  // cooldown applies per-address; use the store directly to age it out
  const store = require("../api/_lib/store.js");
  await store.del("otp:cd:md@example.com");
  r = await call(requestOtp, { body: { email: "md@example.com" } });
  r = await call(verifyOtp, { body: { email: "md@example.com", code: r.out.devCode } });
  ok(r.code === 200 && r.out.user.loginCount === 2 && r.out.user.name === "Morgan Delaney",
     `profile persisted across logins (loginCount=${r.out.user.loginCount}, name kept)`);

  console.log(`\n${process.exitCode ? "FAILURES ABOVE" : `ALL ${passed} BACKEND CHECKS PASS`}`);
})().catch((e) => { console.error("HARNESS ERROR:", e); process.exit(1); });
