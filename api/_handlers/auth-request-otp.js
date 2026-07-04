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

module.exports = async (req, res) => {
  if (req.method !== "POST") return A.json(res, 405, { error: "POST only" });
  if (!store.configured())
    return A.json(res, 503, { error: "SERVER AUTH NOT CONFIGURED — no database attached (see README: Upstash Redis)" });
  if (!email.configured())
    return A.json(res, 503, { error: "EMAIL NOT CONFIGURED — set GMAIL_USER + GMAIL_APP_PASSWORD (see README)" });

  let body;
  try { body = await A.readBody(req); } catch { return A.json(res, 400, { error: "invalid JSON" }); }
  const addr = String(body.email || "").trim().toLowerCase();
  if (!A.EMAIL_RE.test(addr)) return A.json(res, 400, { error: "ENTER A VALID EMAIL" });

  try {
    if (await store.get(`otp:cd:${addr}`))
      return A.json(res, 429, { error: "CODE ALREADY SENT — WAIT 60S BEFORE RESENDING" });
    if ((await store.incr(`otp:n:${addr}`, 3600)) > A.HOURLY_SEND_CAP)
      return A.json(res, 429, { error: "TOO MANY CODES REQUESTED — TRY AGAIN IN AN HOUR" });

    const code = A.newOtp();
    await store.setex(`otp:${addr}`, A.OTP_TTL,
      JSON.stringify({ h: A.hashOtp(addr, code), tries: 0, ts: Date.now() }));
    await store.setex(`otp:cd:${addr}`, A.RESEND_COOLDOWN, "1");

    const sent = await email.sendOtp(addr, code);
    const out = { ok: true, sent: true, expiresInSec: A.OTP_TTL };
    if (sent.devEcho) out.devCode = sent.code;   // test harness only — never in production
    return A.json(res, 200, out);
  } catch (err) {
    return A.json(res, 502, { error: String(err.message || err).slice(0, 180) });
  }
};
