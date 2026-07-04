// In-process tests for the Razorpay billing + usage-metering backend.
// Runs the real api/*.js handlers with the store in dev-memory mode and the
// gateway in dev-fake mode (orders get dev ids; signatures verify against
// the fixed dev secret) — the full purchase lifecycle without credentials.
//
//     node scripts/test_billing_api.js

process.env.AUTH_DEV_MEMORY = "1";

const crypto = require("node:crypto");
const store = require("../api/_lib/store");

const handlers = {
  requestOtp: require("../api/_handlers/auth-request-otp.js"),
  verifyOtp: require("../api/_handlers/auth-verify-otp.js"),
  billingConfig: require("../api/_handlers/billing-config.js"),
  billingOrder: require("../api/_handlers/billing-order.js"),
  billingVerify: require("../api/_handlers/billing-verify.js"),
  billingWebhook: require("../api/_handlers/billing-webhook.js"),
  usage: require("../api/usage.js"),
};

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

/** Invoke a handler the way Vercel would; returns {code, body}. */
async function call(fn, { method = "GET", body, token, headers = {}, rawBody } = {}) {
  const req = {
    method,
    body: rawBody !== undefined ? rawBody : body,
    headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers },
  };
  return new Promise((resolve, reject) => {
    const res = {
      _code: 200,
      setHeader() {},
      status(c) { this._code = c; return this; },
      json(o) { resolve({ code: this._code, body: o }); },
      end(s) { resolve({ code: this._code, body: s ? JSON.parse(s) : {} }); },
    };
    Promise.resolve(fn(req, res)).catch(reject);
  });
}

const devSig = (msg) => crypto.createHmac("sha256", "devsecret").update(msg).digest("hex");

async function login(email) {
  const sent = await call(handlers.requestOtp, { method: "POST", body: { email } });
  const ver = await call(handlers.verifyOtp,
    { method: "POST", body: { email, code: sent.body.devCode, name: "Test User" } });
  return ver.body.token;
}

async function buy(token, plan) {
  const order = await call(handlers.billingOrder, { method: "POST", token, body: { plan } });
  if (!order.body.ok) return order;
  const paymentId = "pay_test" + crypto.randomBytes(6).toString("hex");
  return call(handlers.billingVerify, {
    method: "POST", token,
    body: {
      razorpay_order_id: order.body.orderId,
      razorpay_payment_id: paymentId,
      razorpay_signature: devSig(`${order.body.orderId}|${paymentId}`),
    },
  });
}

