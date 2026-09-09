// api/_lib/auth.js — shared helpers for the OTP auth endpoints.

const crypto = require("crypto");
const store = require("./store");

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PEPPER = process.env.AUTH_SECRET || ""; // optional server-side pepper

const SESSION_TTL = 30 * 86_400;   // seconds — 30 days
const OTP_TTL = 600;               // 10 minutes
const OTP_MAX_TRIES = 5;
const RESEND_COOLDOWN = 60;        // seconds between sends
const HOURLY_SEND_CAP = 5;         // per email address

const sha256 = (s) => crypto.createHash("sha256").update(s).digest("hex");
const hashOtp = (email, code) => sha256(`${email}:${code}:${PEPPER}`);
const newToken = () => crypto.randomBytes(32).toString("base64url");
const newOtp = () => String(crypto.randomInt(0, 1_000_000)).padStart(6, "0");

function timingSafeEq(a, b) {
  const ba = Buffer.from(a), bb = Buffer.from(b);
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

/* Password layer on top of OTP: set after a code has proven the email, and
 * stored server-side as scrypt(salt, 64) — the password itself never
 * persists anywhere. Later sign-ins can then skip the email round-trip. */
const PW_MIN = 8;
const PW_MAX_TRIES = 10;           // wrong passwords per window per email
const PW_TRY_WINDOW = 900;         // seconds

function hashPasswordRecord(password) {
  const salt = crypto.randomBytes(16).toString("hex");
  return { algo: "scrypt-64", salt,
           hash: crypto.scryptSync(password, salt, 64).toString("hex") };
}
function verifyPasswordRecord(password, rec) {
  if (!rec || !rec.salt || !rec.hash) return false;
  return timingSafeEq(crypto.scryptSync(password, rec.salt, 64).toString("hex"), rec.hash);
}

// The raw-stream fallback path (used whenever Vercel hasn't already
// pre-parsed req.body — every non-Vercel host, and the raw-signature path
// billing-webhook.js reads for itself) buffered without limit until this
// cap was added: a single unauthenticated request could push an arbitrary
// number of megabytes into memory before JSON.parse ever ran. None of these
// endpoints legitimately need more than a few KB (email/code/name/password,
// a Google id_token, a plan/grant body) — 256KB leaves generous headroom.
const MAX_BODY_BYTES = 256 * 1024;

class BodyTooLargeError extends Error {}

/** Read the raw request body as a string, rejecting once MAX_BODY_BYTES is
 *  exceeded (checked incrementally, not just via Content-Length — a caller
 *  can omit or lie about that header). */
async function readRawBody(req) {
  const len = req.headers && req.headers["content-length"];
  if (len && Number(len) > MAX_BODY_BYTES) throw new BodyTooLargeError("body too large");
  const chunks = [];
  let total = 0;
  for await (const c of req) {
    total += c.length;
    if (total > MAX_BODY_BYTES) throw new BodyTooLargeError("body too large");
    chunks.push(c);
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** Parse the JSON body (Vercel pre-parses; fall back to the raw stream). */
async function readBody(req) {
  if (req.body !== undefined && req.body !== null) {
    return typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body;
  }
  const raw = await readRawBody(req);
  return raw ? JSON.parse(raw) : {};
}

function json(res, code, obj) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.status(code).json(obj);
}

function bearer(req) {
  const h = req.headers && (req.headers.authorization || req.headers.Authorization);
  const m = /^Bearer\s+(.+)$/i.exec(h || "");
  return m ? m[1].trim() : null;
}

// Session transport: an httpOnly cookie (the browser client relies on this
// exclusively — it never reads or stores the token) with a Bearer header
// kept as a fallback for any non-browser caller (the test harness, a future
// API consumer). Cookie wins when both are present.
const SESSION_COOKIE = "fm_sess";

function cookieToken(req) {
  const raw = req.headers && req.headers.cookie;
  if (!raw) return null;
  for (const part of String(raw).split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() === SESSION_COOKIE) return decodeURIComponent(part.slice(eq + 1).trim());
  }
  return null;
}

//: SameSite=Strict is safe here (not just convenient) — every legitimate
//  caller of this cookie is a same-origin fetch() from this app's own pages;
//  nothing legitimately needs it sent cross-site, so Strict also means this
//  cookie carries no CSRF exposure of its own without needing CSRF tokens.
//  Secure requires HTTPS in production; browsers treat localhost/127.0.0.1
//  as a secure context too, so local dev over plain HTTP still works.
function setSessionCookie(res, token, maxAgeSec) {
  res.setHeader("Set-Cookie",
    `${SESSION_COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAgeSec}`);
}

function clearSessionCookie(res) {
  res.setHeader("Set-Cookie", `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`);
}

async function getSession(req) {
  const token = cookieToken(req) || bearer(req);
  if (!token) return null;
  const raw = await store.get(`sess:${token}`);
  if (!raw) return null;
  try { return { token, ...JSON.parse(raw) }; } catch { return null; }
}

module.exports = {
  EMAIL_RE, SESSION_TTL, OTP_TTL, OTP_MAX_TRIES, RESEND_COOLDOWN, HOURLY_SEND_CAP,
  PW_MIN, PW_MAX_TRIES, PW_TRY_WINDOW, MAX_BODY_BYTES, BodyTooLargeError,
  hashOtp, newToken, newOtp, timingSafeEq, readBody, readRawBody, json, bearer, cookieToken, getSession,
  setSessionCookie, clearSessionCookie,
  hashPasswordRecord, verifyPasswordRecord,
};
