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
// Paid plans are bought as one-time orders for a MONTHLY or ANNUAL pass (no
// dashboard plan objects needed): FREE 10 analyses/mo · ANALYST PRO 50
// uploads/mo @ $29/mo or $299/yr · DESK UNLIMITED (unlimited uploads)
// @ $59/mo or $599/yr · BOUTIQUE FUND (unlimited uploads, up to 5 seats)
// @ $249/mo or $2,499/yr. "Upload" = one IB-desk company analysis,
// whether loaded from a ticker (api/fundamentals.js) or an uploaded PDF. Analyst Pro
// and above also unlock the Ind AS 116 hidden-debt normalizer and reverse
// DCF solver — unlike every other model's math (client-side WASM), these
// two are actually computed server-side, in api/premium.py, which
// re-derives plan entitlement from this same store rather than trusting
// the client. terminal.js's PREMIUM_MODELS/premiumModelGate() are a fast
// UX pre-check only, not the enforcement boundary.
//
// Pricing is one global USD figure per plan, converted to INR at a single
// fixed rate (USD_TO_INR below) — deliberately NOT geo-discounted, so a
// buyer in Mumbai and a buyer in Manhattan pay the same real price. INR is
// the only currency actually charged (Razorpay settles in INR only; see
// createOrder), so it is the authoritative `amount`; USD is derived from it
// for display (terminal.js picks the label by IP-derived country, see
// state.billing.geo) at the exact same rate, not a discounted one.
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

const ORDER_TTL = 3600;            // pending order records live 1 hour
const PERIODS = ["monthly", "annual"];

//: Reference rate for deriving the INR (authoritative, charged) amount from
//  each plan's canonical USD price. Fixed rather than live-fetched — a price
//  that moved mid-checkout would be worse than one that's a few percent
//  stale — so nudge this occasionally to track the real rate, not on every
//  deploy. usd * 8800 is always an integer number of paise for a whole-
//  dollar price, so no rounding is needed.
const USD_TO_INR = 88;
const usdToPaise = (usd) => usd * USD_TO_INR * 100;

//: Authoritative catalogue. `id`/`name`/`uploads`/`blurb` are billing-period
//  independent (a monthly and an annual Pro subscriber get the same 50/mo
//  cap and the same premium-model access). Only price and pass length vary
//  by period, under `periods.monthly`/`periods.annual` — `amount` is paise
//  (Razorpay's unit, what is actually charged), `usd` is the same price in
//  dollars (display only, terminal.js picks which one to show), `days` is
//  how long one purchase of that period grants. `uploads` null = unlimited.
const PLANS = {
  //: 10, not 3. The metered unit used to require possessing a 10-K PDF, which
  //  was friction enough that 3 lasted a while. Ticker load removed that
  //  friction entirely — an evaluator can now spend 3 analyses in under a
  //  minute and meet the paywall before they have seen what the product
  //  actually does, which converts worse, not better. 10 buys enough room to
  //  check a few real holdings; it is still far short of habitual use.
  free: { id: "free", name: "FREE", uploads: 10,
          blurb: "10 company analyses / month · ticker or PDF · six-model valuation report · " +
                 "all 10 calculators · every assumption sourced · SCEN engine" },
  pro: { id: "pro", name: "ANALYST PRO", uploads: 50,
         periods: { monthly: { amount: usdToPaise(29), usd: 29, days: 30 },
                    annual: { amount: usdToPaise(299), usd: 299, days: 365 } },
         //: Blurbs lead with volume and provenance, not with the two forensic
         //  models. Those are the sharpest differentiator but the narrowest
         //  audience; the pain most buyers actually have is "turn a filing
         //  into a model I can defend, without spending an afternoon on it".
         blurb: "50 company analyses / month · every figure traced to its source · " +
                "also unlocks the Ind AS 116 hidden-debt normalizer and reverse-DCF solver" },
  unlimited: { id: "unlimited", name: "DESK UNLIMITED", uploads: null,
               periods: { monthly: { amount: usdToPaise(59), usd: 59, days: 30 },
                          annual: { amount: usdToPaise(599), usd: 599, days: 365 } },
               blurb: "Unlimited company analyses · everything in ANALYST PRO" },
  //: Self-serve tier for small funds/RIAs — sits between DESK UNLIMITED
  //  (single desk) and ENTERPRISE (bespoke, sales-led). `seats` here is
  //  declarative, same as ENTERPRISE's: this store is per-email, so
  //  provisioning the named team members is a manual step by the operator
  //  today, not an automated multi-seat login system.
  boutique: { id: "boutique", name: "BOUTIQUE FUND", uploads: null, seats: 5,
              periods: { monthly: { amount: usdToPaise(249), usd: 249, days: 30 },
                         annual: { amount: usdToPaise(2499), usd: 2499, days: 365 } },
              blurb: "Unlimited uploads · everything in DESK UNLIMITED · priority support · " +
                     "provisioning for up to 5 named team members" },
  //: Sales-led tier — bespoke pricing, so there's no self-serve `periods`
  //  entry and `contact:true`. The order handler rejects any plan without a
  //  `periods` catalogue, so ENTERPRISE can never be self-served through
  //  Razorpay; it is provisioned by the operator (admin grant) after a
  //  commercial agreement.
  enterprise: { id: "enterprise", name: "ENTERPRISE", uploads: null,
                contact: true, seats: 20,
                blurb: "Unrestricted access to the entire platform with unlimited analyses. " +
                       "Guaranteed priority compute during periods of peak market traffic, " +
                       "provisioning for up to 20 named team members, and early access to newly " +
                       "released capabilities ahead of general availability — accompanied by " +
                       "dedicated onboarding and priority support." },
};

