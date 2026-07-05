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
//   POST {action:"reset_password", email}       -> clears the account's
//        password so the owner must return through EMAIL CODE and set a
//        new one next sign-in (see NOTE below).
//
// The GET response also includes `geo`: a country-by-country visitor tally
// from /api/geo (IP-derived, hour-deduplicated) — real geography data for
// conversion tracking, not tied to any one user account.
//
// NOTE — no endpoint here ever returns a password, hashed or otherwise.
// Passwords are stored as scrypt(salt, 64) (api/_lib/auth.js) and the
// plaintext is never retained anywhere, by design — there is nothing to
// display. What IS shown per-account: whether a password is set
// (`passwordSet`) and when it was last set (`pwSetAt`). Use `reset_password`
// if you need to force re-verification (e.g. suspected compromise) —
// this revokes the current password rather than exposing it.

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
      pwSetAt: u ? u.pwSetAt || null : null,
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

async function geoBreakdown() {
  const codes = await store.smembers("geo:countries");
  const [counts, totalRaw] = await Promise.all([
    store.mget(codes.map((c) => `geo:country:${c}`)),
    store.get("geo:total"),
  ]);
  const countries = codes.map((code, i) => ({ code, count: parseInt(counts[i] || "0", 10) || 0 }))
    .sort((a, b) => b.count - a.count);
  return { total: parseInt(totalRaw || "0", 10) || 0, countries };
}

module.exports = async (req, res) => {
  if (!KEY || !store.configured())
    return A.json(res, 503, { error: "ADMIN DESK NOT CONFIGURED — set ADMIN_KEY (and a store) in Vercel env vars" });
  if (!authorized(req)) return A.json(res, 401, { error: "INVALID ADMIN KEY" });

  try {
    if (req.method === "GET")
      return A.json(res, 200, { ok: true, ...(await listUsers()), geo: await geoBreakdown() });
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
    if (body.action === "reset_password") {
      let user;
      try { user = JSON.parse((await store.get(`user:${email}`)) || "null"); } catch { user = null; }
      if (!user) return A.json(res, 404, { error: "NO ACCOUNT FOR THIS EMAIL" });
      delete user.pw;
      delete user.pwSetAt;
      await store.set(`user:${email}`, JSON.stringify(user));
      return A.json(res, 200, { ok: true, resetPassword: email });
    }
    return A.json(res, 400, { error: "action MUST BE grant, revoke OR reset_password" });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
