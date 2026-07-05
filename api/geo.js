// api/geo.js — resolve the caller's IP to a city/timezone so the terminal
// clock can show local time instead of only UTC, and keep a lightweight,
// privacy-conscious tally of visitor geography for conversion tracking
// (country-level counters only — no raw IP is retained beyond the hour used
// to de-duplicate repeat page loads from the same visitor).
//
//   GET /api/geo -> { ok, ip, city, country, country_code, flag, timezone,
//                     utc_offset_sec }
//
// Uses ipwho.is — free, keyless, HTTPS, no rate-limit key required.

const crypto = require("crypto");
const store = require("./_lib/store");

function clientIp(req) {
  const xf = req.headers && req.headers["x-forwarded-for"];
  if (xf) return String(xf).split(",")[0].trim();
  return (req.socket && req.socket.remoteAddress) || (req.connection && req.connection.remoteAddress) || "";
}

function isPrivate(ip) {
  return !ip || ip === "::1" || ip === "127.0.0.1" ||
    /^10\./.test(ip) || /^192\.168\./.test(ip) || /^172\.(1[6-9]|2\d|3[01])\./.test(ip);
}

module.exports = async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "private, no-store");

  const ip = clientIp(req);
  if (isPrivate(ip)) {
    return res.status(200).json({ ok: false, reason: "private-ip" });
  }

  try {
    const r = await fetch(`https://ipwho.is/${encodeURIComponent(ip)}`,
      { signal: AbortSignal.timeout(6000) });
    const j = await r.json();
    if (!j || j.success === false) return res.status(200).json({ ok: false, reason: "lookup-failed" });

    // aggregate, privacy-light analytics: count this visit once per IP per
    // hour so a single visitor refreshing the page doesn't inflate totals.
    try {
      const hashedIp = crypto.createHash("sha256").update(ip).digest("hex").slice(0, 24);
      const seenKey = `geo:seen:${hashedIp}`;
      if (!(await store.get(seenKey))) {
        await store.setex(seenKey, 3600, "1");
        const cc = j.country_code || "XX";
        await store.sadd("geo:countries", cc);   // registry so the admin desk can enumerate
        await store.incr(`geo:country:${cc}`);
        await store.incr("geo:total");
      }
    } catch { /* analytics is best-effort; never block the response on it */ }

    return res.status(200).json({
      ok: true,
      ip,
      city: j.city || null,
      country: j.country || null,
      country_code: j.country_code || null,
      flag: (j.flag && j.flag.emoji) || null,
      timezone: (j.timezone && j.timezone.id) || null,
      utc_offset_sec: (j.timezone && j.timezone.offset) || null,
    });
  } catch {
    return res.status(200).json({ ok: false, reason: "unreachable" });
  }
};
