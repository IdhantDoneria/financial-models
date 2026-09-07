// api/quotes.js — live market-ticker feed for the FINMODELS terminal.
//
// Why a serverless function: the browser cannot fetch Yahoo Finance / Stooq
// directly (those hosts send no CORS headers), and no keyless, CORS-open
// source covers world indices + energy + metals. So the browser calls this
// SAME-ORIGIN endpoint, which fetches Yahoo server-side (no key, no CORS
// concern) and returns a compact JSON array. The response is CDN-cached
// (`s-maxage`) so Yahoo is hit at most ~once per minute no matter how many
// visitors are watching the tape.
//
// No dependencies: uses the Node 18+ global fetch that Vercel provides.

const SYMBOLS = [
  // energy · metals · crypto — the headline commodities/crypto leaders
  { sym: "BTC-USD",   label: "BITCOIN",  money: true },
  { sym: "ETH-USD",   label: "ETHEREUM", money: true },
  { sym: "CL=F",      label: "WTI CRUDE", money: true },
  { sym: "BZ=F",      label: "BRENT",     money: true },
  { sym: "GC=F",      label: "GOLD",      money: true },
  { sym: "SI=F",      label: "SILVER",    money: true },
  // 15 of the world's most valued equity indices
  { sym: "^GSPC",     label: "S&P 500" },
  { sym: "^IXIC",     label: "NASDAQ" },
  { sym: "^DJI",      label: "DOW JONES" },
  { sym: "^FTSE",     label: "FTSE 100" },
  { sym: "^GDAXI",    label: "DAX" },
  { sym: "^FCHI",     label: "CAC 40" },
  { sym: "^STOXX50E", label: "EURO STOXX 50" },
  { sym: "^N225",     label: "NIKKEI 225" },
  { sym: "^HSI",      label: "HANG SENG" },
  { sym: "000001.SS", label: "SHANGHAI" },
  { sym: "^NSEI",     label: "NIFTY 50" },
  { sym: "^GSPTSE",   label: "TSX" },
  { sym: "^AXJO",     label: "ASX 200" },
  { sym: "^KS11",     label: "KOSPI" },
  { sym: "^TWII",     label: "TAIWAN" },
];

async function quote(s) {
  const url = "https://query1.finance.yahoo.com/v8/finance/chart/" +
    encodeURIComponent(s.sym) + "?range=1d&interval=1d";
  try {
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; FinModelsTerminal/1.0)" },
    });
    if (!r.ok) return null;
    const j = await r.json();
    const m = j && j.chart && j.chart.result && j.chart.result[0] && j.chart.result[0].meta;
    if (!m || typeof m.regularMarketPrice !== "number") return null;
    const price = m.regularMarketPrice;
    const prev = (typeof m.chartPreviousClose === "number" ? m.chartPreviousClose
                 : typeof m.previousClose === "number" ? m.previousClose : price);
    const pct = prev ? ((price - prev) / prev) * 100 : 0;
    return { label: s.label, money: !!s.money, price, pct };
  } catch {
    return null;
  }
}

//: Ad-hoc single-symbol lookup — used by the Reverse DCF model's optional
//  "live price" mode (a user-typed ticker, not the fixed tape basket above).
//  Same Yahoo Finance chart endpoint, same server-side-only fetch (the
//  browser still can't reach Yahoo directly), just parameterised by
//  `?sym=` instead of the hardcoded SYMBOLS list.
const SYM_RE = /^[A-Za-z0-9.\-^=]{1,16}$/;   // conservative allowlist — no path/query injection

module.exports = async (req, res) => {
  const url = new URL(req.url || "/", "http://internal");
  const sym = url.searchParams.get("sym");

  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Access-Control-Allow-Origin", "*");

  if (sym) {
    // Single ad-hoc quote: no CDN caching (a user just typed this ticker;
    // stale-serving a 30s-old *different* symbol's edge-cache entry would
    // be a correctness bug, not just a freshness one), so no-store instead.
    res.setHeader("Cache-Control", "no-store");
    if (!SYM_RE.test(sym)) return res.status(400).json({ ok: false, error: "invalid symbol" });
    const q = await quote({ sym, label: sym, money: true });
    if (!q) return res.status(502).json({ ok: false, error: `could not fetch a quote for ${sym}` });
    return res.status(200).json({ ok: true, symbol: sym, price: q.price, pct: q.pct, ts: Date.now() });
  }

  const settled = await Promise.all(SYMBOLS.map(quote));
  const quotes = settled.filter(Boolean);
  // Serve stale instantly while revalidating in the background — the tape is
  // never blocked on a cold Yahoo fetch. Kept short so the shared edge cache
  // can't hold a quote for long even in the worst case (revalidation failing
  // repeatedly); the client itself polls this endpoint every 60s regardless.
  res.setHeader("Cache-Control", "public, s-maxage=30, stale-while-revalidate=90");
  res.status(200).json({ ts: Date.now(), quotes });
};
