// Full-stack local server: public/ statics + the real api/*.js functions in
// one process, with the auth backend in dev-memory mode (AUTH_DEV_MEMORY=1,
// email echoed instead of sent). Mirrors the Vercel runtime closely enough
// to browser-test the complete OTP login flow locally.
//
//     node scripts/dev_auth_server.js [port]

process.env.AUTH_DEV_MEMORY = process.env.AUTH_DEV_MEMORY || "1";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "public");
const PORT = parseInt(process.argv[2] || "8124", 10);

const API = {
  "auth-config": require("../api/auth-config.js"),
  "auth-request-otp": require("../api/auth-request-otp.js"),
  "auth-verify-otp": require("../api/auth-verify-otp.js"),
  "auth-me": require("../api/auth-me.js"),
  "auth-logout": require("../api/auth-logout.js"),
  "quotes": require("../api/quotes.js"),
  "billing-config": require("../api/billing-config.js"),
  "billing-order": require("../api/billing-order.js"),
  "billing-verify": require("../api/billing-verify.js"),
  "billing-webhook": require("../api/billing-webhook.js"),
  "usage": require("../api/usage.js"),
};

const MIME = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".csv": "text/csv", ".png": "image/png",
  ".svg": "image/svg+xml", ".py": "text/x-python", ".md": "text/markdown",
};

http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const apiMatch = /^\/api\/([\w-]+)$/.exec(url.pathname);

  if (apiMatch) {
    const handler = API[apiMatch[1]];
    if (!handler) { res.writeHead(404).end("{}"); return; }
    // Vercel-style helpers on the raw Node response
    res.status = (c) => { res.statusCode = c; return res; };
    res.json = (o) => { res.end(JSON.stringify(o)); };
    try { await handler(req, res); }
    catch (e) { res.writeHead(500, { "content-type": "application/json" })
                   .end(JSON.stringify({ error: String(e).slice(0, 200) })); }
    return;
  }

  let p = url.pathname === "/" ? "/index.html" : url.pathname;
  if (!path.extname(p) && fs.existsSync(path.join(ROOT, p + ".html"))) p += ".html"; // cleanUrls
  const file = path.join(ROOT, path.normalize(p).replace(/^([/\\])+/, ""));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end("not found");
    return;
  }
  res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}).listen(PORT, "127.0.0.1", () =>
  console.log(`dev full-stack server: http://127.0.0.1:${PORT} (auth: memory + email echo)`));
