// api/_lib/google.js — verifies a Google Identity Services ID token
// server-side: RS256 signature checked against Google's published JWKS,
// then issuer/audience/expiry/email-verified checked. No google-auth-library
// dependency — this repo keeps its footprint to nodemailer only, and
// verifying an RS256 JWT against a JWKS is a few dozen lines with Node's
// built-in crypto (importing a JWK straight into crypto.createPublicKey
// needs Node >=15.12, well under this project's >=18 floor).

const crypto = require("crypto");

const CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs";
const ISSUERS = new Set(["accounts.google.com", "https://accounts.google.com"]);
const JWKS_TTL_MS = 6 * 60 * 60 * 1000; // Google rotates keys infrequently

let cache = { keys: [], fetchedAt: 0 };

async function fetchJwks() {
  const r = await fetch(CERTS_URL);
  if (!r.ok) throw new Error(`could not fetch Google signing keys (${r.status})`);
  const j = await r.json();
  cache = { keys: Array.isArray(j.keys) ? j.keys : [], fetchedAt: Date.now() };
  return cache.keys;
}

async function keyFor(kid) {
  const fresh = Date.now() - cache.fetchedAt < JWKS_TTL_MS;
  let keys = fresh ? cache.keys : await fetchJwks();
  let jwk = keys.find((k) => k.kid === kid);
  if (!jwk) jwk = (await fetchJwks()).find((k) => k.kid === kid); // rotated — refetch once
  if (!jwk) throw new Error("no matching Google signing key");
  return crypto.createPublicKey({ key: jwk, format: "jwk" });
}

const b64url = (s) => Buffer.from(String(s).replace(/-/g, "+").replace(/_/g, "/"), "base64");

/** Verifies a Google-issued ID token (signature, issuer, audience, expiry,
 *  email_verified) and returns its payload. Throws with a user-safe message
 *  on any failure. */
async function verifyGoogleIdToken(idToken, audience) {
  const parts = String(idToken || "").split(".");
  if (parts.length !== 3) throw new Error("malformed token");
  const [h, p, s] = parts;

  let header, payload;
  try {
    header = JSON.parse(b64url(h).toString("utf8"));
    payload = JSON.parse(b64url(p).toString("utf8"));
  } catch { throw new Error("malformed token"); }

  if (header.alg !== "RS256") throw new Error("unexpected token algorithm");

  const key = await keyFor(header.kid);
  const verified = crypto.verify("RSA-SHA256", Buffer.from(`${h}.${p}`), key, b64url(s));
  if (!verified) throw new Error("bad signature");

  if (!ISSUERS.has(payload.iss)) throw new Error("unexpected issuer");
  if (payload.aud !== audience) throw new Error("audience mismatch");
  if (!payload.exp || Date.now() / 1000 > payload.exp) throw new Error("token expired");
  if (!payload.email) throw new Error("token has no email");
  if (payload.email_verified !== true) throw new Error("email not verified with Google");

  return payload; // { email, email_verified, name, sub, iss, aud, exp, ... }
}

module.exports = { verifyGoogleIdToken };
