// /api/admin — the operator's desk. Auth: X-Admin-Key header, compared
// timing-safe against the ADMIN_KEY env var (dev-memory harness accepts
// "devadmin"). 503 until ADMIN_KEY and a store exist.
//
//   GET  ?action=users            -> every account: email, name, sign-in
//                                    history, founder slot, plan, expiry,
//                                    uploads this month (+ totals row)
//   POST {action:"grant", email, plan, days}   -> free premium for anyone —
//        type an email + duration; works even before that person signs up
//        (the pass is waiting when they first verify their email).
//   POST {action:"revoke", email}              -> end a plan immediately.

const store = require("./_lib/store");
const A = require("./_lib/auth");
const B = require("./_lib/billing");

const DEV = process.env.AUTH_DEV_MEMORY === "1";
const KEY = process.env.ADMIN_KEY || (DEV ? "devadmin" : "");

function authorized(req) {
  const k = req.headers && (req.headers["x-admin-key"] || req.headers["X-Admin-Key"]);
  return !!k && A.timingSafeEq(String(k), KEY);
}

async function listUsers() {
  const emails = await store.smembers("users:index");
  const month = B.monthKey();
  const [users, subs, uses] = await Promise.all([
    store.mget(emails.map((e) => `user:${e}`)),
    store.mget(emails.map((e) => `sub:${e}`)),
    store.mget(emails.map((e) => `use:${e}:${month}`)),
  ]);
  const now = Date.now();
  const rows = emails.map((email, i) => {
    let u = null, s = null;
    try { u = JSON.parse(users[i] || "null"); } catch { /* corrupt row */ }
    try { s = JSON.parse(subs[i] || "null"); } catch { /* corrupt row */ }
    const active = s && s.expiresAt && Date.parse(s.expiresAt) > now;
    const plan = active ? s.plan : "free";
    return {
      email,
      name: u ? u.name : null,
      createdAt: u ? u.createdAt : null,
      lastLoginAt: u ? u.lastLoginAt : null,
      loginCount: u ? u.loginCount || 0 : 0,
      founder: u ? u.founder || null : null,
      passwordSet: !!(u && u.pw),
      signedUp: !!u,                              // false: granted, not yet signed up
      plan, planName: (B.PLANS[plan] || B.PLANS.free).name,
      via: active ? s.via || "checkout" : null,
      expiresAt: active ? s.expiresAt : null,
      usedThisMonth: parseInt(uses[i] || "0", 10) || 0,
    };
  }).sort((a, b) => (b.createdAt || "9") > (a.createdAt || "9") ? -1 : 1);
  return {
    month, rows,
    totals: {
      users: rows.filter((r) => r.signedUp).length,
      activePaid: rows.filter((r) => r.via === "checkout").length,
      granted: rows.filter((r) => r.via === "grant" || r.via === "founder").length,
      foundersClaimed: B.FOUNDER_CAP - (await B.foundersLeft()),
      foundersLeft: await B.foundersLeft(),
    },
  };
}

module.exports = async (req, res) => {
  if (!KEY || !store.configured())
    return A.json(res, 503, { error: "ADMIN DESK NOT CONFIGURED — set ADMIN_KEY (and a store) in Vercel env vars" });
  if (!authorized(req)) return A.json(res, 401, { error: "INVALID ADMIN KEY" });

  try {
    if (req.method === "GET") return A.json(res, 200, { ok: true, ...(await listUsers()) });
    if (req.method !== "POST") return A.json(res, 405, { error: "GET or POST" });

    let body;
    try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
    const email = String(body.email || "").trim().toLowerCase();
    if (!A.EMAIL_RE.test(email)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });

    if (body.action === "grant") {
      const plan = String(body.plan || "unlimited");
      if (!B.PLANS[plan] || !B.PLANS[plan].amount)
        return A.json(res, 400, { error: "PLAN MUST BE pro OR unlimited" });
      const days = Math.round(Number(body.days));
      if (!Number.isFinite(days) || days < 1 || days > 365)
        return A.json(res, 400, { error: "DAYS MUST BE 1–365" });
      const sub = await B.grant(email, plan, days, "grant");
      await store.sadd("users:index", email);   // visible even pre-signup
      return A.json(res, 200, { ok: true, granted: plan, email, days,
                                expiresAt: sub.expiresAt });
    }
    if (body.action === "revoke") {
      await store.del(`sub:${email}`);
      return A.json(res, 200, { ok: true, revoked: email });
    }
    return A.json(res, 400, { error: "action MUST BE grant OR revoke" });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
