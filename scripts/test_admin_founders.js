// In-process tests for the founders promo, the password sign-in layer, and
// the admin desk (user directory + manual premium grants). Store in
// dev-memory mode; admin key is the dev default "devadmin".
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
  console.log("FOUNDERS PROMO · PASSWORD LAYER · ADMIN DESK — in-memory store\n");

  // -- promo advertised before anyone signs up ----------------------------
  const cfg0 = await call(handlers.authConfig);
  check("config: 20 founder slots advertised", cfg0.body.foundersLeft === 20
    && cfg0.body.founderPlanName === "DESK UNLIMITED" && cfg0.body.passwordLogin === true);

  // -- 22 signups: exactly the first 20 win a free month ------------------
  const results = [];
  for (let i = 1; i <= 22; i++) results.push(await signup(`user${i}@example.com`));
  const winners = results.filter((r) => r.body.founder);
  check("founders: exactly 20 of 22 signups win", winners.length === 20,
    `got ${winners.length}`);
  check("founders: slots numbered 1..20 in order",
    results[0].body.founder === 1 && results[19].body.founder === 20
    && results[20].body.founder === null && results[21].body.founder === null);

  const use1 = await call(handlers.usage, { method: "GET", token: results[0].body.token });
  check("founders: winner holds DESK UNLIMITED via 'founder'",
    use1.body.plan === "unlimited" && use1.body.limit === null
    && use1.body.via === "founder" && use1.body.founderNo === 1);
  const use21 = await call(handlers.usage, { method: "GET", token: results[20].body.token });
  check("founders: user #21 stays on FREE 5/mo",
    use21.body.plan === "free" && use21.body.limit === 5);
  const cfg1 = await call(handlers.authConfig);
  check("founders: slots exhausted -> 0 left", cfg1.body.foundersLeft === 0);
  const again = await signup("user1@example.com");
  check("founders: re-login never draws twice", again.body.founder === 1
    && (await call(handlers.authConfig)).body.foundersLeft === 0);

  // -- password layer -------------------------------------------------------
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
  const nopw = await call(handlers.login,
    { method: "POST", body: { email: "user1@example.com", password: "whatever123" } });
  check("password: OTP-only account pointed back to email code",
    nopw.code === 401 && /EMAIL ME A CODE/.test(nopw.body.error));
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
  // 22 promo signups + pwuser (the short-password attempt was rejected
  // before any account was created).
  check("admin: directory lists every signed-up email (23)",
    list.body.totals.users === 23 && emails.includes("user7@example.com")
    && emails.includes("pwuser@example.com"));
  const row1 = list.body.rows.find((r) => r.email === "user1@example.com");
  check("admin: rows carry founder #, plan, sign-ins, usage",
    row1.founder === 1 && row1.plan === "unlimited" && row1.via === "founder"
    && row1.loginCount === 2 && typeof row1.usedThisMonth === "number");
  check("admin: password flag visible",
    list.body.rows.find((r) => r.email === "pwuser@example.com").passwordSet === true);
  check("admin: founder totals right",
    list.body.totals.foundersClaimed === 20 && list.body.totals.foundersLeft === 0);

  // grant premium to someone who has never signed up
  const g = await call(handlers.admin, { method: "POST", headers: ADMIN,
    body: { action: "grant", email: "vip@bigbank.com", plan: "pro", days: 45 } });
  check("admin: grant by typed email + duration", g.body.ok && g.body.granted === "pro");
  const list2 = await call(handlers.admin, { headers: ADMIN });
  const vip = list2.body.rows.find((r) => r.email === "vip@bigbank.com");
  check("admin: not-yet-signed-up grantee visible in directory",
    !!vip && vip.signedUp === false && vip.plan === "pro" && vip.via === "grant");

  // ...and the pass is waiting when they do sign up
  const vipLogin = await signup("vip@bigbank.com", { name: "VIP" });
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

  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
