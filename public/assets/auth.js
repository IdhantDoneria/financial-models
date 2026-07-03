/* FINMODELS TERMINAL — device-local authentication.
 *
 * Philosophy: the entire product runs client-side (Pyodide/WASM), so auth
 * does too. Accounts live in localStorage with PBKDF2-SHA256-hashed passwords
 * (Web Crypto, per-account random salt, 150k iterations); nothing is ever
 * transmitted. "Continue with Google" is real Google Identity Services and
 * lights up when the deployment exposes a GOOGLE_CLIENT_ID via
 * /api/auth-config; otherwise it declares itself unconfigured instead of
 * pretending. A guest path keeps the terminal open to everyone.
 */
"use strict";

const LS_ACCOUNTS = "finmodels.accounts";
const LS_SESSION = "finmodels.session";
const $ = (s) => document.querySelector(s);

const DAY = 86_400_000;
const SESSION_SHORT = DAY / 2;      // 12 h without "keep me signed in"
const SESSION_LONG = 30 * DAY;

/* ------------------------------ storage -------------------------------- */
function accounts() {
  try { return JSON.parse(localStorage.getItem(LS_ACCOUNTS) || "{}"); }
  catch { return {}; }
}
function saveAccounts(a) { localStorage.setItem(LS_ACCOUNTS, JSON.stringify(a)); }

function session() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_SESSION) || "null");
    if (!s || !s.uid) return null;
    if (s.exp && Date.now() > s.exp) { localStorage.removeItem(LS_SESSION); return null; }
    return s;
  } catch { return null; }
}

function finishLogin(user, remember) {
  localStorage.setItem(LS_SESSION, JSON.stringify({
    uid: user.uid, name: user.name, provider: user.provider,
    ts: Date.now(), exp: Date.now() + (remember ? SESSION_LONG : SESSION_SHORT),
  }));
  location.replace("./");
}

/* ------------------------------ crypto --------------------------------- */
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function hashPassword(password, saltB64, iterations) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: unb64(saltB64), iterations }, key, 256);
  return b64(bits);
}

/* ------------------------------- UI ------------------------------------ */
function showErr(msg) {
  const el = $("#auth-err");
  el.textContent = msg ? "✗ " + msg : "";
  el.classList.toggle("on", !!msg);
}

function setMode(mode) {
  $("#tab-signin").classList.toggle("on", mode === "signin");
  $("#tab-signup").classList.toggle("on", mode === "signup");
  $("#f-signin").style.display = mode === "signin" ? "" : "none";
  $("#f-signup").style.display = mode === "signup" ? "" : "none";
  showErr("");
}

function passwordStrength(p) {
  let s = 0;
  if (p.length >= 8) s++;
  if (p.length >= 12) s++;
  if (/[A-Z]/.test(p) && /[a-z]/.test(p)) s++;
  if (/\d/.test(p)) s++;
  if (/[^A-Za-z0-9]/.test(p)) s++;
  return s; // 0–5
}

/* ------------------------------ handlers ------------------------------- */
async function onSignup(e) {
  e.preventDefault();
  const name = $("#su-name").value.trim();
  const email = $("#su-email").value.trim().toLowerCase();
  const p1 = $("#su-pass").value;
  const p2 = $("#su-pass2").value;
  if (name.length < 2) return showErr("ENTER YOUR NAME");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showErr("ENTER A VALID EMAIL");
  if (p1.length < 8) return showErr("PASSWORD MUST BE AT LEAST 8 CHARACTERS");
  if (p1 !== p2) return showErr("PASSWORDS DO NOT MATCH");
  const all = accounts();
  if (all[email] && all[email].hash)
    return showErr("AN ACCOUNT WITH THIS EMAIL EXISTS — SIGN IN INSTEAD");
  const btn = $("#su-btn");
  btn.disabled = true; btn.textContent = "HASHING (PBKDF2 · 150K)…";
  try {
    const salt = b64(crypto.getRandomValues(new Uint8Array(16)));
    const iter = 150_000;
    const hash = await hashPassword(p1, salt, iter);
    all[email] = { ...(all[email] || {}), name, salt, hash, iter,
                   provider: "password", created: Date.now() };
    saveAccounts(all);
    finishLogin({ uid: email, name, provider: "password" }, true);
  } catch (err) {
    showErr("SIGNUP FAILED: " + err);
    btn.disabled = false; btn.textContent = "CREATE ACCOUNT <GO>";
  }
}

