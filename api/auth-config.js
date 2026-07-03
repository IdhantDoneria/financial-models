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

module.exports = (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "public, s-maxage=300, stale-while-revalidate=3600");
  res.status(200).json({
    googleClientId: process.env.GOOGLE_CLIENT_ID || null,
  });
};
