// POST /api/billing-order  { plan: "pro" | "unlimited" }   (Bearer session)
//
// Creates a Razorpay order server-side — the amount comes from the plan
// catalogue, never from the client — and pins the order to the buyer's
// account so only that account's verify can redeem it.

const store = require("../_lib/store");
const A = require("../_lib/auth");
const B = require("../_lib/billing");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!B.configured() || !store.configured())
    return A.json(res, 503, { error: "BILLING NOT CONFIGURED — see README: Razorpay setup" });

  const sess = await A.getSession(req);
  if (!sess) return A.json(res, 401, { error: "SIGN IN WITH EMAIL TO UPGRADE" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
  const plan = String(body.plan || "");
  const p = B.PLANS[plan];
  if (!p || !p.amount) return A.json(res, 400, { error: "UNKNOWN PLAN" });

  try {
    const order = await B.createOrder(plan, sess.email);
    await store.setex(`order:${order.id}`, B.ORDER_TTL,
      JSON.stringify({ email: sess.email, plan, amount: p.amount }));
    return A.json(res, 200, {
      ok: true, orderId: order.id, amount: p.amount, currency: "INR",
      keyId: B.keyId(), plan, planName: p.name,
    });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
