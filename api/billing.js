// /api/billing — single serverless function for every billing endpoint
// (same function-count consolidation as /api/auth; public URLs preserved
// by vercel.json rewrites, path-suffix dispatch for the dev server).
//
// Body parsing stays OFF for the whole function: the Razorpay webhook must
// verify its signature over the raw bytes, and the other handlers read the
// body through A.readBody, which falls back to the raw stream anyway.

const HANDLERS = {
  "config": require("./_handlers/billing-config.js"),
  "order": require("./_handlers/billing-order.js"),
  "verify": require("./_handlers/billing-verify.js"),
  "webhook": require("./_handlers/billing-webhook.js"),
};

module.exports = async (req, res) => {
  const url = new URL(req.url || "/", "http://internal");
  const action = url.searchParams.get("action")
    || (/\/api\/billing-([\w-]+)/.exec(url.pathname) || [])[1];
  const handler = HANDLERS[action];
  if (!handler) {
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.status(404).json({ error: `unknown billing action '${action || ""}'` });
  }
  return handler(req, res);
};

module.exports.config = { api: { bodyParser: false } };
