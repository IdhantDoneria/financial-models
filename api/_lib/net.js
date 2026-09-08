// api/_lib/net.js — request IP resolution + a small rate-limit helper shared
// by every endpoint that needs either.

const store = require("./store");

/** Best-effort real client IP. Vercel's edge sets x-real-ip to the actual
 *  connecting client itself (not attacker-settable the way a raw
 *  X-Forwarded-For's first hop can be on some proxy chains — a client can
 *  prepend arbitrary values to X-Forwarded-For, so trusting its first entry
 *  lets a request claim any IP it likes). Fall back to the LAST
 *  X-Forwarded-For entry (the one closest to Vercel's own edge) when
 *  x-real-ip isn't present, then to the raw socket address for local/dev. */
function clientIp(req) {
  const real = req.headers && req.headers["x-real-ip"];
  if (real) return String(real).trim();
  const xf = req.headers && req.headers["x-forwarded-for"];
  if (xf) {
    const parts = String(xf).split(",").map((s) => s.trim()).filter(Boolean);
    if (parts.length) return parts[parts.length - 1];
  }
  return (req.socket && req.socket.remoteAddress) || "unknown";
}

/** Per-key sliding-window-ish limiter built on store.incr's TTL-on-first-hit
 *  behaviour (same primitive api/_lib/auth.js already uses for OTP/password
 *  attempt caps). Returns true when the caller is within budget. Fails open
 *  (limit not enforced) if the store itself isn't configured/reachable —
 *  same "unmetered rather than broken" posture as api/usage.js when billing
 *  isn't configured; availability over strict enforcement. */
async function withinLimit(key, max, windowSec) {
  if (!store.configured()) return true;
  try {
    const n = await store.incr(key, windowSec);
    return n <= max;
  } catch {
    return true;
  }
}

module.exports = { clientIp, withinLimit };
