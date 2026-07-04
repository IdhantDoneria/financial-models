// POST /api/auth-verify-otp  { email, code, name? }
//
// Verifies the emailed code (timing-safe hash compare, max 5 attempts, code
// consumed on success), upserts the user profile in the store, and issues a
// 30-day bearer session token. First successful verify for an address IS
// the signup — passwordless accounts.

const store = require("./_lib/store");
const A = require("./_lib/auth");
const B = require("./_lib/billing");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
  const addr = String(body.email || "").trim().toLowerCase();
  const code = String(body.code || "").replace(/\D/g, "");
  if (!A.EMAIL_RE.test(addr)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });
  if (code.length !== 6) return A.json(res, 400, { error: "ENTER THE 6-DIGIT CODE" });

  try {
    const raw = await store.get(`otp:${addr}`);
    if (!raw) return A.json(res, 400, { error: "CODE EXPIRED OR NOT REQUESTED — SEND A NEW ONE" });
    const rec = JSON.parse(raw);

    if (rec.tries >= A.OTP_MAX_TRIES) {
      await store.del(`otp:${addr}`);
      return A.json(res, 429, { error: "TOO MANY WRONG ATTEMPTS — REQUEST A NEW CODE" });
    }
    if (!A.timingSafeEq(A.hashOtp(addr, code), rec.h)) {
      rec.tries += 1;
      const left = A.OTP_MAX_TRIES - rec.tries;
      if (left <= 0) await store.del(`otp:${addr}`);
      else await store.setex(`otp:${addr}`, 300, JSON.stringify(rec));
      return A.json(res, 401, { error: left > 0
        ? `INVALID CODE — ${left} ATTEMPT${left === 1 ? "" : "S"} LEFT`
        : "TOO MANY WRONG ATTEMPTS — REQUEST A NEW CODE" });
    }
    await store.del(`otp:${addr}`);   // single use

    // upsert profile — this is where "all the user details" live server-side
    let user;
    try { user = JSON.parse((await store.get(`user:${addr}`)) || "null"); } catch { user = null; }
    const now = new Date().toISOString();
    if (!user) user = { email: addr, name: null, createdAt: now, loginCount: 0, provider: "email-otp" };
    if (body.name && String(body.name).trim().length >= 2) user.name = String(body.name).trim().slice(0, 80);
    user.lastLoginAt = now;
    user.loginCount = (user.loginCount || 0) + 1;

    // optional password (the code just proved the email): later sign-ins can
    // use email+password directly. Sending one again rotates it.
    if (body.password !== undefined && String(body.password).length > 0) {
      const pw = String(body.password).slice(0, 200);
      if (pw.length < A.PW_MIN)
        return A.json(res, 400, { error: `PASSWORD MUST BE AT LEAST ${A.PW_MIN} CHARACTERS` });
      user.pw = A.hashPasswordRecord(pw);
    }

    // founders promo — each account draws exactly once; the first 20 win a
    // free month of DESK UNLIMITED (pre-promo accounts claim on next login).
    if (!user.founderChecked) {
      user.founderChecked = true;
      try { user.founder = await B.claimFounderSlot(addr); }
      catch { user.founderChecked = false; }   // store hiccup: retry next login
    }

    await store.set(`user:${addr}`, JSON.stringify(user));
    await store.sadd("users:index", addr);     // admin-desk registry

    const token = A.newToken();
    await store.setex(`sess:${token}`, A.SESSION_TTL,
      JSON.stringify({ email: addr, createdAt: now }));

    return A.json(res, 200, {
      ok: true, token, expiresInSec: A.SESSION_TTL,
      passwordSet: !!user.pw,
      founder: user.founder || null,           // 1..20 when a slot was won
      user: { email: user.email, name: user.name, createdAt: user.createdAt,
              lastLoginAt: user.lastLoginAt, loginCount: user.loginCount },
    });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
