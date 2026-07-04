// api/_lib/billing.js — Razorpay payments + plan/usage accounting.
//
// Production: set RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET (dashboard → API
// keys). Orders are created server-side (amounts are authoritative HERE,
// never trusted from the client), paid through Razorpay Checkout on the
// front end, then verified server-side with the documented
// HMAC-SHA256(order_id|payment_id, key_secret) signature check. An optional
// webhook (RAZORPAY_WEBHOOK_SECRET) activates plans even if the buyer's tab
// dies before the client-side verify.
//
// Plans are 30-day passes bought with one-time orders (no dashboard plan
// objects needed): FREE 5 uploads/mo · ANALYST PRO 50 uploads/mo @ ₹299 ·
// DESK UNLIMITED @ ₹499 (MRP ₹599). "Upload" = one IB-desk PDF analysis.
//
// Local testing: with AUTH_DEV_MEMORY=1 and no real keys, a fake gateway
// takes over — orders get dev ids and signatures verify against the fixed
// secret "devsecret" so the whole purchase flow runs offline.

const crypto = require("crypto");
const store = require("./store");

const KEY_ID = process.env.RAZORPAY_KEY_ID || "";
const KEY_SECRET = process.env.RAZORPAY_KEY_SECRET || "";
const WEBHOOK_SECRET = process.env.RAZORPAY_WEBHOOK_SECRET || "";
const DEV = process.env.AUTH_DEV_MEMORY === "1" && !(KEY_ID && KEY_SECRET);
const DEV_SECRET = "devsecret";

const PLAN_TTL = 30 * 86_400;      // seconds — every paid plan is a 30-day pass
const ORDER_TTL = 3600;            // pending order records live 1 hour

//: Authoritative catalogue. `amount` is paise (Razorpay's unit); `uploads`
//  null = unlimited; `mrp` renders as a struck-through anchor price.
const PLANS = {
  free: { id: "free", name: "FREE", amount: 0, uploads: 5,
          blurb: "5 company uploads / month · all 10 models · SCEN engine" },
  pro: { id: "pro", name: "ANALYST PRO", amount: 29_900, uploads: 50,
         blurb: "50 company uploads / month · everything in FREE" },
  unlimited: { id: "unlimited", name: "DESK UNLIMITED", amount: 49_900, mrp: 59_900,
               uploads: null, blurb: "Unlimited uploads · everything in PRO" },
};

const configured = () => DEV || !!(KEY_ID && KEY_SECRET);
const mode = () => (DEV ? "dev-fake" : KEY_ID && KEY_SECRET ? "razorpay" : "unconfigured");
const keyId = () => (DEV ? "rzp_test_devfake" : KEY_ID);
const secret = () => (DEV ? DEV_SECRET : KEY_SECRET);

/* ----------------------------- gateway --------------------------------- */
async function createOrder(plan, email) {
  const p = PLANS[plan];
  if (DEV) {
    return { id: "order_dev" + crypto.randomBytes(8).toString("hex"),
             amount: p.amount, currency: "INR" };
  }
  const r = await fetch("https://api.razorpay.com/v1/orders", {
    method: "POST",
    headers: {
      Authorization: "Basic " + Buffer.from(`${KEY_ID}:${KEY_SECRET}`).toString("base64"),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      amount: p.amount, currency: "INR",
      receipt: `fm-${plan}-${Date.now()}`.slice(0, 40),
      notes: { plan, email, product: "finmodels-terminal" },
    }),
  });
  const j = await r.json();
  if (!r.ok || !j.id) {
    throw new Error(`razorpay order failed: ${(j.error && j.error.description) || r.status}`);
  }
  return j;
}

const hmac = (body, key) => crypto.createHmac("sha256", key).update(body).digest("hex");