async function onSignin(e) {
  e.preventDefault();
  const email = $("#si-email").value.trim().toLowerCase();
  const pass = $("#si-pass").value;
  const acct = accounts()[email];
  if (!acct) return showErr("NO ACCOUNT FOR THIS EMAIL ON THIS DEVICE — CREATE ONE, OR USE GOOGLE/GUEST");
  if (!acct.hash) return showErr("THIS ACCOUNT USES GOOGLE SIGN-IN — USE THE GOOGLE BUTTON");
  const btn = $("#si-btn");
  btn.disabled = true; btn.textContent = "VERIFYING…";
  try {
    const hash = await hashPassword(pass, acct.salt, acct.iter || 150_000);
    if (hash !== acct.hash) {
      showErr("INVALID PASSWORD");
      btn.disabled = false; btn.textContent = "SIGN IN <GO>";
      return;
    }
    finishLogin({ uid: email, name: acct.name || email, provider: "password" },
                $("#si-remember").checked);
  } catch (err) {
    showErr("SIGN-IN FAILED: " + err);
    btn.disabled = false; btn.textContent = "SIGN IN <GO>";
  }
}

/* --------------------------- Google (GIS) ------------------------------ */
function decodeJwtPayload(token) {
  const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(decodeURIComponent(escape(atob(part))));
}

async function initGoogle() {
  let cfg = null;
  try {
    const r = await fetch("api/auth-config", { signal: AbortSignal.timeout(6000) });
    if (r.ok) cfg = await r.json();
  } catch { /* static/local host — no serverless functions */ }
  const cid = cfg && cfg.googleClientId;
  if (!cid) {
    $("#gfake").disabled = true;
    $("#ghint").textContent =
      "GOOGLE SSO NOT CONFIGURED ON THIS DEPLOYMENT — set GOOGLE_CLIENT_ID " +
      "in Vercel env vars (OAuth web client) to enable. Email & guest access work fully.";
    return;
  }
  await new Promise((ok, bad) => {
    const s = document.createElement("script");
    s.src = "https://accounts.google.com/gsi/client";
    s.onload = ok; s.onerror = bad;
    document.head.appendChild(s);
  });
  google.accounts.id.initialize({
    client_id: cid,
    callback: (resp) => {
      try {
        const p = decodeJwtPayload(resp.credential);
        const email = (p.email || "").toLowerCase();
        if (!email) return showErr("GOOGLE DID NOT RETURN AN EMAIL");
        const all = accounts();
        all[email] = { ...(all[email] || {}), name: p.name || email,
                       provider: all[email] && all[email].hash ? "password+google" : "google",
                       created: (all[email] && all[email].created) || Date.now() };
        saveAccounts(all);
        finishLogin({ uid: email, name: p.name || email, provider: "google" }, true);
      } catch (err) { showErr("GOOGLE SIGN-IN FAILED: " + err); }
    },
  });
  $("#gwrap").classList.add("live");
  google.accounts.id.renderButton($("#gbtn"),
    { theme: "filled_black", size: "large", text: "continue_with", width: 300 });
}

/* -------------------------------- boot --------------------------------- */
window.addEventListener("DOMContentLoaded", () => {
  if (session()) { location.replace("./"); return; }   // already signed in

  $("#tab-signin").onclick = () => setMode("signin");
  $("#tab-signup").onclick = () => setMode("signup");
  $("#f-signin").addEventListener("submit", onSignin);
  $("#f-signup").addEventListener("submit", onSignup);
  $("#guest").onclick = () =>
    finishLogin({ uid: "guest", name: "GUEST", provider: "guest" }, true);

  document.querySelectorAll(".peek").forEach((b) => {
    b.onclick = () => {
      const inp = $("#" + b.dataset.for);
      inp.type = inp.type === "password" ? "text" : "password";
      b.textContent = inp.type === "password" ? "SHOW" : "HIDE";
    };
  });
  $("#su-pass").addEventListener("input", () => {
    const s = passwordStrength($("#su-pass").value);
    const bar = $("#pmeter div");
    bar.style.width = (s / 5) * 100 + "%";
    bar.style.background = s >= 4 ? "var(--green)" : s >= 2 ? "var(--amber)" : "var(--red)";
  });

  initGoogle();
});
