// api/_lib/activity.js — what each signed-in account does, for the operator.
//
// Recorded server-side, in the same Redis store as accounts and plans:
//   events:<email>  the account's last PER_USER events (newest first)
//   events:all      the last ALL events across every account (admin feed)
//   act:<email>     time of the account's latest event
//   stat:<day>:<t>  daily count per event type, kept STAT_TTL
//
// An event is { at, email, type, ...detail } with a short, whitelisted detail
// (a ticker symbol, a model name, an export format). Never a filing's
// contents or figures: a PDF is recorded only as "pdf". api/premium.py writes
// the same shape directly (it runs in Python, outside this process).
//
// Tracking must never break the action being tracked, so every failure is
// swallowed: a lost event is better than a failed analysis.

const store = require("./store");

const PER_USER = 200;
const ALL = 500;
const STAT_TTL = 90 * 86_400;

//: Event types and the detail fields each may carry. Anything else is dropped.
const TYPES = {
  signin: ["method"],
  analysis: ["source", "ticker"],
  report: ["models", "mode", "ticker"],
  export: ["format", "ticker"],
  premium: ["model", "status"],
  plan_view: [],
};

const clean = (v) => (Array.isArray(v)
  ? v.slice(0, 12).map((x) => String(x).slice(0, 60))
  : String(v).slice(0, 60));

function build(email, type, detail = {}) {
  const fields = TYPES[type];
  if (!fields) return null;
  const ev = { at: new Date().toISOString(), email, type };
  for (const f of fields) if (detail[f] !== undefined && detail[f] !== null && detail[f] !== "") ev[f] = clean(detail[f]);
  return ev;
}

async function track(email, type, detail) {
  try {
    if (!store.configured() || !email) return null;
    const ev = build(email, type, detail);
    if (!ev) return null;
    const raw = JSON.stringify(ev);
    await Promise.all([
      store.push(`events:${email}`, raw, PER_USER),
      store.push("events:all", raw, ALL),
      store.set(`act:${email}`, ev.at),
      store.incr(`stat:${ev.at.slice(0, 10)}:${type}`, STAT_TTL),
    ]);
    return ev;
  } catch { return null; }
}

const parse = (rows) => rows.map((r) => { try { return JSON.parse(r); } catch { return null; } }).filter(Boolean);

async function recent(email, n = 100) {
  return parse(await store.range(email ? `events:${email}` : "events:all", n));
}

//: Per-type counts for the last `days` days, newest day first.
async function daily(days = 14) {
  const types = Object.keys(TYPES);
  const out = [];
  for (let i = 0; i < days; i++) {
    const day = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10);
    const vals = await store.mget(types.map((t) => `stat:${day}:${t}`));
    const row = { day };
    types.forEach((t, j) => { row[t] = parseInt(vals[j] || "0", 10) || 0; });
    out.push(row);
  }
  return out;
}

module.exports = { track, recent, daily, build, TYPES, PER_USER, ALL };
