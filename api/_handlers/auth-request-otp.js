// POST /api/auth-request-otp  { email }
//
// Generates a 6-digit one-time code, stores only its salted hash (10-min
// TTL, 5 attempts), and emails the code. Guard rails: 60 s resend cooldown
// and 5 sends/hour per address. Returns 503 with an explicit reason until
// the deployment has a store (Upstash Redis) and a mailer (Gmail via
// Nodemailer, or Resend as a fallback).

const store = require("../_lib/store");
const email = require("../_lib/email");
const A = require("../_lib/auth");
const { clientIp, withinLimitLayered } = require("../_lib/net");

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED — no database attached (see README: Upstash Redis)" });
  if (!email.configured())
    return A.json(res, 503, { error: "EMAIL NOT CONFIGURED — set GMAIL_USER + GMAIL_APP_PASSWORD (see README)" });

  let body;
  try { body = await A.readBody(req); } catch (err) {
    if (err instanceof A.BodyTooLargeError) return A.json(res, 413, { error: "REQUEST BODY TOO LARGE" });
    return A.json(res, 400, { error: "invalid JSON" });
  }
  const addr = String(body.email || "").trim().toLowerCase();
  if (!A.EMAIL_RE.test(addr)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });

  try {
    // Per-IP + global cap, checked BEFORE the per-email checks just below.
    // Those (60s cooldown + 5/hour) only bound how often ONE address can be
    // sent a code — they do nothing to stop a single caller spraying codes
    // at many DISTINCT addresses, since each fresh email gets its own
    // untouched per-email budget. A real person requests a code for a
    // handful of addresses at most (their own, maybe a household member's
    // mistyped one); tens of distinct addresses from one source in a short
    // window is email-bombing third parties and burning the shared mailer's
    // daily send quota (a login DoS for everyone once that quota is spent).
    // Layered with a short global backstop the same way quotes.js is
    // (`_lib/net.js` withinLimitLayered) — clientIp() is only as
    // trustworthy as whatever's in front of this function (see the comment
    // there), so the global counter bounds the blast radius of a burst
    // regardless of how many source IPs it claims to come from, and its
    // short window means a burst that exhausts it self-clears in about a
    // minute instead of locking every caller out for the per-IP window.
    if (!(await withinLimitLayered(`otp:iprl:${clientIp(req)}`, 10, 600, "otp:iprl:global", 60, 60)))
      return A.json(res, 429, { error: "TOO MANY CODE REQUESTS — TRY AGAIN LATER" });

    if (await store.get(`otp:cd:${addr}`))
      return A.json(res, 429, { error: "CODE ALREADY SENT — WAIT 60S BEFORE RESENDING" });
    if ((await store.incr(`otp:n:${addr}`, 3600)) > A.HOURLY_SEND_CAP)
      return A.json(res, 429, { error: "TOO MANY CODES REQUESTED — TRY AGAIN IN AN HOUR" });

    const code = A.newOtp();
    await store.setex(`otp:${addr}`, A.OTP_TTL,
      JSON.stringify({ h: A.hashOtp(addr, code), ts: Date.now() }));
    await store.del(`otp:tries:${addr}`);   // fresh code — reset the atomic attempt counter (see auth-verify-otp.js)
    await store.setex(`otp:cd:${addr}`, A.RESEND_COOLDOWN, "1");

    const sent = await email.sendOtp(addr, code);
    const out = { ok: true, sent: true, expiresInSec: A.OTP_TTL };
    if (sent.devEcho) out.devCode = sent.code;   // test harness only — never in production
    return A.json(res, 200, out);
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
