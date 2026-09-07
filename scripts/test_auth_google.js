// Integration test for the server-side Google Sign-In verification path
// (api/_handlers/auth-google.js + api/_lib/google.js).
//
// Runs the real handler in-process against the in-memory store
// (AUTH_DEV_MEMORY=1). Google's JWKS endpoint is stubbed with a locally
// generated RSA keypair — no network, no browser, no real Google token.
//
//     node scripts/test_auth_google.js

process.env.AUTH_DEV_MEMORY = "1";
const CLIENT_ID = "test-client-id.apps.googleusercontent.com";
process.env.GOOGLE_CLIENT_ID = CLIENT_ID;

const assert = require("node:assert");
const crypto = require("node:crypto");

// --- stub Google's JWKS endpoint with a locally generated keypair --------
const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const KID = "test-kid-1";
const jwk = publicKey.export({ format: "jwk" });
jwk.kid = KID;
jwk.alg = "RS256";
jwk.use = "sig";

const realFetch = global.fetch;
global.fetch = async (url, opts) => {
  if (String(url) === "https://www.googleapis.com/oauth2/v3/certs") {
    return { ok: true, json: async () => ({ keys: [jwk] }) };
  }
  return realFetch(url, opts);
};

const b64url = (buf) => Buffer.from(buf).toString("base64url");
function signIdToken({ email, aud = CLIENT_ID, iss = "https://accounts.google.com",
                        emailVerified = true, name = "Ada Lovelace", expInSec = 3600, kid = KID }) {
  const header = { alg: "RS256", kid, typ: "JWT" };
  const payload = { iss, aud, email, email_verified: emailVerified, name,
                     sub: "1234567890", iat: Math.floor(Date.now() / 1000),
                     exp: Math.floor(Date.now() / 1000) + expInSec };
  const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const sig = crypto.sign("RSA-SHA256", Buffer.from(signingInput), privateKey);
  return `${signingInput}.${b64url(sig)}`;
}

const authGoogle = require("../api/_handlers/auth-google.js");
const me = require("../api/_handlers/auth-me.js");
const logout = require("../api/_handlers/auth-logout.js");

function call(handler, { method = "POST", body, token } = {}) {
  const req = { method, body, headers: token ? { authorization: `Bearer ${token}` } : {} };
  const res = {
    headers: {}, code: 0, out: null,
    setHeader(k, v) { this.headers[k] = v; },
    status(c) { this.code = c; return this; },
    json(o) { this.out = o; },
  };
  return Promise.resolve(handler(req, res)).then(() => res);
}

let passed = 0;
function ok(cond, label) {
  if (!cond) { console.error(`  ✗ ${label}`); process.exitCode = 1; }
  else { console.log(`  ✓ ${label}`); passed++; }
}

(async () => {
  console.log("· valid Google ID token → server-verified session");
  let token1 = signIdToken({ email: "ADA@Example.com" });
  let r = await call(authGoogle, { body: { credential: token1 } });
  ok(r.code === 200 && r.out.ok && typeof r.out.token === "string",
     `issues a bearer session (code=${r.code})`);
  ok(r.out.user.email === "ada@example.com", "lowercases the email");
  ok(r.out.user.name === "Ada Lovelace", "carries the Google display name");
  ok(r.out.user.loginCount === 1, "first login recorded (loginCount=1)");
  const sessionToken = r.out.token;

  console.log("· issued session works against /api/auth-me");
  r = await call(me, { method: "GET", token: sessionToken });
  ok(r.code === 200 && r.out.user && r.out.user.email === "ada@example.com",
     "session resolves to the same account");

  console.log("· returning Google sign-in updates the same record");
  const token2 = signIdToken({ email: "ada@example.com" });
  r = await call(authGoogle, { body: { credential: token2 } });
  ok(r.code === 200 && r.out.user.loginCount === 2, "loginCount increments on repeat sign-in");

  console.log("· logout revokes the session");
  r = await call(logout, { token: sessionToken });
  ok(r.code === 200, "logout accepted");
  r = await call(me, { method: "GET", token: sessionToken });
  ok(r.code === 401, "revoked token is rejected (401)");

  console.log("· tampered signature is rejected");
  const [h, p] = signIdToken({ email: "eve@example.com" }).split(".");
  const forged = `${h}.${p}.` + b64url(crypto.randomBytes(256));
  r = await call(authGoogle, { body: { credential: forged } });
  ok(r.code === 401, `bad signature rejected (code=${r.code})`);

  console.log("· wrong audience is rejected");
  r = await call(authGoogle, { body: { credential: signIdToken({ email: "x@example.com", aud: "someone-elses-client-id" }) } });
  ok(r.code === 401, `audience mismatch rejected (code=${r.code})`);

  console.log("· expired token is rejected");
  r = await call(authGoogle, { body: { credential: signIdToken({ email: "x@example.com", expInSec: -60 }) } });
  ok(r.code === 401, `expired token rejected (code=${r.code})`);

  console.log("· unverified email is rejected");
  r = await call(authGoogle, { body: { credential: signIdToken({ email: "x@example.com", emailVerified: false }) } });
  ok(r.code === 401, `unverified email rejected (code=${r.code})`);

  console.log("· wrong HTTP method rejected");
  r = await call(authGoogle, { method: "GET" });
  ok(r.code === 405, "GET rejected (405)");

  console.log("· misconfigured deployment (no GOOGLE_CLIENT_ID) fails closed");
  delete process.env.GOOGLE_CLIENT_ID;
  r = await call(authGoogle, { body: { credential: token1 } });
  ok(r.code === 503, `503 when GOOGLE_CLIENT_ID unset (code=${r.code})`);
  process.env.GOOGLE_CLIENT_ID = CLIENT_ID;

  global.fetch = realFetch;
  console.log(`\n${passed} checks passed.`);
  if (process.exitCode) console.error("SOME CHECKS FAILED");
})();
