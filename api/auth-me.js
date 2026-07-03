// GET /api/auth-me  (Authorization: Bearer <token>)
//
// Validates the session and returns the stored profile — the terminal calls
// this after boot to confirm a server session is still live and to refresh
// the display name.

const store = require("./_lib/store");
const A = require("./_lib/auth");

module.exports = async (req, res) => {
  if (req.method !== "GET") return A.json(res, 405, { error: "GET only" });
  if (!store.configured()) return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED" });
  try {
    const sess = await A.getSession(req);
    if (!sess) return A.json(res, 401, { error: "INVALID OR EXPIRED SESSION" });
    const user = JSON.parse((await store.get(`user:${sess.email}`)) || "null");
    if (!user) return A.json(res, 401, { error: "ACCOUNT NOT FOUND" });
    return A.json(res, 200, { ok: true, user: {
      email: user.email, name: user.name, createdAt: user.createdAt,
      lastLoginAt: user.lastLoginAt, loginCount: user.loginCount,
    }});
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
