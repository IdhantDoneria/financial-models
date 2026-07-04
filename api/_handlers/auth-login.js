// POST /api/auth-login  { email, password }
//
// Password sign-in for returning users. A password only exists after an
// OTP verify proved the email (set on the code screen), so this endpoint
// never creates accounts — it just skips the email round-trip on later
// visits. scrypt hash compare (timing-safe), 10 wrong tries per 15 min per
// address, then the same 30-day bearer session the OTP path issues.

const store = require("../_lib/store");
const A = require("../_lib/auth");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
  const addr = String(body.email || "").trim().toLowerCase();
  const pw = String(body.password || "");
  if (!A.EMAIL_RE.test(addr)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });
  if (!pw) return A.json(res, 400, { error: "ENTER YOUR PASSWORD" });

  try {
    const tries = parseInt((await store.get(`pwtry:${addr}`)) || "0", 10);
    if (tries >= A.PW_MAX_TRIES)
      return A.json(res, 429, { error: "TOO MANY WRONG PASSWORDS — WAIT 15 MIN OR USE EMAIL ME A CODE" });

    let user;
    try { user = JSON.parse((await store.get(`user:${addr}`)) || "null"); } catch { user = null; }
    if (!user || !user.pw) {
      // No password on file (or no account): the OTP path is the answer in
      // both cases, and answering identically avoids account enumeration.
      return A.json(res, 401, { error: "NO PASSWORD SET FOR THIS EMAIL — USE EMAIL ME A CODE" });
    }
    if (!A.verifyPasswordRecord(pw, user.pw)) {
      const n = await store.incr(`pwtry:${addr}`, A.PW_TRY_WINDOW);
      const left = Math.max(0, A.PW_MAX_TRIES - n);
      return A.json(res, 401, { error: left > 0
        ? `INVALID PASSWORD — ${left} TR${left === 1 ? "Y" : "IES"} LEFT (OR USE EMAIL ME A CODE)`
        : "TOO MANY WRONG PASSWORDS — WAIT 15 MIN OR USE EMAIL ME A CODE" });
    }
    await store.del(`pwtry:${addr}`);

    const now = new Date().toISOString();
    user.lastLoginAt = now;
    user.loginCount = (user.loginCount || 0) + 1;
    await store.set(`user:${addr}`, JSON.stringify(user));
    await store.sadd("users:index", addr);

    const token = A.newToken();
    await store.setex(`sess:${token}`, A.SESSION_TTL,
      JSON.stringify({ email: addr, createdAt: now }));

    return A.json(res, 200, {
      ok: true, token, expiresInSec: A.SESSION_TTL, passwordSet: true,
      founder: user.founder || null,
      user: { email: user.email, name: user.name, createdAt: user.createdAt,
              lastLoginAt: user.lastLoginAt, loginCount: user.loginCount },
    });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
