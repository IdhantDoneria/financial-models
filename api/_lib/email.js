// api/_lib/email.js — OTP delivery via Resend (https://resend.com).
//
// Configure with RESEND_API_KEY (free tier: 100 emails/day). EMAIL_FROM
// sets the sender; the default onboarding@resend.dev sender only delivers
// to the Resend account owner's own inbox — verify a domain and set
// EMAIL_FROM="FINMODELS <auth@yourdomain.com>" for real users.
//
// In the local test harness (AUTH_DEV_MEMORY=1 and no API key) the send is
// echoed back instead of delivered so tests can read the code. Production
// never takes that path: without a key, requesting an OTP returns 503.

const KEY = process.env.RESEND_API_KEY;
const FROM = process.env.EMAIL_FROM || "FINMODELS TERMINAL <onboarding@resend.dev>";
const DEV_ECHO = process.env.AUTH_DEV_MEMORY === "1" && !KEY;

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

module.exports = {
  configured: () => DEV_ECHO || !!KEY,
  mode: () => (DEV_ECHO ? "dev-echo" : KEY ? "resend" : "unconfigured"),

  async sendOtp(to, code) {
    if (DEV_ECHO) return { devEcho: true, code };
    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: FROM, to: [to],
        subject: `${code} — your FINMODELS sign-in code`,
        html: otpHtml(code),
      }),
    });
    if (!r.ok) {
      const detail = (await r.text()).slice(0, 200);
      throw new Error(`email send failed (${r.status}): ${detail}`);
    }
    return { id: (await r.json()).id };
  },
};
