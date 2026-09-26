// Plans, metering and activity tracking with NO payment gateway — how
// production runs before Razorpay is connected. The operator assigns plans and
// switches enforcement from the admin desk; every signed-in account's usage is
// counted and its activity recorded server-side.
//
//     node scripts/test_manual_billing.js

process.env.AUTH_DEV_MEMORY = "1";
process.env.DEV_CHECKOUT = "0";          // in-memory store, but no (fake) checkout

const store = require("../api/_lib/store");
const B = require("../api/_lib/billing");

const handlers = {
  requestOtp: require("../api/_handlers/auth-request-otp.js"),
  verifyOtp: require("../api/_handlers/auth-verify-otp.js"),
  login: require("../api/_handlers/auth-login.js"),
  usage: require("../api/usage.js"),
  admin: require("../api/admin.js"),
  config: require("../api/_handlers/billing-config.js"),
  order: require("../api/_handlers/billing-order.js"),
};

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

async function call(fn, { method = "GET", body, token, headers = {}, url = "/" } = {}) {
  const req = { method, body, url,
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
  await store.del(`otp:cd:${email}`);
  const sent = await call(handlers.requestOtp, { method: "POST", body: { email } });
  return call(handlers.verifyOtp, { method: "POST", body: { email, code: sent.body.devCode, ...extra } });
}

const ADMIN = { "x-admin-key": "devadmin" };
const admin = (body) => call(handlers.admin, { method: "POST", headers: ADMIN, body });
const adminGet = (url) => call(handlers.admin, { headers: ADMIN, url });

(async () => {
  console.log("MANUAL BILLING (no checkout) · METERING · ACTIVITY — in-memory store\n");

  check("harness: checkout is off, the store is on", !B.configured() && store.configured());

  // -- public config --------------------------------------------------------
  const cfg = (await call(handlers.config)).body;
  check("config: plans are enforced (billing true) with no checkout",
    cfg.billing === true && cfg.checkout === false && cfg.tracking === true, JSON.stringify(cfg).slice(0, 200));
  check("config: no gateway key is published", cfg.keyId === null && cfg.devFake === false);
  check("config: the Pro models stay open by default", cfg.proOpen === true);
  const order = await call(handlers.order, { method: "POST", body: { plan: "pro", period: "monthly" } });
  check("checkout: creating an order is refused (503)", order.code === 503);

  // -- sign-in is recorded ----------------------------------------------------
  const s = await signup("ana@example.com", { name: "Ana", password: "Str0ngPass!1" });
  const token = s.body.token;
  check("signup: session issued", !!token, JSON.stringify(s.body).slice(0, 160));
  const login = await call(handlers.login, { method: "POST", body: { email: "ana@example.com", password: "Str0ngPass!1" } });
  check("password sign-in works", login.code === 200);

  // -- metering: the free allowance, then 402 -------------------------------
  const g0 = await call(handlers.usage, { token });
  check("usage: FREE, 10 a month, enforced, no checkout",
    g0.body.plan === "free" && g0.body.limit === 10 && g0.body.metered === true && g0.body.checkout === false);
  const limit = g0.body.limit;
  let last;
  for (let i = 0; i < limit; i++) {
    last = await call(handlers.usage, { method: "POST", token, body: { source: "ticker", ticker: `T${i}` } });
  }
  check(`usage: ${limit} analyses allowed`, last.code === 200 && last.body.used === limit);
  const over = await call(handlers.usage, { method: "POST", token, body: { source: "ticker", ticker: "NOPE" } });
  check("usage: one more is refused with 402, pointing to the operator, not a checkout",
    over.code === 402 && /ASK THE OPERATOR/.test(over.body.error) && !/UPGRADE IN MENU/.test(over.body.error));

  // -- events the browser reports are logged but not counted ----------------
  const rep = await call(handlers.usage, { method: "POST", token,
    body: { event: "report", models: ["Discounted Cash Flow", "Reverse DCF / Market-Implied Expectations"], mode: "auto", ticker: "AAPL", secret: "x" } });
  check("event: a report run is logged without using the allowance", rep.code === 200 && rep.body.used === limit);
  const forged = await call(handlers.usage, { method: "POST", token, body: { event: "signin" } });
  check("event: the browser cannot forge a sign-in or an analysis", forged.code === 400);
  const noSess = await call(handlers.usage, { method: "POST", body: { event: "report" } });
  check("event: no session, no log (401 while enforced)", noSess.code === 401);

  // -- the activity log -------------------------------------------------------
  const mine = (await adminGet("/api/admin?action=activity&email=ana@example.com")).body;
  const types = mine.events.map((e) => e.type);
  check("activity: sign-ins, analyses and the report are all recorded, newest first",
    types[0] === "report" && types.filter((t) => t === "analysis").length === limit
    && types.filter((t) => t === "signin").length === 2, types.join(","));
  const reportEv = mine.events[0];
  check("activity: only whitelisted detail is stored (no 'secret' field)",
    reportEv.ticker === "AAPL" && Array.isArray(reportEv.models) && !("secret" in reportEv));
  check("activity: the refused analysis is not logged as one",
    !mine.events.some((e) => e.type === "analysis" && e.ticker === "NOPE"));
  const methods = mine.events.filter((e) => e.type === "signin").map((e) => e.method).sort().join(",");
  check("activity: each sign-in records its method", methods === "email-code,password", methods);
  const all = (await adminGet("/api/admin?action=activity")).body;
  check("activity: the all-accounts feed and 14 days of daily counts",
    all.events.length >= limit + 3 && all.daily.length === 14 && all.daily[0].analysis === limit
    && all.daily[0].signin === 2 && all.daily[0].report === 1);
  const badEmail = await adminGet("/api/admin?action=activity&email=not-an-email");
  check("activity: a malformed email filter is refused (400)", badEmail.code === 400);
  const anon = await call(handlers.admin, { url: "/api/admin?action=activity" });
  check("activity: the log needs the admin key", anon.code === 401);

  // -- the directory ------------------------------------------------------------
  const dir = (await adminGet("/api/admin")).body;
  const row = dir.rows.find((r) => r.email === "ana@example.com");
  check("directory: usage, limit and last-active per account",
    row.usedThisMonth === limit && row.limit === 10 && !!row.lastActiveAt);
  check("directory: reports the metering switch", dir.proAccess.metering === true && dir.proAccess.billingLive === false);

  // -- the operator manages access -----------------------------------------
  const reset = await admin({ action: "reset_usage", email: "ana@example.com" });
  check("admin: reset_usage zeroes this month", reset.code === 200 && (await B.getUsed("ana@example.com")) === 0);
  await admin({ action: "grant", email: "ana@example.com", plan: "pro", days: 30 });
  const g1 = await call(handlers.usage, { token });
  check("admin: a grant moves the account to ANALYST PRO (50 a month)",
    g1.body.plan === "pro" && g1.body.limit === 50 && g1.body.via === "grant");

  const mBad = await admin({ action: "metering", on: "yes" });
  check("admin: metering needs a boolean", mBad.code === 400);
  const mNoKey = await call(handlers.admin, { method: "POST", body: { action: "metering", on: false } });
  check("admin: metering needs the admin key", mNoKey.code === 401);
  const mOff = await admin({ action: "metering", on: false });
  check("admin: enforcement can be switched off", mOff.code === 200 && mOff.body.proAccess.metering === false);
  const cfgOff = (await call(handlers.config)).body;
  check("config: then billing is false but tracking stays on", cfgOff.billing === false && cfgOff.tracking === true);

  await admin({ action: "revoke", email: "ana@example.com" });
  for (let i = 0; i < limit + 2; i++) await call(handlers.usage, { method: "POST", token, body: { source: "pdf" } });
  const counted = await call(handlers.usage, { token });
  check("not enforced: a FREE account can go past 10, and it is still counted",
    counted.body.used === limit + 2 && counted.body.metered === false);
  const guest = await call(handlers.usage, {});
  check("not enforced: no session gets the unmetered reply, not a 401", guest.code === 200 && guest.body.metered === false);
  await admin({ action: "grant", email: "ana@example.com", plan: "unlimited", days: 7 });
  const withGrant = await call(handlers.usage, { token });
  check("not enforced: a granted plan is still reported (grant field for older clients)",
    withGrant.body.grant && withGrant.body.grant.plan === "unlimited");
  const pdfEv = (await adminGet("/api/admin?action=activity&email=ana@example.com")).body.events[0];
  check("not enforced: analyses are still logged (a PDF as 'pdf', nothing else)",
    pdfEv.type === "analysis" && pdfEv.source === "pdf" && !pdfEv.ticker);

  const mOn = await admin({ action: "metering", on: true });
  check("admin: enforcement back on", mOn.body.proAccess.metering === true && (await B.metering()) === true);

  // -- caps -------------------------------------------------------------------
  const activity = require("../api/_lib/activity");
  for (let i = 0; i < activity.PER_USER + 20; i++) await activity.track("cap@example.com", "plan_view");
  check(`activity: an account keeps only its last ${activity.PER_USER} events`,
    (await activity.recent("cap@example.com", 1000)).length === activity.PER_USER);
  for (let i = 0; i < activity.ALL; i++) await activity.track(`u${i % 7}@example.com`, "plan_view");
  check(`activity: the all-accounts feed keeps only ${activity.ALL}`,
    (await activity.recent(null, 1000)).length === activity.ALL);
  const long = await activity.track("cap@example.com", "analysis", { source: "ticker", ticker: "X".repeat(500) });
  check("activity: detail values are length-capped", long.ticker.length === 60);

  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