(async () => {
  console.log("BILLING BACKEND — dev-fake gateway, in-memory store\n");

  // The founders promo gifts the first 20 signups a free month of UNLIMITED;
  // this suite tests the paid paths, so start with the promo exhausted.
  await store.set("founders:claimed", "20");

  // -- catalogue ---------------------------------------------------------
  const cfg = await call(handlers.billingConfig);
  check("config: billing on (dev-fake)", cfg.body.billing === true && cfg.body.devFake === true);
  const byId = Object.fromEntries(cfg.body.plans.map((p) => [p.id, p]));
  check("config: PRO is ₹299 / 50 uploads", byId.pro.priceInr === 299 && byId.pro.uploads === 50);
  check("config: UNLIMITED is ₹499 (MRP ₹599) / unlimited",
    byId.unlimited.priceInr === 499 && byId.unlimited.mrpInr === 599 && byId.unlimited.uploads === null);
  check("config: FREE tier is 5 uploads", byId.free.uploads === 5 && byId.free.priceInr === 0);

  // -- auth requirements ---------------------------------------------------
  const anon = await call(handlers.billingOrder, { method: "POST", body: { plan: "pro" } });
  check("order: 401 without a session", anon.code === 401);
  const anonUse = await call(handlers.usage, { method: "POST" });
  check("usage: 401 without a session", anonUse.code === 401);

  const token = await login("vp@example.com");
  check("login: OTP session issued", !!token);

  const badPlan = await call(handlers.billingOrder, { method: "POST", token, body: { plan: "gold" } });
  check("order: unknown plan rejected", badPlan.code === 400);

  // -- free-tier metering: 5 uploads then a 402 ---------------------------
  const first = await call(handlers.usage, { method: "GET", token });
  check("usage: fresh account on FREE 0/5",
    first.body.plan === "free" && first.body.used === 0 && first.body.limit === 5);
  let last;
  for (let i = 0; i < 5; i++) last = await call(handlers.usage, { method: "POST", token });
  check("usage: five uploads consumed", last.code === 200 && last.body.used === 5);
  const sixth = await call(handlers.usage, { method: "POST", token });
  check("usage: sixth upload -> 402 with upgrade pointer",
    sixth.code === 402 && /UPGRADE/.test(sixth.body.error));

  // -- purchase PRO --------------------------------------------------------
  const order = await call(handlers.billingOrder, { method: "POST", token, body: { plan: "pro" } });
  check("order: created with authoritative amount 29900", order.body.ok && order.body.amount === 29900);
  const badSig = await call(handlers.billingVerify, {
    method: "POST", token,
    body: { razorpay_order_id: order.body.orderId, razorpay_payment_id: "pay_x",
            razorpay_signature: "deadbeef" },
  });
  check("verify: forged signature -> 401", badSig.code === 401);

  const stranger = await login("intruder@example.com");
  const pid = "pay_hijack1";
  const hijack = await call(handlers.billingVerify, {
    method: "POST", token: stranger,
    body: { razorpay_order_id: order.body.orderId, razorpay_payment_id: pid,
            razorpay_signature: devSig(`${order.body.orderId}|${pid}`) },
  });
  check("verify: another account cannot redeem the order", hijack.code === 403);

  const pid2 = "pay_good0001";
  const good = await call(handlers.billingVerify, {
    method: "POST", token,
    body: { razorpay_order_id: order.body.orderId, razorpay_payment_id: pid2,
            razorpay_signature: devSig(`${order.body.orderId}|${pid2}`) },
  });
  check("verify: valid signature activates PRO", good.body.ok && good.body.plan === "pro");
  const replay = await call(handlers.billingVerify, {
    method: "POST", token,
    body: { razorpay_order_id: order.body.orderId, razorpay_payment_id: pid2,
            razorpay_signature: devSig(`${order.body.orderId}|${pid2}`) },
  });
  check("verify: order is single-use (replay -> 404)", replay.code === 404);

  const proUse = await call(handlers.usage, { method: "POST", token });
  check("usage: PRO unblocks uploads (6/50)",
    proUse.code === 200 && proUse.body.plan === "pro" && proUse.body.limit === 50 && proUse.body.used === 6);

  // -- upgrade to UNLIMITED w/ day carry-over ------------------------------
  const up = await buy(token, "unlimited");
  check("upgrade: UNLIMITED activates over PRO", up.body.ok && up.body.plan === "unlimited");
  check("upgrade: unused PRO days carried over",
    Date.parse(up.body.subscription.expiresAt) - Date.now() > 59 * 86_400_000);
  const unlUse = await call(handlers.usage, { method: "POST", token });
  check("usage: UNLIMITED has no cap", unlUse.code === 200 && unlUse.body.limit === null);

  // -- webhook fallback (buyer's tab died before verify) -------------------
  const whToken = await login("webhook-buyer@example.com");
  const whOrder = await call(handlers.billingOrder, { method: "POST", token: whToken, body: { plan: "pro" } });
  const whBody = JSON.stringify({
    event: "payment.captured",
    payload: { payment: { entity: { id: "pay_wh1", order_id: whOrder.body.orderId,
      notes: { email: "webhook-buyer@example.com", plan: "pro" } } } },
  });
  const whBad = await call(handlers.billingWebhook, {
    method: "POST", rawBody: whBody, headers: { "x-razorpay-signature": "bad" } });
  check("webhook: bad signature -> 401", whBad.code === 401);
  const wh = await call(handlers.billingWebhook, {
    method: "POST", rawBody: whBody, headers: { "x-razorpay-signature": devSig(whBody) } });
  check("webhook: payment.captured activates the plan", wh.body.ok && wh.body.activated === "pro");
  const whUse = await call(handlers.usage, { method: "GET", token: whToken });
  check("webhook: buyer's entitlement is live", whUse.body.plan === "pro" && whUse.body.limit === 50);

  // -- expiry: a lapsed pass falls back to FREE ----------------------------
  await store.set("sub:webhook-buyer@example.com", JSON.stringify({
    plan: "pro", paymentId: "pay_wh1", orderId: whOrder.body.orderId,
    activatedAt: new Date(Date.now() - 40 * 86_400_000).toISOString(),
    expiresAt: new Date(Date.now() - 10 * 86_400_000).toISOString(),
  }));
  const lapsed = await call(handlers.usage, { method: "GET", token: whToken });
  check("expiry: lapsed PRO falls back to FREE 5/mo",
    lapsed.body.plan === "free" && lapsed.body.limit === 5);

  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
