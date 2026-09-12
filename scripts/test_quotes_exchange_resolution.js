// Integration test for api/quotes.js's exchange-suffix resolution (India
// live-price fix) and the currency field it now returns. Runs the real
// handler in-process, against real Yahoo Finance — no mocked responses,
// since the whole point of the fix is real Indian tickers resolving
// correctly, which a synthetic response could hide a regression in.
//
//     node scripts/test_quotes_exchange_resolution.js

process.env.VERCEL_KV_REST_API_URL = "";   // no store configured -> net.js fails open

const assert = require("node:assert");
const quotes = require("../api/quotes.js");

function call(query) {
  const req = { url: "/api/quotes?" + query, headers: {} };
  const res = {
    headers: {}, code: 0, out: null,
    setHeader(k, v) { this.headers[k] = v; },
    status(c) { this.code = c; return this; },
    json(o) { this.out = o; return this; },
  };
  return Promise.resolve(quotes(req, res)).then(() => res);
}

let passed = 0, failed = 0;
function ok(cond, label, detail) {
  if (cond) { console.log(`  ✔ ${label}`); passed++; }
  else { console.error(`  ✘ ${label}${detail ? " — " + detail : ""}`); failed++; }
}

(async () => {
  console.log("· a bare Indian ticker resolves via NSE when cc=IN (the actual bug being fixed)");
  const r1 = await call("sym=RELIANCE&cc=IN");
  ok(r1.code === 200 && r1.out.ok, "RELIANCE + cc=IN succeeds", JSON.stringify(r1.out));
  if (r1.out.ok) {
    ok(r1.out.resolvedSymbol === "RELIANCE.NS", "resolved via .NS, not .BO or bare", r1.out.resolvedSymbol);
    ok(r1.out.currency === "INR", "currency reported as INR", r1.out.currency);
    ok(typeof r1.out.price === "number" && r1.out.price > 100, "plausible positive price", r1.out.price);
    ok(r1.out.symbol === "RELIANCE", "echoes the symbol the user actually typed", r1.out.symbol);
  }

  console.log("· the same bare ticker WITHOUT cc still fails today (proves this isn't just luck)");
  const r2 = await call("sym=RELIANCE");
  ok(r2.code === 502 && r2.out.ok === false, "bare RELIANCE with no cc still 502s", JSON.stringify(r2.out));

  console.log("· a non-Indian ticker looked up while India is selected is NOT mislabeled INR");
  const r3 = await call("sym=AAPL&cc=IN");
  ok(r3.code === 200 && r3.out.ok, "AAPL + cc=IN succeeds", JSON.stringify(r3.out));
  if (r3.out.ok) {
    ok(r3.out.resolvedSymbol === "AAPL", "falls through to the bare US symbol", r3.out.resolvedSymbol);
    ok(r3.out.currency === "USD", "currency correctly reported as USD, not INR", r3.out.currency);
  }

  console.log("· an already-suffixed symbol is used as-is (no double-suffixing)");
  const r4 = await call("sym=RELIANCE.NS&cc=IN");
  ok(r4.code === 200 && r4.out.ok && r4.out.resolvedSymbol === "RELIANCE.NS", "explicit .NS passes through unchanged");

  console.log("· the fixed-basket tape endpoint now reports currency per quote too");
  const r5 = await call("");
  ok(r5.code === 200 && Array.isArray(r5.out.quotes) && r5.out.quotes.length > 0, "basket still returns quotes");
  const gold = r5.out.quotes.find((q) => q.label === "GOLD");
  ok(!!gold && gold.currency === "USD", "GOLD quote carries currency: USD (COMEX futures)", JSON.stringify(gold));

  console.log(`\n${passed} passed · ${failed} failed`);
  if (failed > 0) process.exitCode = 1;
})();
