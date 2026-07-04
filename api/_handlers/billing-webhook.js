// POST /api/billing-webhook — Razorpay webhook receiver (payment.captured).
//
// Belt-and-braces activation: if the buyer's tab dies between paying and
// the client-side verify, Razorpay still delivers the capture event here.
// Signature = HMAC-SHA256(raw body, RAZORPAY_WEBHOOK_SECRET) in the
// X-Razorpay-Signature header; the order note's email/plan (written by us
// at order creation) say whose pass to activate. Activation is idempotent
// with the checkout-verify path.

const store = require("../_lib/store");
const B = require("../_lib/billing");

// Signature is over raw bytes — the parent /api/billing function keeps
// Vercel's JSON body helper off for the whole dispatcher.
module.exports = async (req, res) => {
  const json = (code, obj) => {
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.status(code).json(obj);
  };
  if (req.method !== "POST") return json(405, { error: "POST only" });
  if (!B.webhookConfigured() || !store.configured())
    return json(503, { error: "WEBHOOK NOT CONFIGURED" });

  let raw;
  if (req.body !== undefined && req.body !== null) {
    raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body);
  } else {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    raw = Buffer.concat(chunks).toString("utf8");
  }

  const sig = req.headers["x-razorpay-signature"] || "";
  if (!sig || !B.verifyWebhookSig(raw, sig)) return json(401, { error: "BAD SIGNATURE" });

  let event;
  try { event = JSON.parse(raw); } catch { return json(400, { error: "invalid JSON" }); }
  if (event.event !== "payment.captured") return json(200, { ok: true, ignored: event.event });

  try {
    const pay = event.payload.payment.entity;
    const orderId = pay.order_id;
    // Prefer our own pending-order record; fall back to the order notes we
    // attached at creation (the record may have expired before delivery).
    let email = null, plan = null;
    const rec = await store.get(`order:${orderId}`);
    if (rec) ({ email, plan } = JSON.parse(rec));
    if ((!email || !plan) && pay.notes) ({ email, plan } = pay.notes);
    if (!email || !B.PLANS[plan] || !B.PLANS[plan].amount)
      return json(200, { ok: true, ignored: "no matching order/plan" });

    const sub = await B.activate(email, plan, pay.id, orderId, "webhook");
    if (rec) await store.del(`order:${orderId}`);
    return json(200, { ok: true, activated: plan, expiresAt: sub.expiresAt });
  } catch (err) {
    return json(502, { error: String(err.message || err).slice(0, 180) });
  }
};
