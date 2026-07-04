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

/** Parse the JSON body (Vercel pre-parses; fall back to the raw stream). */
async function readBody(req) {
  if (req.body !== undefined && req.body !== null) {
    return typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body;
  }
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
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

async function getSession(req) {
  const token = bearer(req);
  if (!token) return null;
  const raw = await store.get(`sess:${token}`);
  if (!raw) return null;
  try { return { token, ...JSON.parse(raw) }; } catch { return null; }
}

module.exports = {
  EMAIL_RE, SESSION_TTL, OTP_TTL, OTP_MAX_TRIES, RESEND_COOLDOWN, HOURLY_SEND_CAP,
  PW_MIN, PW_MAX_TRIES, PW_TRY_WINDOW,
  hashOtp, newToken, newOtp, timingSafeEq, readBody, json, bearer, getSession,
  hashPasswordRecord, verifyPasswordRecord,
};
