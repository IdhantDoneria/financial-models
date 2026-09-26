"use strict";
const $ = (s) => document.querySelector(s);
//: user-controlled fields (email, display name) are rendered via innerHTML
//  below — EMAIL_RE only forbids whitespace/@, so an attacker can register
//  an address/name carrying HTML metacharacters. Escape everything that
//  isn't a server-derived enum/number/timestamp before it reaches the DOM,
//  in both text and quoted-attribute (data-*) contexts.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const LSK = "finmodels.adminkey";
//: The cached key is a standing credential with full account-takeover power
//  (grant/revoke/reset on every user) — cap how long it survives in
//  localStorage so a stale tab/browser profile doesn't hold it forever.
const LSK_TTL_MS = 12 * 60 * 60 * 1000; // 12h
let KEY = null;

async function api(method, body, query = "") {
  const r = await fetch("api/admin" + query, {
    method,
    headers: { "X-Admin-Key": KEY, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

const fdate = (iso) => iso ? new Date(iso).toLocaleString(undefined,
  { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

function render(data) {
  const t = data.totals;
  $("#stats").innerHTML = `
    <div class="stat">SIGNED-UP USERS<b>${t.users}</b></div>
    <div class="stat">ACTIVE PAID<b>${t.activePaid}</b></div>
    <div class="stat">FREE-ACCESS GRANTS<b>${t.granted}</b></div>
    <div class="stat">TRACKED VISITS<b>${data.geo ? data.geo.total : 0}</b></div>`;
  const pa = data.proAccess;
  const proBox = $("#proaccess");
  if (pa && proBox) {
    const meterBox = pa.billingLive
      ? `<div class="stat">MONTHLY LIMITS<b>CHECKOUT LIVE: ALWAYS ENFORCED</b></div>`
      : `<div class="stat">MONTHLY LIMITS (FREE 10 · PRO 50)<b>${pa.metering ? "ENFORCED" : "NOT ENFORCED (STILL COUNTED)"}</b>
           <button id="meter-toggle" class="${pa.metering ? "danger" : ""}">${pa.metering ? "STOP ENFORCING" : "ENFORCE"}</button></div>`;
    proBox.innerHTML = (pa.billingLive
      ? `<div class="stat">PRO MODELS<b>BILLING LIVE: THE PLAN DECIDES</b></div>`
      : `<div class="stat">PRO MODELS (IND AS 116, REVERSE DCF)<b>${pa.open ? "OPEN TO EVERY SIGNED-IN ACCOUNT" : "LOCKED: GRANTED ACCOUNTS ONLY"}</b>
           <button id="pro-toggle" class="${pa.open ? "danger" : ""}">${pa.open ? "LOCK THEM" : "OPEN THEM"}</button></div>`)
      + meterBox;
    const mt = $("#meter-toggle");
    if (mt) mt.onclick = () => {
      if (pa.metering && !confirm("Stop enforcing monthly limits? Usage keeps being counted.")) return;
      if (!pa.metering && !confirm("Enforce monthly limits? Accounts over their plan's allowance will be refused, and guests can no longer run company analyses.")) return;
      act({ action: "metering", on: !pa.metering },
          pa.metering ? "MONTHLY LIMITS NO LONGER ENFORCED" : "MONTHLY LIMITS ENFORCED");
    };
    const tog = $("#pro-toggle");
    if (tog) tog.onclick = () => act({ action: "pro_access", open: !pa.open },
      pa.open ? "PRO MODELS LOCKED TO GRANTED ACCOUNTS" : "PRO MODELS OPEN TO EVERY SIGNED-IN ACCOUNT");
  }
  const tb = $("#users tbody");
  tb.innerHTML = data.rows.map((r) => `
    <tr>
      <td><b>${esc(r.email)}</b>${r.founder ? ` <span class="tag founder">FOUNDER #${r.founder}</span>` : ""}</td>
      <td>${r.signedUp ? esc(r.name || "—") : `<span class="ghosted">not signed up yet</span>`}</td>
      <td class="plan-${r.plan}">${esc(r.planName)}</td>
      <td>${r.via ? `<span class="tag ${r.via}">${esc(r.via.toUpperCase())}</span>` : "—"}</td>
      <td>${r.expiresAt ? new Date(r.expiresAt).toLocaleDateString() : "—"}</td>
      <td class="num">${r.usedThisMonth} / ${r.limit === null || r.limit === undefined ? "∞" : r.limit}
        ${r.usedThisMonth ? `<button class="ghost" data-resetuse="${esc(r.email)}">RESET</button>` : ""}</td>
      <td class="num">${r.loginCount}</td>
      <td>${fdate(r.lastLoginAt)}</td>
      <td>${fdate(r.lastActiveAt)}</td>
      <td>${fdate(r.createdAt)}</td>
      <td>${r.passwordSet
        ? `SET ${fdate(r.pwSetAt)} <button class="ghost" data-resetpw="${esc(r.email)}">RESET</button>`
        : `<span class="ghosted">not set</span>`}</td>
      <td>${r.plan !== "free"
        ? `<button class="danger" data-revoke="${esc(r.email)}">REVOKE</button>`
        : `<button data-grant="${esc(r.email)}">GIFT 30D</button>`}
        <button class="ghost" data-activity="${esc(r.email)}">ACTIVITY</button></td>
    </tr>`).join("") || `<tr><td colspan="12" class="ghosted">No users yet — accounts appear
      here the moment someone verifies their email on the login page.</td></tr>`;
  tb.querySelectorAll("[data-resetpw]").forEach((b) => b.onclick = async () => {
    if (!confirm(`Reset the password for ${b.dataset.resetpw}? They will need to sign in via EMAIL CODE and set a new one.`)) return;
    await act({ action: "reset_password", email: b.dataset.resetpw }, `PASSWORD RESET FOR ${b.dataset.resetpw}`);
  });
  tb.querySelectorAll("[data-resetuse]").forEach((b) => b.onclick = async () => {
    if (!confirm(`Reset this month's analysis count for ${b.dataset.resetuse} to 0?`)) return;
    await act({ action: "reset_usage", email: b.dataset.resetuse }, `USAGE RESET FOR ${b.dataset.resetuse}`);
  });
  tb.querySelectorAll("[data-activity]").forEach((b) => b.onclick = () => {
    activityFor = b.dataset.activity;
    refreshActivity().catch((e) => msg("✗ " + e.message, "err"));
    $("#activity").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  tb.querySelectorAll("[data-revoke]").forEach((b) => b.onclick = async () => {
    await act({ action: "revoke", email: b.dataset.revoke }, `REVOKED ${b.dataset.revoke}`);
  });
  tb.querySelectorAll("[data-grant]").forEach((b) => b.onclick = async () => {
    await act({ action: "grant", email: b.dataset.grant, plan: "unlimited", days: 30 },
              `GRANTED 30 DAYS UNLIMITED TO ${b.dataset.grant}`);
  });
  $("#asof").textContent = `AS OF ${new Date().toLocaleTimeString()} · MONTH ${data.month}`;

  const geo = data.geo || { total: 0, countries: [] };
  const gtb = $("#geo tbody");
  gtb.innerHTML = geo.countries.map((c) => `
    <tr><td>${esc(c.code)}</td><td class="num">${c.count}</td>
      <td class="num">${geo.total ? ((c.count / geo.total) * 100).toFixed(1) : "0.0"}%</td></tr>`
  ).join("") || `<tr><td colspan="3" class="ghosted">No tracked visits yet.</td></tr>`;
}

//: INTERIM manual-UPI flow for early users — a stopgap so we can take
//  payments now WITHOUT the Razorpay integration. This is NOT a replacement
//  for Razorpay: the Razorpay code path stays fully intact and is
//  re-enabled by setting PAYMENTS_MODE=razorpay (+ the Razorpay keys).
//  email/upiRef/note below come straight from the buyer's claim form via
//  /api/billing-claim — they are just as attacker-controlled as the users
//  table's email/name, so every field gets the same esc() treatment.
function renderClaims(data) {
  const rows = (data && data.claims) || [];
  const tb = $("#claims tbody");
  tb.innerHTML = rows.map((c) => `
    <tr>
      <td>${esc(c.email)}</td>
      <td>${esc(c.plan)}</td>
      <td>${esc(c.period)}</td>
      <td class="num">₹${esc(String(c.amountInr))}</td>
      <td>${esc(c.upiRef)}</td>
      <td>${c.note ? esc(c.note) : `<span class="ghosted">—</span>`}</td>
      <td>${fdate(c.createdAt)}</td>
      <td><span class="tag ${esc(c.status)}">${esc(String(c.status).toUpperCase())}</span></td>
      <td>${c.status === "pending"
        ? `<button data-approve="${esc(c.id)}">APPROVE</button>
           <button class="danger" data-reject="${esc(c.id)}">REJECT</button>`
        : "—"}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="ghosted">No UPI claims yet — they appear
      here the moment a buyer submits a payment reference from the PLAN tab.</td></tr>`;
  tb.querySelectorAll("[data-approve]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.approve;
    if (!confirm(`Approve claim ${id}? This immediately grants the plan to the buyer.`)) return;
    await act({ action: "approve_claim", id }, `APPROVED CLAIM ${id}`);
  });
  tb.querySelectorAll("[data-reject]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.reject;
    const reason = prompt("Reason for rejecting this claim (internal note, not shown to buyer):", "");
    if (reason === null) return; // cancelled
    await act({ action: "reject_claim", id, reason }, `REJECTED CLAIM ${id}`);
  });
  $("#claims-asof").textContent = `${rows.length} CLAIM${rows.length === 1 ? "" : "S"}`;
}

async function refreshClaims() { renderClaims(await api("POST", { action: "claims" })); }

function msg(text, cls) { const el = $("#msg"); el.textContent = text; el.className = cls || ""; }

async function act(body, okText) {
  try { await api("POST", body); msg("✔ " + okText, "ok"); await refresh(); }
  catch (e) { msg("✗ " + e.message, "err"); }
}

//: Whose activity the ACTIVITY table shows: one account, or null for all.
let activityFor = null;

const EVENT_LABEL = { signin: "SIGN-IN", analysis: "ANALYSIS", report: "REPORT", export: "EXPORT",
                      premium: "PRO MODEL", plan_view: "PLAN TAB" };

//: The detail column, built only from the event's known fields. Every value
//  came from a user's request, so each is escaped.
function eventDetail(e) {
  const parts = [];
  if (e.method) parts.push(`via ${esc(e.method)}`);
  if (e.source) parts.push(esc(e.source));
  if (e.ticker) parts.push(`<b>${esc(e.ticker)}</b>`);
  if (e.model) parts.push(esc(e.model));
  if (e.models) parts.push(esc([].concat(e.models).join(", ")));
  if (e.mode) parts.push(esc(e.mode));
  if (e.format) parts.push(esc(e.format).toUpperCase());
  if (e.status) parts.push(esc(e.status));
  return parts.join(" · ") || `<span class="ghosted">—</span>`;
}

async function refreshActivity() {
  const data = await api("GET", null, activityFor
    ? `?action=activity&email=${encodeURIComponent(activityFor)}` : "?action=activity");
  $("#act-title").textContent = activityFor
    ? `RECENT ACTIVITY · ${activityFor.toUpperCase()}` : "RECENT ACTIVITY · EVERY ACCOUNT";
  $("#act-all").style.display = activityFor ? "" : "none";
  const rows = data.events || [];
  $("#activity tbody").innerHTML = rows.map((e) => `
    <tr><td>${fdate(e.at)}</td><td>${esc(e.email || "")}</td>
      <td><span class="tag">${esc(EVENT_LABEL[e.type] || e.type)}</span></td><td>${eventDetail(e)}</td></tr>`
  ).join("") || `<tr><td colspan="4" class="ghosted">No activity recorded yet.</td></tr>`;
  const daily = data.daily;
  $("#daily").style.display = daily ? "" : "none";
  if (daily) {
    $("#daily tbody").innerHTML = daily.map((d) => `
      <tr><td>${esc(d.day)}</td><td class="num">${d.signin}</td><td class="num">${d.analysis}</td>
        <td class="num">${d.report}</td><td class="num">${d.export}</td><td class="num">${d.premium}</td>
        <td class="num">${d.plan_view}</td></tr>`).join("");
  }
}

async function refresh() {
  render(await api("GET"));
  await refreshClaims();
  await refreshActivity();
}

function saveKey(key) {
  localStorage.setItem(LSK, JSON.stringify({ key, ts: Date.now() }));
}

function loadKey() {
  let rec;
  try { rec = JSON.parse(localStorage.getItem(LSK) || "null"); } catch { rec = null; }
  if (!rec || !rec.key || !rec.ts) return null;
  if (Date.now() - rec.ts > LSK_TTL_MS) { localStorage.removeItem(LSK); return null; }
  return rec.key;
}

async function unlock(key) {
  KEY = key;
  try {
    await refresh();
    saveKey(key);
    $("#gate").style.display = "none";
    $("#app").style.display = "block";   // CSS default is none — inline must override
  } catch (e) {
    KEY = null;
    $("#gate-err").textContent = "✗ " + e.message;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("#enter").onclick = () => unlock($("#key").value.trim());
  $("#key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#enter").click(); });
  $("#refresh").onclick = () => refresh().catch((e) => msg("✗ " + e.message, "err"));
  $("#act-all").onclick = () => { activityFor = null; refreshActivity().catch((e) => msg("✗ " + e.message, "err")); };
  $("#lock").onclick = () => { localStorage.removeItem(LSK); location.reload(); };
  $("#g-go").onclick = () => {
    const email = $("#g-email").value.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return msg("✗ ENTER A VALID EMAIL", "err");
    act({ action: "grant", email, plan: $("#g-plan").value, days: +$("#g-days").value },
        `GRANTED ${$("#g-days").value} DAYS OF ${$("#g-plan").value.toUpperCase()} TO ${email}`);
  };
  const saved = loadKey();
  if (saved) unlock(saved);
});
