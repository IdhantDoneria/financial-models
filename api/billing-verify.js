// POST /api/billing-verify                                  (Bearer session)
//   { razorpay_order_id, razorpay_payment_id, razorpay_signature }
//
// Redeems a paid checkout: timing-safe HMAC-SHA256(order|payment, secret)
// per Razorpay's docs, order must exist, be unexpired, and belong to the
// calling account. On success the 30-day plan pass is written (idempotent
// with the webhook path) and the order record is consumed.

const store = require("./_lib/store");
const A = require("./_lib/auth");
const B = require("./_lib/billing");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!B.configured() || !store.configured())
    return A.json(res, 503, { error: "BILLING NOT CONFIGURED" });

  const sess = await A.getSession(req);
  if (!sess) return A.json(res, 401, { error: "SIGN IN WITH EMAIL TO UPGRADE" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
  const orderId = String(body.razorpay_order_id || "");
  const paymentId = String(body.razorpay_payment_id || "");
  const sig = String(body.razorpay_signature || "");
  if (!orderId || !paymentId || !sig) return A.json(res, 400, { error: "MISSING PAYMENT FIELDS" });

  try {
    if (!B.verifyCheckoutSig(orderId, paymentId, sig))
      return A.json(res, 401, { error: "PAYMENT SIGNATURE INVALID" });

    const raw = await store.get(`order:${orderId}`);
    if (!raw) return A.json(res, 404, { error: "ORDER UNKNOWN OR EXPIRED" });
    const order = JSON.parse(raw);
    if (order.email !== sess.email)
      return A.json(res, 403, { error: "ORDER BELONGS TO A DIFFERENT ACCOUNT" });

    const sub = await B.activate(sess.email, order.plan, paymentId, orderId, "checkout");
    await store.del(`order:${orderId}`);
    return A.json(res, 200, { ok: true, subscription: sub, plan: order.plan });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
