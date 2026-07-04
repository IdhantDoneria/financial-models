// /api/auth — single serverless function for every auth endpoint.
//
// Vercel's Hobby plan caps a deployment at 12 functions, so the auth
// endpoints live in api/_handlers/ (underscore dirs don't deploy as
// functions) behind this one dispatcher. The public URLs are unchanged:
// vercel.json rewrites /api/auth-<action> here with ?action=<action>, and
// the dispatcher also derives the action from the original path so the
// local dev server can route the legacy names straight through.

const HANDLERS = {
  "config": require("./_handlers/auth-config.js"),
  "request-otp": require("./_handlers/auth-request-otp.js"),
  "verify-otp": require("./_handlers/auth-verify-otp.js"),
  "login": require("./_handlers/auth-login.js"),
  "me": require("./_handlers/auth-me.js"),
  "logout": require("./_handlers/auth-logout.js"),
};

module.exports = async (req, res) => {
  const url = new URL(req.url || "/", "http://internal");
  const action = url.searchParams.get("action")
    || (/\/api\/auth-([\w-]+)/.exec(url.pathname) || [])[1];
  const handler = HANDLERS[action];
  if (!handler) {
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.status(404).json({ error: `unknown auth action '${action || ""}'` });
  }
  return handler(req, res);
};
