// /api/usage — plan entitlement, monthly metering and activity. (session)
//
//   GET  -> { plan, planName, used, limit, expiresAt, billing, metered, checkout }
//   POST {}                         -> one analysis: counted, logged, and
//        {source, ticker}              refused with 402 once the month's
//                                      allowance is spent (when enforced)
//   POST {event, ...detail}         -> log an activity event only (report,
//                                      export, plan_view); nothing counted
//
// Plans run without a payment gateway: the operator assigns them from the
// admin desk, and usage is counted per account per month in the store.
// `billing` / `metered` say whether the allowance is ENFORCED (the admin
// desk's metering switch); usage is counted and activity logged whenever a
// store exists, enforced or not. `checkout` says whether Razorpay is live.
//
// With no store at all the terminal is free and unmetered: GET says so and
// POST is a no-op success, so the front end never blocks.

const store = require("./_lib/store");
const A = require("./_lib/auth");
const B = require("./_lib/billing");
const activity = require("./_lib/activity");

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

//: Events the browser may report. "analysis" and "signin" are recorded by the
//  server itself (below, and in the sign-in handlers), never taken on trust.
const CLIENT_EVENTS = new Set(["report", "export", "plan_view"]);

const UNMETERED = { ok: true, billing: false, metered: false, checkout: false,
                    plan: "free", planName: "FREE", used: 0, limit: null };

module.exports = async (req, res) => {
  if (!store.configured()) return A.json(res, 200, UNMETERED);

  const enforced = await B.metering();
  const checkout = B.configured();
  const sess = await A.getSession(req);
  if (!sess) {
    if (!enforced) return A.json(res, 200, { ...UNMETERED });
    return A.json(res, 401, { error: "SIGN IN WITH EMAIL TO USE UPLOADS", billing: true });
  }

  try {
    const { plan, sub } = await B.effectivePlan(sess.email);
    const p = B.PLANS[plan];
    const used = await B.getUsed(sess.email);
    const base = { billing: enforced, metered: enforced, checkout, plan, planName: p.name,
                   limit: p.uploads, expiresAt: sub ? sub.expiresAt : null,
                   via: sub ? sub.via || "checkout" : null,       // founder/grant/checkout
                   founderNo: sub && sub.founderNo ? sub.founderNo : null,
                   month: B.monthKey() };
    //: Kept for clients built before metering was on: they read `grant`
    //  to show an operator-assigned plan while `billing` is false.
    if (!enforced && plan !== "free") {
      base.grant = { plan, planName: p.name, expiresAt: sub.expiresAt, via: sub.via || "grant" };
    }

    if (req.method === "GET") return A.json(res, 200, { ok: true, used, ...base });
    if (req.method !== "POST") return A.json(res, 405, { error: "GET or POST" });

    let body = {};
    try { body = await A.readBody(req); } catch { body = {}; }

    if (body.event) {
      if (!CLIENT_EVENTS.has(body.event)) return A.json(res, 400, { error: "unknown event" });
      await activity.track(sess.email, body.event, body);
      return A.json(res, 200, { ok: true, used, ...base });
    }

    if (enforced && p.uploads !== null && used >= p.uploads) {
      return A.json(res, 402, { error: `MONTHLY UPLOAD LIMIT REACHED (${used}/${p.uploads})` +
        (checkout ? " — UPGRADE IN MENU ▸ PLAN" : " — ASK THE OPERATOR FOR MORE IN MENU ▸ PLAN"),
        used, ...base });
    }
    const now = await B.consumeUpload(sess.email);
    await activity.track(sess.email, "analysis",
      { source: body.source === "pdf" ? "pdf" : "ticker", ticker: body.ticker });
    return A.json(res, 200, { ok: true, used: now, ...base });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};

module.exports.offlineGrant = offlineGrant;