//: Plans a customer can self-serve buy through Razorpay — the only ones
//  with a `periods` catalogue. FREE has no purchase path; ENTERPRISE is
//  operator-provisioned only (see above).
const PURCHASABLE_PLANS = Object.keys(PLANS).filter((id) => PLANS[id].periods);

const configured = () => DEV || !!(KEY_ID && KEY_SECRET);

//: Operator switch for the two Pro models while checkout is offline. Open by
//  default (nobody can buy Pro yet, so gating it would only turn people away);
//  the admin desk writes "0" here to lock it back to granted accounts. It has
//  no effect once billing is live: then the plan decides, as before.
//  api/premium.py reads the same key, directly, on every request.
const PRO_OPEN_KEY = "flag:pro_open";
const proOpen = async () => {
  if (configured()) return false;
  try { return (await store.get(PRO_OPEN_KEY)) !== "0"; } catch { return true; }
};
const setProOpen = (open) => store.set(PRO_OPEN_KEY, open ? "1" : "0");
const mode = () => (DEV ? "dev-fake" : KEY_ID && KEY_SECRET ? "razorpay" : "unconfigured");
const keyId = () => (DEV ? "rzp_test_devfake" : KEY_ID);
const secret = () => (DEV ? DEV_SECRET : KEY_SECRET);

/* ----------------------------- gateway --------------------------------- */
//: `period` is "monthly" or "annual" — selects which entry of the plan's
//  `periods` catalogue sets the order amount and, later, the pass length.
async function createOrder(plan, period, email) {
  const p = PLANS[plan];
  const term = p.periods[period];
  if (DEV) {
    return { id: "order_dev" + crypto.randomBytes(8).toString("hex"),
             amount: term.amount, currency: "INR" };
  }
  const r = await fetch("https://api.razorpay.com/v1/orders", {
    method: "POST",
    headers: {
      Authorization: "Basic " + Buffer.from(`${KEY_ID}:${KEY_SECRET}`).toString("base64"),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      amount: term.amount, currency: "INR",
      receipt: `fm-${plan}-${period}-${Date.now()}`.slice(0, 40),
      // `period` travels in the order notes too — the webhook's fallback
      // path (order record expired before delivery) reads plan/period from
      // here, so it must carry everything activate() needs.
      notes: { plan, period, email, product: "finmodels-terminal" },
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
//  `period` ("monthly"/"annual") picks the pass length from the plan's
//  `periods` catalogue — a monthly purchase grants 30 days, annual 365.
async function activate(email, plan, period, paymentId, orderId, via) {
  const existing = await getSub(email);
  if (existing && existing.paymentId === paymentId) return existing;
  const term = PLANS[plan].periods[period];
  const now = Date.now();
  //: Renewing/upgrading before expiry credits the unused days.
  const carry = existing && PLANS[existing.plan] && Date.parse(existing.expiresAt) > now
    ? Date.parse(existing.expiresAt) - now : 0;
  const sub = {
    plan, period, paymentId, orderId, via,
    activatedAt: new Date(now).toISOString(),
    expiresAt: new Date(now + term.days * 86_400_000 + carry).toISOString(),
  };
  await store.set(`sub:${email}`, JSON.stringify(sub));
  return sub;
}

/* ------------------------- grants & founders ---------------------------- *
 * Non-payment activations: the first-20 founders promo (retired — see below)
 * and operator grants (the admin desk types an email + duration -> free
 * premium; this is now how complimentary-access requests emailed in are
 * fulfilled). Both write the same sub:<email> record the paywall reads,
 * tagged with `via` so the UI can say where the access came from.
 *
 * FOUNDER_CAP is 0: the automatic first-20-signups promo is disabled.
 * Free access is now granted manually, case-by-case, after a request to
 * the concierge inbox (see login page) — via admin.js `grant`. Existing
 * founder grants already issued are unaffected. */
const FOUNDER_CAP = 0;
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
  PLANS, PERIODS, PURCHASABLE_PLANS, ORDER_TTL, FOUNDER_CAP, FOUNDER_PLAN, FOUNDER_DAYS, USD_TO_INR,
  configured, mode, keyId, proOpen, setProOpen, PRO_OPEN_KEY,
  createOrder, verifyCheckoutSig, verifyWebhookSig,
  getSub, effectivePlan, activate, getUsed, consumeUpload, monthKey,
  grant, claimFounderSlot, foundersLeft,
  webhookConfigured: () => DEV || !!WEBHOOK_SECRET,
};
