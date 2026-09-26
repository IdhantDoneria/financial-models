// POST /api/auth-login  { email, password }
//
// Password sign-in for returning users. A password only exists after an
// OTP verify proved the email (set on the code screen), so this endpoint
// never creates accounts — it just skips the email round-trip on later
// visits. scrypt hash compare (timing-safe), 10 wrong tries per 15 min per
// address, then the same 30-day bearer session the OTP path issues.

const store = require("../_lib/store");
const A = require("../_lib/auth");
const activity = require("../_lib/activity");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED" });

  let body;
  try { body = await A.readBody(req); } catch (err) {
    if (err instanceof A.BodyTooLargeError) return A.json(res, 413, { error: "REQUEST BODY TOO LARGE" });
    return A.json(res, 400, { error: "invalid JSON" });
  }
  const addr = String(body.email || "").trim().toLowerCase();
  const pw = String(body.password || "");
  if (!A.EMAIL_RE.test(addr)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });
  if (!pw) return A.json(res, 400, { error: "ENTER YOUR PASSWORD" });

  try {
    // Atomic claim-then-check — a single store.incr hands out a strictly
    // increasing ticket per request (the same primitive auth-verify-otp.js
    // uses for its OTP attempt cap), so a fan of concurrent requests can
    // never get more real password comparisons through than PW_MAX_TRIES
    // allows. The previous sequence here read the counter, decided, and
    // only incremented it later (inside the wrong-password branch) — race-
    // free only by accident of this dev server's synchronous in-memory
    // store; against real Redis (network round-trips, concurrent
    // serverless invocations) concurrent guesses could each read a stale
    // low count before any of their increments landed, letting more than
    // PW_MAX_TRIES real scrypt comparisons through. Incrementing
    // unconditionally — including for an email with no account or no
    // password set — also means a fixed nonexistent address can no longer
    // be probed without limit.
    const tries = await store.incr(`pwtry:${addr}`, A.PW_TRY_WINDOW);
    if (tries > A.PW_MAX_TRIES)
      return A.json(res, 429, { error: "TOO MANY WRONG PASSWORDS — WAIT 15 MIN OR USE EMAIL ME A CODE" });

    let user;
    try { user = JSON.parse((await store.get(`user:${addr}`)) || "null"); } catch { user = null; }

    // Always run a real scrypt comparison — even when there's no account or
    // no password on file — against a fixed decoy record in that case.
    // scrypt is deliberately slow, so short-circuiting it whenever no real
    // password record exists let a caller tell "this account has a
    // password" apart from "it doesn't" purely from response latency, even
    // once the JSON body below was made identical between the two cases.
    // The single error string below (instead of the old "NO PASSWORD SET"
    // vs "INVALID PASSWORD" split) closes the same oracle at the message
    // level: a user who never set a password now just sees the generic
    // "invalid" response — same as anyone who mistyped one — and is still
    // pointed at "email me a code" as the way in.
    const hasPw = !!(user && user.pw);
    const record = hasPw ? user.pw : A.DUMMY_PW_RECORD;
    const passwordOk = A.verifyPasswordRecord(pw, record) && hasPw;

    if (!passwordOk) {
      const left = Math.max(0, A.PW_MAX_TRIES - tries);
      return A.json(res, 401, { error: left > 0
        ? `INVALID EMAIL OR PASSWORD — ${left} TR${left === 1 ? "Y" : "IES"} LEFT (OR USE EMAIL ME A CODE)`
        : "TOO MANY WRONG PASSWORDS — WAIT 15 MIN OR USE EMAIL ME A CODE" });
    }
    await store.del(`pwtry:${addr}`);

    const now = new Date().toISOString();
    user.lastLoginAt = now;
    user.loginCount = (user.loginCount || 0) + 1;
    await store.set(`user:${addr}`, JSON.stringify(user));
    await activity.track(addr, "signin", { method: "password" });
    await store.sadd("users:index", addr);

    const token = A.newToken();
    await store.setex(`sess:${token}`, A.SESSION_TTL,
      JSON.stringify({ email: addr, createdAt: now }));
    A.setSessionCookie(res, token, A.SESSION_TTL);

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