function timingSafeEq(a, b) {
  const ba = Buffer.from(String(a)), bb = Buffer.from(String(b));
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

//: Checkout signature — HMAC-SHA256("order_id|payment_id", key_secret).
const verifyCheckoutSig = (orderId, paymentId, sig) =>
  timingSafeEq(hmac(`${orderId}|${paymentId}`, secret()), sig);

//: Webhook signature — HMAC-SHA256(raw request body, webhook secret).
const verifyWebhookSig = (rawBody, sig) =>
  timingSafeEq(hmac(rawBody, DEV ? DEV_SECRET : WEBHOOK_SECRET), sig);

/* --------------------------- subscriptions ----------------------------- */
async function getSub(email) {
  try { return JSON.parse((await store.get(`sub:${email}`)) || "null"); }
  catch { return null; }
}

//: The plan a user is entitled to right now (expired passes fall to free).
async function effectivePlan(email) {
  const sub = await getSub(email);
  if (sub && PLANS[sub.plan] && sub.expiresAt && Date.parse(sub.expiresAt) > Date.now()) {
    return { plan: sub.plan, sub };
  }
  return { plan: "free", sub: null };
}

//: Idempotent activation — the checkout verify and the webhook can both
//  fire for one payment; the second write is a harmless no-op re-set.
async function activate(email, plan, paymentId, orderId, via) {
  const existing = await getSub(email);
  if (existing && existing.paymentId === paymentId) return existing;
  const now = Date.now();
  //: Renewing/upgrading before expiry credits the unused days.
  const carry = existing && PLANS[existing.plan] && Date.parse(existing.expiresAt) > now
    ? Date.parse(existing.expiresAt) - now : 0;
  const sub = {
    plan, paymentId, orderId, via,
    activatedAt: new Date(now).toISOString(),
    expiresAt: new Date(now + PLAN_TTL * 1000 + carry).toISOString(),
  };
  await store.set(`sub:${email}`, JSON.stringify(sub));
  return sub;
}

/* ------------------------- grants & founders ---------------------------- *
 * Non-payment activations: the first-20 founders promo (every one of the
 * first 20 accounts gets a free month of DESK UNLIMITED, claimed atomically
 * at signup) and operator grants (the admin desk types an email + duration
 * -> free premium). Both write the same sub:<email> record the paywall
 * reads, tagged with `via` so the UI can say where the access came from.   */
const FOUNDER_CAP = 20;
const FOUNDER_PLAN = "unlimited";
const FOUNDER_DAYS = 30;

//: Days stack on an active pass (extend from its expiry), otherwise from now.
async function grant(email, plan, days, via, extra = {}) {
  const now = Date.now();
  const existing = await getSub(email);
  const base = existing && existing.expiresAt && Date.parse(existing.expiresAt) > now
    ? Date.parse(existing.expiresAt) : now;
  const sub = {
    plan, via, ...extra,
    paymentId: `${via}-${crypto.randomBytes(6).toString("hex")}`,
    orderId: null,
    activatedAt: new Date(now).toISOString(),
    expiresAt: new Date(base + days * 86_400_000).toISOString(),
  };
  await store.set(`sub:${email}`, JSON.stringify(sub));
  return sub;
}

//: Atomically claim the next founder slot; INCR is the arbiter, so two
//  simultaneous signups can never share a number. Returns the slot (1..20)
//  or null when the promo is exhausted. Callers guard with a per-user flag
//  so each account draws at most once.
async function claimFounderSlot(email) {
  const n = await store.incr("founders:claimed");   // permanent counter
  if (n > FOUNDER_CAP) return null;
  await grant(email, FOUNDER_PLAN, FOUNDER_DAYS, "founder", { founderNo: n });
  return n;
}

async function foundersLeft() {
  const n = parseInt((await store.get("founders:claimed")) || "0", 10);
  return Math.max(0, FOUNDER_CAP - (Number.isFinite(n) ? n : 0));
}

/* ------------------------------- usage --------------------------------- */
const monthKey = () => new Date().toISOString().slice(0, 7);   // UTC YYYY-MM

async function getUsed(email) {
  const n = parseInt((await store.get(`use:${email}:${monthKey()}`)) || "0", 10);
  return Number.isFinite(n) ? n : 0;
}

//: Counter TTL comfortably outlives the calendar month it belongs to.
const consumeUpload = (email) => store.incr(`use:${email}:${monthKey()}`, 35 * 86_400);

module.exports = {
  PLANS, PLAN_TTL, ORDER_TTL, FOUNDER_CAP, FOUNDER_PLAN, FOUNDER_DAYS,
  configured, mode, keyId,
  createOrder, verifyCheckoutSig, verifyWebhookSig,
  getSub, effectivePlan, activate, getUsed, consumeUpload, monthKey,
  grant, claimFounderSlot, foundersLeft,
  webhookConfigured: () => DEV || !!WEBHOOK_SECRET,
};
