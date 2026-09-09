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

/** Every per-IP limiter here is keyed on clientIp(), which trusts the
 *  X-Real-Ip / X-Forwarded-For headers the request arrives with — accurate
 *  when Vercel's edge is guaranteed to set/overwrite them, but a caller can
 *  rotate a fresh value per request against anything else in front (a local
 *  dev server, a misconfigured proxy, direct access), making the per-IP cap
 *  free to defeat by simply changing the header. withinLimitLayered() adds
 *  a second counter that ignores IP entirely, so no amount of header
 *  rotation raises the *total* attempt budget past `globalMax` — only makes
 *  it look like it's coming from more places.
 *
 *  That global counter is itself a shared resource, though — a real local
 *  pentest confirmed a single caller can burn through it in well under a
 *  second (hundreds of cheap, header-rotated requests), and every OTHER
 *  caller then gets rejected for the rest of the window even though they
 *  personally did nothing wrong. `globalWindowSec` bounds how long that
 *  collateral lockout can last: defaulting it much shorter than the
 *  per-caller `windowSec` (while scaling `globalMax` down to match, so the
 *  steady-state allowed *rate* is unchanged) means a burst that exhausts
 *  the shared budget self-clears in a few seconds instead of persisting for
 *  the whole per-caller window. This doesn't fix the underlying header-
 *  trust question (that's a platform/edge-configuration concern, not
 *  something this code can verify at runtime) — it just keeps the blast
 *  radius of any single burst small regardless of why it happened. */
async function withinLimitLayered(key, max, windowSec, globalKey, globalMax, globalWindowSec) {
  const perCaller = await withinLimit(key, max, windowSec);
  const overall = await withinLimit(globalKey, globalMax, globalWindowSec || windowSec);
  return perCaller && overall;
}

module.exports = { clientIp, withinLimit, withinLimitLayered };
