// api/auth-config.js — public auth configuration for the login page.
//
// The FINMODELS terminal keeps ALL user data on-device (accounts, sessions,
// analysis history live in localStorage; passwords are PBKDF2-hashed with Web
// Crypto and never transmitted). The only server-side piece of auth is this
// endpoint, which exposes the *public* Google OAuth client id so the login
// page can render a real "Continue with Google" button via Google Identity
// Services. Set GOOGLE_CLIENT_ID in the Vercel project's environment
// variables (a standard OAuth "Web application" client with this site's
// origin authorised); until then the page shows an explicit
// "not configured" state instead of a broken button.

const store = require("./_lib/store");
const email = require("./_lib/email");
const B = require("./_lib/billing");

module.exports = async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "public, s-maxage=60, stale-while-revalidate=300");
  const serverAuth = store.configured() && email.configured();
  // founders promo: how many of the first-20 free-month slots remain — the
  // login page shows this to prospective sign-ups.
  let foundersLeft = null;
  if (serverAuth) {
    try { foundersLeft = await B.foundersLeft(); } catch { /* store hiccup */ }
  }
  res.status(200).json({
    googleClientId: process.env.GOOGLE_CLIENT_ID || null,
    // email-OTP backend: on when a database AND a mailer are configured;
    // the login page switches from device-local to server mode automatically.
    serverAuth,
    passwordLogin: serverAuth,   // email+password works once set via OTP
    foundersLeft,
    founderPlanName: B.PLANS[B.FOUNDER_PLAN].name,
    founderDays: B.FOUNDER_DAYS,
    storage: store.mode(),
    email: email.mode(),
  });
};
