// /api/usage — plan entitlement + monthly upload metering. (Bearer session)
//
//   GET  -> { plan, planName, used, limit, expiresAt, billing }
//   POST -> consume one upload (called when the IB desk analyzes a PDF);
//           402 with the same shape when the month's allowance is spent.
//
// When billing isn't configured the terminal is free and unmetered — GET
// says so and POST is a no-op success, so the front end never blocks.

const store = require("./_lib/store");
const A = require("./_lib/auth");
const B = require("./_lib/billing");

//: The signed-in account's admin-granted plan, or null (no session, no store,
//  no active grant). Never throws: this only decorates an unmetered reply.
async function offlineGrant(req) {
  try {
    if (!store.configured()) return null;
    const sess = await A.getSession(req);
    if (!sess) return null;
    const { plan, sub } = await B.effectivePlan(sess.email);
    if (plan === "free") return null;
    return { plan, planName: B.PLANS[plan].name, expiresAt: sub.expiresAt, via: sub.via || "grant" };
  } catch { return null; }
}

module.exports = async (req, res) => {
  const billing = B.configured() && store.configured();
  if (!billing) {
    // Checkout is offline, so nothing is metered and `plan` stays "free". An
    // account the operator has granted a plan still gets to see it: `grant`
    // carries the real plan, so the plan chip and tab stop showing FREE for it.
    const out = { ok: true, billing: false, metered: false,
                  plan: "free", planName: "FREE", used: 0, limit: null };
    const grant = await offlineGrant(req);
    if (grant) out.grant = grant;
    return A.json(res, 200, out);
  }

  const sess = await A.getSession(req);
  if (!sess) return A.json(res, 401, { error: "SIGN IN WITH EMAIL TO USE UPLOADS", billing });

  try {
    const { plan, sub } = await B.effectivePlan(sess.email);
    const p = B.PLANS[plan];
    const used = await B.getUsed(sess.email);
    const base = { billing: true, metered: true, plan, planName: p.name,
                   limit: p.uploads, expiresAt: sub ? sub.expiresAt : null,
                   via: sub ? sub.via || "checkout" : null,       // founder/grant/checkout
                   founderNo: sub && sub.founderNo ? sub.founderNo : null,
                   month: B.monthKey() };

    if (req.method === "GET") return A.json(res, 200, { ok: true, used, ...base });
    if (req.method !== "POST") return A.json(res, 405, { error: "GET or POST" });

    if (p.uploads !== null && used >= p.uploads) {
      return A.json(res, 402, { error: `MONTHLY UPLOAD LIMIT REACHED (${used}/${p.uploads})` +
        ` — UPGRADE IN MENU ▸ PLAN`, used, ...base });
    }
    const now = await B.consumeUpload(sess.email);
    return A.json(res, 200, { ok: true, used: now, ...base });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};

module.exports.offlineGrant = offlineGrant;
