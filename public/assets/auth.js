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
const LS_LAST_EMAIL = "finmodels.lastServerEmail";
const $ = (s) => document.querySelector(s);

//: Remembered locally (not security-sensitive — an email address, never a
//  password) so a returning visitor's login page opens straight on the
//  PASSWORD tab instead of making them retrace the EMAIL CODE signup flow.
function rememberServerEmail(email) {
  try { localStorage.setItem(LS_LAST_EMAIL, email); } catch { /* storage unavailable */ }
}
function lastServerEmail() {
  try { return localStorage.getItem(LS_LAST_EMAIL); } catch { return null; }
}

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
    token: user.token || null,   // server-issued bearer token (OTP mode)
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

/* -------------------------- server OTP mode ---------------------------- */
async function fetchConfig() {
  try {
    const r = await fetch("api/auth-config", { signal: AbortSignal.timeout(6000) });
    if (r.ok) return await r.json();
  } catch { /* static/local host — no serverless functions */ }
  return null;
}

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: AbortSignal.timeout(15000),
  });
  let out = {};
  try { out = await r.json(); } catch { /* non-JSON error body */ }
  if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
  return out;
}

let resendTimer = null;
function startResendCountdown(secs) {
  const btn = $("#otp-resend");
  let left = secs;
  btn.disabled = true;
  clearInterval(resendTimer);
  const tick = () => {
    btn.textContent = left > 0 ? `RESEND CODE (${left})` : "RESEND CODE";
    if (left-- <= 0) { btn.disabled = false; clearInterval(resendTimer); }
  };
  tick();
  resendTimer = setInterval(tick, 1000);
}

async function onOtpSend(e) {
  if (e) e.preventDefault();
  const email = $("#otp-email").value.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showErr("ENTER A VALID EMAIL");
  const btn = $("#otp-send");
  btn.disabled = true; btn.textContent = "SENDING CODE…";
  showErr("");
  try {
    await postJson("api/auth-request-otp", { email });
    $("#otp-step2").style.display = "";
    $("#otp-sentto").textContent = `— SENT TO ${email.toUpperCase()}`;
    $("#otp-code").focus();
    startResendCountdown(60);
  } catch (err) {
    showErr(String(err.message || err));
  }
  btn.disabled = false; btn.textContent = "EMAIL ME A CODE <GO>";
}

async function onOtpVerify() {
  const email = $("#otp-email").value.trim().toLowerCase();
  const code = $("#otp-code").value.replace(/\D/g, "");
  if (code.length !== 6) return showErr("ENTER THE 6-DIGIT CODE FROM YOUR EMAIL");
  const pass = $("#otp-pass").value;
  // A password is required to finish signup; returning users who already
  // have one aren't forced to retype it here (the backend only enforces
  // this for accounts with none on file yet), but validate length either way.
  if (pass && pass.length < 8) return showErr("PASSWORD MUST BE AT LEAST 8 CHARACTERS");
  const btn = $("#otp-verify");
  btn.disabled = true; btn.textContent = "VERIFYING…";
  showErr("");
  try {
    const body = { email, code, name: $("#otp-name").value.trim() };
    if (pass) body.password = pass;
    const out = await postJson("api/auth-verify-otp", body);
    if (out.founder) {   // founders promo: let the winner see it before the redirect
      try { sessionStorage.setItem("finmodels.founderToast", String(out.founder)); } catch { /* ignore */ }
    }
    rememberServerEmail(email);
    finishLogin({ uid: email, name: (out.user && out.user.name) || email,
                  provider: "otp", token: out.token }, true);
    return;
  } catch (err) {
    showErr(String(err.message || err));
    if (/PASSWORD IS REQUIRED/.test(String(err.message || err))) $("#otp-pass").focus();
  }
  btn.disabled = false; btn.textContent = "VERIFY & SIGN IN <GO>";
}

//: Password sign-in for returning users (server mode). The password was set
//  on the email-code screen and lives server-side as an scrypt hash.
async function onPwLogin(e) {
  if (e) e.preventDefault();
  const email = $("#pw-email").value.trim().toLowerCase();
  const pass = $("#pw-pass").value;
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showErr("ENTER A VALID EMAIL");
  if (!pass) return showErr("ENTER YOUR PASSWORD");
  const btn = $("#pw-btn");
  btn.disabled = true; btn.textContent = "VERIFYING…";
  showErr("");
  try {
    const out = await postJson("api/auth-login", { email, password: pass });
    rememberServerEmail(email);
    finishLogin({ uid: email, name: (out.user && out.user.name) || email,
                  provider: "otp", token: out.token }, true);
    return;
  } catch (err) {
    showErr(String(err.message || err));
  }
  btn.disabled = false; btn.textContent = "SIGN IN <GO>";
}

function setServerTab(which) {
  $("#stab-code").classList.toggle("on", which === "code");
  $("#stab-pass").classList.toggle("on", which === "pass");
  $("#f-otp").style.display = which === "code" ? "" : "none";
  $("#f-pw").style.display = which === "pass" ? "" : "none";
  showErr("");
}

function initServerAuth(cfg) {
  if (!cfg || !cfg.serverAuth) {
    // device-local mode stays; say so above the (still-present) support line
    $(".afoot").innerHTML = "SERVER AUTH OFFLINE — DEVICE-LOCAL MODE<br>" + $(".afoot").innerHTML;
    return;
  }
  // server mode: email OTP (+ a required password) replaces local accounts.
  // A device that has already completed a server sign-in once jumps straight
  // to PASSWORD — otherwise, signing out would mean redoing the email-code
  // flow every time, which reads as "onboarding all over again".
  document.querySelector(".atabs").style.display = "none";
  $("#f-signin").style.display = "none";
  $("#f-signup").style.display = "none";
  $("#stabs").style.display = "";
  const known = lastServerEmail();
  if (known) { $("#pw-email").value = known; setServerTab("pass"); $("#pw-pass").focus(); }
  else setServerTab("code");
  $("#stab-code").onclick = () => setServerTab("code");
  $("#stab-pass").onclick = () => setServerTab("pass");
  $("#f-otp").addEventListener("submit", onOtpSend);
  $("#f-pw").addEventListener("submit", onPwLogin);
  $("#otp-verify").onclick = onOtpVerify;
  $("#otp-resend").onclick = () => onOtpSend();
  $("#otp-code").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); onOtpVerify(); }
  });
  document.querySelector(".apriv").innerHTML =
    "🔒 SERVER AUTH ENABLED — sign-in codes are emailed to you (10-minute expiry, " +
    "single use). Your profile (email, name, sign-in history, optional scrypt-hashed " +
    "password) is stored in the cloud; analyses and model work still run entirely in " +
    "<b>your</b> browser.";

  // complimentary-access notice — reviewed and granted manually by the team,
  // rather than an automatic promo (see admin.js `grant`).
  const c = $("#concierge");
  c.style.display = "";
  c.innerHTML = `COMPLIMENTARY ACCESS — Individuals and organisations may request complimentary
    access to FINMODELS TERMINAL by writing to <b>finmodels10@gmail.com</b> with a brief note on
    intended use. Each request is reviewed personally by our team, and a response is provided
    within <b>48 business hours</b>.`;
}

async function initGoogle(cfg) {
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

  fetchConfig().then((cfg) => { initServerAuth(cfg); initGoogle(cfg); });
});
