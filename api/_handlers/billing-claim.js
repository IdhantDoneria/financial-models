// INTERIM manual-UPI flow for early users — a stopgap so we can take
// payments now WITHOUT the Razorpay integration. This is NOT a replacement
// for Razorpay: the Razorpay code path stays fully intact and is re-enabled
// by setting PAYMENTS_MODE=razorpay (+ the Razorpay keys). Do not delete the
// Razorpay path.
//
// POST /api/billing-claim  { plan, period, upiRef, note? }   (session required)
//
// Files a PENDING manual-UPI payment claim — a buyer pays the published VPA
// out-of-band, then reports the UTR/reference here. This endpoint NEVER
// grants a plan itself; it only writes a `upiclaim:<id>` record an operator
// later reviews at the admin desk (api/admin.js: claims / approve_claim /
// reject_claim), and only THAT approval step calls B.grant(). A forged or
// spammed claim is therefore harmless — at worst it clutters the operator's
// pending queue; nothing about calling this endpoint unlocks anything.

const crypto = require("crypto");
const store = require("../_lib/store");
const A = require("../_lib/auth");
const B = require("../_lib/billing");
const { clientIp, withinLimitLayered } = require("../_lib/net");

//: UTR/RRN references are ~12 alphanumeric characters in practice; allow a
//  small surrounding range rather than hardcoding exactly 12.
const UPI_REF_RE = /^[A-Za-z0-9]{6,25}$/;
const NOTE_MAX = 140;

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "BILLING NOT CONFIGURED" });

  const sess = await A.getSession(req);
  if (!sess) return A.json(res, 401, { error: "SIGN IN FIRST" });
  const email = sess.email;

  // Per-IP budget plus a global backstop, same layered idiom api/quotes.js
  // uses — clientIp() is only as trustworthy as whatever sits in front of
  // this function (see _lib/net.js), so the global counter bounds the total
  // damage a header-rotated burst can do regardless of per-IP throttling.
  if (!(await withinLimitLayered(`claim:rl:${clientIp(req)}`, 5, 3600, "claim:rl:global", 100, 3600)))
    return A.json(res, 429, { error: "TOO MANY REQUESTS — TRY AGAIN LATER" });

  let body;
  try { body = await A.readBody(req); } catch (err) {
    if (err instanceof A.BodyTooLargeError) return A.json(res, 413, { error: "REQUEST BODY TOO LARGE" });
    return A.json(res, 400, { error: "invalid JSON" });
  }

  const plan = String(body.plan || "");
  if (!B.PURCHASABLE_PLANS.includes(plan)) return A.json(res, 400, { error: "INVALID PLAN" });
  const period = String(body.period || "");
  if (!B.PERIODS.includes(period)) return A.json(res, 400, { error: "INVALID PERIOD" });
  const term = B.PLANS[plan].periods[period];
  if (!term) return A.json(res, 400, { error: `${plan.toUpperCase()} HAS NO ${period.toUpperCase()} OPTION` });

  const upiRef = String(body.upiRef || "").trim().toUpperCase();
  if (!UPI_REF_RE.test(upiRef)) return A.json(res, 400, { error: "ENTER A VALID UPI REFERENCE (UTR)" });

  const note = String(body.note || "").slice(0, NOTE_MAX);

  try {
    // One open claim per email — tapping "I've paid" again while an earlier
    // claim is still pending should not stack a duplicate operator ticket.
    const pendingKey = `upiclaim:pending:${email}`;
    const existingId = await store.get(pendingKey);
    if (existingId) {
      let existing = null;
      try { existing = JSON.parse((await store.get(`upiclaim:${existingId}`)) || "null"); } catch { existing = null; }
      if (existing && existing.status === "pending")
        return A.json(res, 200, { ok: true, status: "pending", deduped: true });
      // Stale pointer (the claim was resolved, or the record is corrupt) —
      // clear it and fall through to file a fresh claim.
      await store.del(pendingKey);
    }

    const id = crypto.randomBytes(9).toString("hex");
    const claim = {
      id, email, plan, period, upiRef, amountInr: term.amount / 100, note,
      status: "pending", createdAt: new Date().toISOString(),
    };
    await store.set(`upiclaim:${id}`, JSON.stringify(claim));
    await store.sadd("upiclaims:index", id);
    await store.set(pendingKey, id);

    return A.json(res, 200, { ok: true, status: "pending" });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
