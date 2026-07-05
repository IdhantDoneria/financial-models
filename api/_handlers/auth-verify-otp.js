// POST /api/auth-verify-otp  { email, code, name?, password? }
//
// Verifies the emailed code (timing-safe hash compare, max 5 attempts),
// upserts the user profile in the store, and issues a 30-day bearer session
// token. A password is required to complete first-time signup — the code
// only proves the email, so this is where an account gets one; a returning
// user who already has a password isn't forced to resend it (sending one
// again rotates it, e.g. a self-serve "forgot password" reset). The code
// is only marked used once every check (including the password) has
// passed, so a missing/short password can be corrected by resubmitting
// the same code instead of requesting a new one.

const store = require("../_lib/store");
const A = require("../_lib/auth");
const B = require("../_lib/billing");

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
    // The code is correct — but don't consume it yet. A password is
    // required to finish first-time signup, and if it's missing or too
    // short we want the same still-valid code to be retryable (just add a
    // password and press verify again) rather than forcing a whole new
    // "request code" round-trip over a form-validation mistake.
    let user;
    try { user = JSON.parse((await store.get(`user:${addr}`)) || "null"); } catch { user = null; }
    const now = new Date().toISOString();
    if (!user) user = { email: addr, name: null, createdAt: now, loginCount: 0, provider: "email-otp" };

    let pwHash = null;
    if (body.password !== undefined && String(body.password).length > 0) {
      const pw = String(body.password).slice(0, 200);
      if (pw.length < A.PW_MIN)
        return A.json(res, 400, { error: `PASSWORD MUST BE AT LEAST ${A.PW_MIN} CHARACTERS` });
      pwHash = A.hashPasswordRecord(pw);
    } else if (!user.pw) {
      return A.json(res, 400,
        { error: `A PASSWORD IS REQUIRED — SET ONE (MIN ${A.PW_MIN} CHARACTERS) TO FINISH SIGNING IN` });
    }

    await store.del(`otp:${addr}`);   // single use — committed past this point

    // upsert profile — this is where "all the user details" live server-side
    if (body.name && String(body.name).trim().length >= 2) user.name = String(body.name).trim().slice(0, 80);
    user.lastLoginAt = now;
    user.loginCount = (user.loginCount || 0) + 1;
    if (pwHash) { user.pw = pwHash; user.pwSetAt = now; }   // rotates if one was already set

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
