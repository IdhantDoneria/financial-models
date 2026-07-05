// In-process tests for the (now-retired) founders promo, the password
// sign-in layer, and the admin desk (user directory + manual premium
// grants). Store in dev-memory mode; admin key is the dev default
// "devadmin".
//
//     node scripts/test_admin_founders.js

process.env.AUTH_DEV_MEMORY = "1";

const store = require("../api/_lib/store");

const handlers = {
  requestOtp: require("../api/_handlers/auth-request-otp.js"),
  verifyOtp: require("../api/_handlers/auth-verify-otp.js"),
  login: require("../api/_handlers/auth-login.js"),
  authConfig: require("../api/_handlers/auth-config.js"),
  usage: require("../api/usage.js"),
  admin: require("../api/admin.js"),
};

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

async function call(fn, { method = "GET", body, token, headers = {} } = {}) {
  const req = { method, body,
    headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers } };
  return new Promise((resolve, reject) => {
    const res = {
      _code: 200,
      setHeader() {},
      status(c) { this._code = c; return this; },
      json(o) { resolve({ code: this._code, body: o }); },
    };
    Promise.resolve(fn(req, res)).catch(reject);
  });
}

async function signup(email, extra = {}) {
  await store.del(`otp:cd:${email}`);   // tests move faster than the 60s cooldown
  const sent = await call(handlers.requestOtp, { method: "POST", body: { email } });
  return call(handlers.verifyOtp,
    { method: "POST", body: { email, code: sent.body.devCode, ...extra } });
}

const ADMIN = { "x-admin-key": "devadmin" };

