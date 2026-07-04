// POST /api/auth-logout  (Authorization: Bearer <token>)
//
// Revokes the server-side session (idempotent — always succeeds so the
// client can clear its local state regardless).

const store = require("../_lib/store");
const A = require("../_lib/auth");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  try {
    const token = A.bearer(req);
    if (token && store.configured()) await store.del(`sess:${token}`);
  } catch { /* revocation is best-effort */ }
  return A.json(res, 200, { ok: true });
};
