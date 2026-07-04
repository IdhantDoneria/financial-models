// api/_lib/store.js — persistence layer for server-side auth.
//
// Production: Upstash Redis over its REST API (the Vercel Marketplace
// "Upstash for Redis" integration injects KV_REST_API_URL/KV_REST_API_TOKEN;
// a direct Upstash database uses the UPSTASH_REDIS_REST_* names — both are
// honoured). Everything auth stores is a string value under a namespaced
// key; TTLs enforce OTP expiry (10 min), rate-limit windows and session
// lifetime (30 days) server-side.
//
// Local testing: AUTH_DEV_MEMORY=1 swaps in an in-process Map with the same
// interface. That mode is for the test harness only — serverless
// invocations don't share a process, so it can never back production.

const URL_ = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
const TOKEN = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
const DEV = process.env.AUTH_DEV_MEMORY === "1";

const mem = new Map(); // key -> { v, exp }
function memGet(key) {
  const e = mem.get(key);
  if (!e) return null;
  if (e.exp && Date.now() > e.exp) { mem.delete(key); return null; }
  return e.v;
}

async function redis(...cmd) {
  const r = await fetch(URL_, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  });
  if (!r.ok) throw new Error(`store unreachable (${r.status})`);
  const j = await r.json();
  if (j.error) throw new Error(`store error: ${j.error}`);
  return j.result;
}

module.exports = {
  configured: () => DEV || !!(URL_ && TOKEN),
  mode: () => (DEV ? "memory-dev" : URL_ && TOKEN ? "redis" : "unconfigured"),

  async get(key) {
    if (DEV) return memGet(key);
    return redis("GET", key);
  },
  async set(key, value) {
    if (DEV) { mem.set(key, { v: value, exp: 0 }); return; }
    await redis("SET", key, value);
  },
  async setex(key, ttlSec, value) {
    if (DEV) { mem.set(key, { v: value, exp: Date.now() + ttlSec * 1000 }); return; }
    await redis("SET", key, value, "EX", String(ttlSec));
  },
  async del(key) {
    if (DEV) { mem.delete(key); return; }
    await redis("DEL", key);
  },
  /** INCR, with an optional TTL started on first increment (rate-limit
   *  counters). Without a TTL it's a permanent counter (founder slots). */
  async incr(key, ttlSec) {
    if (DEV) {
      const cur = parseInt(memGet(key) || "0", 10) + 1;
      const e = mem.get(key);
      mem.set(key, { v: String(cur),
        exp: e && e.exp ? e.exp : ttlSec ? Date.now() + ttlSec * 1000 : 0 });
      return cur;
    }
    const n = await redis("INCR", key);
    if (n === 1 && ttlSec) await redis("EXPIRE", key, String(ttlSec));
    return n;
  },

  /** Set membership — the user registry (users:index) for the admin desk. */
  async sadd(key, member) {
    if (DEV) {
      const cur = mem.get(key);
      const set = cur && cur.v instanceof Set ? cur.v : new Set();
      set.add(member);
      mem.set(key, { v: set, exp: 0 });
      return;
    }
    await redis("SADD", key, member);
  },
  async smembers(key) {
    if (DEV) {
      const cur = mem.get(key);
      return cur && cur.v instanceof Set ? [...cur.v] : [];
    }
    return (await redis("SMEMBERS", key)) || [];
  },
  /** Batched GET — one round-trip for the admin user table. */
  async mget(keys) {
    if (!keys.length) return [];
    if (DEV) return keys.map((k) => memGet(k));
    return (await redis("MGET", ...keys)) || keys.map(() => null);
  },
};
