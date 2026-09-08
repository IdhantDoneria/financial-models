// POST /api/auth-google  { credential }
//
// Verifies a Google Identity Services ID token server-side (see
// ../_lib/google.js), then upserts the same Redis user record and bearer
// session the OTP/password paths issue — so a Google sign-in is
// indistinguishable from any other account to the admin desk, billing, and
// every other server-side check. Previously the Google callback wrote
// straight to the browser's localStorage and never told the server a user
// existed, so Google-only accounts had no server record at all (no billing,
// no admin visibility, no cross-device sign-in). No password is set here;
// verifyGoogleIdToken already proves the email belongs to the caller.

const store = require("../_lib/store");
const A = require("../_lib/auth");
const B = require("../_lib/billing");
const { verifyGoogleIdToken } = require("../_lib/google");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED" });
  const clientId = process.env.GOOGLE_CLIENT_ID;
  if (!clientId) return A.json(res, 503, { error: "GOOGLE SIGN-IN NOT CONFIGURED" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }

  let payload;
  try {
    payload = await verifyGoogleIdToken(body.credential, clientId);
  } catch (err) {
    return A.json(res, 401, { error: "GOOGLE SIGN-IN FAILED: " + String(err.message || err).slice(0, 120) });
  }

  const addr = String(payload.email).toLowerCase();
  try {
    let user;
    try { user = JSON.parse((await store.get(`user:${addr}`)) || "null"); } catch { user = null; }
    const now = new Date().toISOString();
    if (!user) user = { email: addr, name: null, createdAt: now, loginCount: 0, provider: "google" };

    if (payload.name) user.name = String(payload.name).trim().slice(0, 80);
    user.lastLoginAt = now;
    user.loginCount = (user.loginCount || 0) + 1;
    user.googleLinked = true;   // account may also have a password — both can coexist

    // founders promo — same one-shot claim the OTP/password paths run (see
    // billing.js: FOUNDER_CAP is 0 today, so this is a harmless no-op, kept
    // for parity so every sign-in path behaves identically if it's ever revived).
    if (!user.founderChecked) {
      user.founderChecked = true;
      try { user.founder = await B.claimFounderSlot(addr); }
      catch { user.founderChecked = false; }
    }

    await store.set(`user:${addr}`, JSON.stringify(user));
    await store.sadd("users:index", addr);   // admin-desk registry

    const token = A.newToken();
    await store.setex(`sess:${token}`, A.SESSION_TTL,
      JSON.stringify({ email: addr, createdAt: now }));
    A.setSessionCookie(res, token, A.SESSION_TTL);

    return A.json(res, 200, {
      ok: true, token, expiresInSec: A.SESSION_TTL,
      founder: user.founder || null,
      user: { email: user.email, name: user.name, createdAt: user.createdAt,
              lastLoginAt: user.lastLoginAt, loginCount: user.loginCount },
    });
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
