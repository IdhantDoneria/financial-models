// api/_lib/email.js — OTP delivery.
//
// Primary transport: a dedicated Gmail "work" account over SMTP via
// Nodemailer. Configure with:
//   GMAIL_USER          the work address, e.g. finmodels.auth@gmail.com
//   GMAIL_APP_PASSWORD  a 16-char Google "App Password" (requires 2-Step
//                       Verification on that account — a normal login
//                       password will NOT authenticate over SMTP)
//   EMAIL_FROM          optional; overrides the visible sender. Defaults to
//                       "FINMODELS TERMINAL <GMAIL_USER>". Gmail only lets you
//                       send as the account itself (or a verified "Send mail
//                       as" alias), so keep the address here equal to
//                       GMAIL_USER unless such an alias is configured.
//
// Optional fallback: if no Gmail credentials are present but RESEND_API_KEY is
// set, delivery falls back to the original Resend HTTP API. This keeps the
// old setup working and lets you switch back by removing the Gmail vars.
//
// Local test harness (AUTH_DEV_MEMORY=1 with no transport configured): the
// send is echoed back instead of delivered so tests can read the code.
// Production never takes that path — with nothing configured, requesting an
// OTP returns 503.

const GMAIL_USER = process.env.GMAIL_USER;
const GMAIL_PASS = process.env.GMAIL_APP_PASSWORD || process.env.GMAIL_PASSWORD;
const RESEND_KEY = process.env.RESEND_API_KEY;

const GMAIL_OK = !!(GMAIL_USER && GMAIL_PASS);
const RESEND_OK = !GMAIL_OK && !!RESEND_KEY; // fallback only when Gmail is absent
const DEV_ECHO = process.env.AUTH_DEV_MEMORY === "1" && !GMAIL_OK && !RESEND_OK;

const FROM = process.env.EMAIL_FROM
  || (GMAIL_USER ? `FINMODELS TERMINAL <${GMAIL_USER}>`
                 : "FINMODELS TERMINAL <onboarding@resend.dev>");

const subjectFor = (code) => `${code} — your FINMODELS sign-in code`;
const textFor = (code) =>
  `Your FINMODELS one-time sign-in code is ${code}.\n` +
  `It expires in 10 minutes and works once. ` +
  `If you didn't request it, ignore this email.`;

function otpHtml(code) {
  return `<!doctype html>
  <div style="background:#050608;padding:40px 0;font-family:'IBM Plex Mono',Menlo,monospace">
    <div style="max-width:460px;margin:0 auto;background:#0b0e12;border:1px solid #1d2530;padding:32px">
      <div style="color:#ffb000;font-size:18px;letter-spacing:0.3em">FINMODELS TERMINAL</div>
      <p style="color:#c9d4e0;font-size:13px;line-height:1.7;margin:22px 0 8px">
        Your one-time sign-in code:</p>
      <div style="color:#ffb000;font-size:34px;letter-spacing:0.35em;padding:14px 0;
                  border:1px dashed #8a6200;text-align:center">${code}</div>
      <p style="color:#5c6b7d;font-size:11px;line-height:1.7;margin-top:18px">
        The code expires in 10 minutes and works once. If you didn't request it,
        ignore this email — no account action is taken without the code.</p>
    </div>
  </div>`;
}

// One pooled SMTP transport, reused across warm serverless invocations —
// re-creating it per request would re-handshake TLS every time. nodemailer is
// required lazily (only on the first real Gmail send) so the dev-echo and
// Resend paths never need the package installed.
let _tx = null;
function gmailTransport() {
  if (_tx) return _tx;
  const nodemailer = require("nodemailer");
  _tx = nodemailer.createTransport({
    host: "smtp.gmail.com",
    port: 465,
    secure: true, // implicit TLS
    auth: { user: GMAIL_USER, pass: GMAIL_PASS },
  });
  return _tx;
}

async function sendViaGmail(to, code) {
  try {
    const info = await gmailTransport().sendMail({
      from: FROM,
      to,
      subject: subjectFor(code),
      text: textFor(code),
      html: otpHtml(code),
    });
    return { id: info.messageId };
  } catch (err) {
    throw new Error(`gmail send failed: ${String(err.message || err).slice(0, 180)}`);
  }
}

async function sendViaResend(to, code) {
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${RESEND_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      from: FROM, to: [to],
      subject: subjectFor(code),
      text: textFor(code),
      html: otpHtml(code),
    }),
  });
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 200);
    throw new Error(`email send failed (${r.status}): ${detail}`);
  }
  return { id: (await r.json()).id };
}

module.exports = {
  configured: () => DEV_ECHO || GMAIL_OK || RESEND_OK,
  mode: () => (DEV_ECHO ? "dev-echo"
             : GMAIL_OK ? "gmail"
             : RESEND_OK ? "resend"
             : "unconfigured"),

  async sendOtp(to, code) {
    if (DEV_ECHO) return { devEcho: true, code };
    if (GMAIL_OK) return sendViaGmail(to, code);
    return sendViaResend(to, code);
  },
};