(async () => {
  console.log("FOUNDERS PROMO (RETIRED) · PASSWORD LAYER · ADMIN DESK — in-memory store\n");

  // -- promo retired: FOUNDER_CAP=0, so nobody ever wins a slot ------------
  const cfg0 = await call(handlers.authConfig);
  check("config: founders promo reports 0 slots (retired)",
    cfg0.body.foundersLeft === 0 && cfg0.body.passwordLogin === true);

  const results = [];
  for (let i = 1; i <= 3; i++)
    results.push(await signup(`user${i}@example.com`, { password: `Str0ngPass!${i}` }));
  const winners = results.filter((r) => r.body.founder);
  check("founders: promo retired -> nobody wins a slot", winners.length === 0,
    `got ${winners.length}`);

  const use1 = await call(handlers.usage, { method: "GET", token: results[0].body.token });
  check("founders: signups stay on FREE 5/mo (no auto-grant)",
    use1.body.plan === "free" && use1.body.limit === 5 && use1.body.via === null);
  const again = await signup("user1@example.com");   // repeat sign-in: already has a password
  check("founders: re-login still never wins a slot", again.body.founder === null
    && again.code === 200
    && (await call(handlers.authConfig)).body.foundersLeft === 0);

  // -- password layer: compulsory on first signup --------------------------
  const nopass = await signup("nopass@example.com");
  check("password: required on first signup (no password -> 400)",
    nopass.code === 400 && /PASSWORD IS REQUIRED/.test(nopass.body.error));

  const pw = await signup("pwuser@example.com", { name: "P W", password: "hunter2!secure" });
  check("password: set during OTP verify", pw.body.passwordSet === true);
  const short = await signup("shortpw@example.com", { password: "short" });
  check("password: under 8 chars rejected", short.code === 400);

  const good = await call(handlers.login,
    { method: "POST", body: { email: "pwuser@example.com", password: "hunter2!secure" } });
  check("password: sign-in issues a session", good.code === 200 && !!good.body.token
    && good.body.user.name === "P W");
  const me = await call(handlers.usage, { method: "GET", token: good.body.token });
  check("password: session carries the same entitlement", me.code === 200);

  const wrong = await call(handlers.login,
    { method: "POST", body: { email: "pwuser@example.com", password: "wrong-pass" } });
  check("password: wrong password -> 401 with tries left",
    wrong.code === 401 && /TRIES LEFT/.test(wrong.body.error));

  // simulate a legacy account that pre-dates the compulsory-password
  // requirement (there's no way to create one through the public API any
  // more) to prove auth-login still degrades it gracefully to EMAIL CODE
  const legacy = JSON.parse(await store.get("user:user1@example.com"));
  delete legacy.pw; delete legacy.pwSetAt;
  await store.set("user:user1@example.com", JSON.stringify(legacy));
  const nopw = await call(handlers.login,
    { method: "POST", body: { email: "user1@example.com", password: "whatever123" } });
  check("password: legacy no-password account pointed back to email code",
    nopw.code === 401 && /EMAIL ME A CODE/.test(nopw.body.error));
  const relogin = await signup("user1@example.com", { password: "NewStr0ngPass!" });
  check("password: legacy account can set a password again via email code",
    relogin.code === 200 && relogin.body.passwordSet === true);
  let lock;
  for (let i = 0; i < 10; i++) {
    lock = await call(handlers.login,
      { method: "POST", body: { email: "pwuser@example.com", password: "wrong-" + i } });
  }
  check("password: brute force locked out after 10 tries", lock.code === 429);

  // -- admin desk ------------------------------------------------------------
  const noKey = await call(handlers.admin);
  check("admin: no key -> 401", noKey.code === 401);
  const badKey = await call(handlers.admin, { headers: { "x-admin-key": "guess" } });
  check("admin: wrong key -> 401", badKey.code === 401);

  const list = await call(handlers.admin, { headers: ADMIN });
  const emails = list.body.rows.map((r) => r.email);
  // 3 promo signups + pwuser (the short-password attempt was rejected
  // before any account was created).
  check("admin: directory lists every signed-up email (4)",
    list.body.totals.users === 4 && emails.includes("user2@example.com")
    && emails.includes("pwuser@example.com"));
  const row1 = list.body.rows.find((r) => r.email === "user1@example.com");
  // user1 has signed in 3 times by now: initial signup, a repeat sign-in,
  // and re-verifying via email code after the simulated legacy-account reset.
  check("admin: rows carry plan, sign-ins, usage (no founder auto-grant)",
    row1.founder === null && row1.plan === "free" && row1.via === null
    && row1.loginCount === 3 && typeof row1.usedThisMonth === "number");
  const rowPw = list.body.rows.find((r) => r.email === "pwuser@example.com");
  check("admin: password metadata visible (set + when, never the password itself)",
    rowPw.passwordSet === true && typeof rowPw.pwSetAt === "string" && rowPw.password === undefined);
  check("admin: founder totals right (promo retired)",
    list.body.totals.foundersClaimed === 0 && list.body.totals.foundersLeft === 0);
  check("admin: geo breakdown shape present (no visits tracked in this harness)",
    list.body.geo && list.body.geo.total === 0 && Array.isArray(list.body.geo.countries));

  // grant premium to someone who has never signed up
  const g = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "grant", email: "vip@bigbank.com", plan: "pro", days: 45 } });
  check("admin: grant by typed email + duration", g.body.ok && g.body.granted === "pro");
  const list2 = await call(handlers.admin, { headers: ADMIN });
  const vip = list2.body.rows.find((r) => r.email === "vip@bigbank.com");
  check("admin: not-yet-signed-up grantee visible in directory",
    !!vip && vip.signedUp === false && vip.plan === "pro" && vip.via === "grant");

  // ...and the pass is waiting when they do sign up
  const vipLogin = await signup("vip@bigbank.com", { name: "VIP", password: "Str0ngPass!VIP" });
  const vipUse = await call(handlers.usage, { method: "GET", token: vipLogin.body.token });
  check("admin: grantee signs up into the waiting plan",
    vipUse.body.plan === "pro" && vipUse.body.limit === 50 && vipUse.body.via === "grant");

  // stacking + revoke
  await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "grant", email: "vip@bigbank.com", plan: "pro", days: 45 } });
  const vip2 = (await call(handlers.admin, { headers: ADMIN }))
    .body.rows.find((r) => r.email === "vip@bigbank.com");
  check("admin: re-granting stacks the duration (~90d)",
    Date.parse(vip2.expiresAt) - Date.now() > 88 * 86_400_000);
  const rv = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "revoke", email: "vip@bigbank.com" } });
  const vipAfter = await call(handlers.usage, { method: "GET", token: vipLogin.body.token });
  check("admin: revoke drops the account to FREE immediately",
    rv.body.ok && vipAfter.body.plan === "free");
  const badDays = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "grant", email: "vip@bigbank.com", plan: "pro", days: 4000 } });
  check("admin: duration bounded to 1–365 days", badDays.code === 400);

  // -- admin: reset_password (the secure alternative to displaying one) ----
  // pwuser is already locked out from the brute-force test above (429
  // persists regardless of password state) — use a fresh account so the
  // reset's own effect is what's under test, not an unrelated lockout.
  await signup("resetme@example.com", { name: "Reset Me", password: "OriginalPass!1" });
  const rp = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "reset_password", email: "resetme@example.com" } });
  check("admin: reset_password clears the account's password",
    rp.code === 200 && rp.body.ok && rp.body.resetPassword === "resetme@example.com");
  const oldPwLogin = await call(handlers.login,
    { method: "POST", body: { email: "resetme@example.com", password: "OriginalPass!1" } });
  check("admin: old password rejected after reset (pointed back to email code)",
    oldPwLogin.code === 401 && /EMAIL ME A CODE/.test(oldPwLogin.body.error));
  const list3 = await call(handlers.admin, { headers: ADMIN });
  const rowPwAfter = list3.body.rows.find((r) => r.email === "resetme@example.com");
  check("admin: directory reflects the reset (passwordSet false)",
    rowPwAfter.passwordSet === false && rowPwAfter.pwSetAt === null);
  const rpMissing = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "reset_password", email: "nobody@example.com" } });
  check("admin: reset_password on an unknown email -> 404", rpMissing.code === 404);

  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
