// api/rates.js — country-correct market rates for the terminal & IB desk.
//
// GET /api/rates?cc=IN  ->  { cc, ccy, rf, rfSource, fx, fxDate }
//
// The terminal anchors every model (and the IB desk's auto-assumptions) to
// the selected country's cost of capital. This endpoint serves the live
// 10-year sovereign yield per market, stitched from multiple free sources:
//
//   1. US        — Treasury FiscalData API (keyless, official, daily-ish).
//   2. Others    — FRED (St. Louis Fed) OECD long-term government bond
//                  yields, series IRLTLT01<CC>M156N. Needs FRED_API_KEY —
//                  a free key from https://fred.stlouisfed.org/docs/api/api_key.html
//                  (set it in Vercel env vars). Without the key those
//                  markets fall back to the curated Damodaran baselines
//                  hardcoded in the front end (rf: null here signals that).
//   3. FX        — open.er-api.com (keyless) for the country currency per
//                  USD, so the UI can surface the FX context of a filing.
//
// Responses are CDN-cached hard (s-maxage 6h): sovereign yields are
// monthly/daily series, so per-visitor freshness buys nothing.

const MARKETS = {
  US: { ccy: "USD" },                                  // FiscalData (keyless)
  CN: { ccy: "CNY", fred: "IRLTLT01CNM156N" },
  JP: { ccy: "JPY", fred: "IRLTLT01JPM156N" },
  IN: { ccy: "INR", fred: "IRLTLT01INM156N" },
  HK: { ccy: "HKD" },                                  // no OECD series — baseline
  FR: { ccy: "EUR", fred: "IRLTLT01FRM156N" },
  GB: { ccy: "GBP", fred: "IRLTLT01GBM156N" },
  CA: { ccy: "CAD", fred: "IRLTLT01CAM156N" },
  SA: { ccy: "SAR" },                                  // no OECD series — baseline
  DE: { ccy: "EUR", fred: "IRLTLT01DEM156N" },
  CH: { ccy: "CHF", fred: "IRLTLT01CHM156N" },
  TW: { ccy: "TWD" },                                  // no OECD series — baseline
  AU: { ccy: "AUD", fred: "IRLTLT01AUM156N" },
  KR: { ccy: "KRW", fred: "IRLTLT01KRM156N" },
  NL: { ccy: "EUR", fred: "IRLTLT01NLM156N" },
};

const T = (ms) => AbortSignal.timeout(ms);

//: Parses one year's daily-treasury-rates CSV and returns the most recent
//  finite "10 Yr" reading, or null if the file has no usable row (e.g. an
//  empty first-week-of-January file before Treasury has published anything).
//  Exported for testing against an inline fixture rather than a live fetch.
function parseTenYearParYield(csvText) {
  const lines = csvText.split(/\r?\n/).filter((l) => l.trim().length);
  if (lines.length < 2) return null;
  //: Header cells are quoted ("10 Yr"); locate the column BY NAME, not by a
  //  fixed index — Treasury has reordered/added maturity columns before (the
  //  "1.5 Month" column is a recent addition), so a hardcoded index silently
  //  reads the wrong maturity the next time the layout shifts.
  const header = lines[0].split(",").map((h) => h.trim().replace(/^"|"$/g, ""));
  const col = header.indexOf("10 Yr");
  if (col < 0) return null;
  // Rows are newest-first; the first row with a finite value in that column
  // is the most recent published reading.
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
    const dateCell = cells[0];
    const raw = cells[col];
    if (!dateCell || raw === undefined || raw === "") continue;  // blank cell — market holiday etc.
    const pct = parseFloat(raw);
    if (!Number.isFinite(pct)) continue;
    //: Sanity-bound the parsed value — reject anything outside a plausible
    //  10Y yield range (0-20%) so a parsing/column mistake throws instead of
    //  silently returning a nonsense rf that every model would then use.
    if (pct < 0 || pct > 20) throw new Error(`10Y par yield out of range: ${pct}`);
    // dateCell is MM/DD/YYYY; reformat to ISO for rfSource.
    const [mm, dd, yyyy] = dateCell.split("/");
    if (!mm || !dd || !yyyy) continue;
    return { pct, iso: `${yyyy}-${mm}-${dd}` };
  }
  return null;
}

//: US 10Y — Treasury's own daily par yield curve, "10 Yr" column. Keyless CSV,
//  no API key needed. This replaced avg_interest_rates ("Treasury Notes"),
//  which is the weighted-average COUPON across all outstanding notes (a
//  backward-looking blend of issuance history), not the market yield a DCF's
//  risk-free rate is supposed to be — it read 3.345% against a real 10Y par
//  yield of 4.96% on the same day, a >150bp understatement baked into every
//  discount rate the product computed.
async function usTreasury() {
  const now = new Date();
  const year = now.getUTCFullYear();
  const fetchYear = async (y) => {
    const url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
      + `daily-treasury-rates.csv/${y}/all?type=daily_treasury_yield_curve&field_tdr_date_value=${y}`;
    const r = await fetch(url, { signal: T(8000) });
    if (!r.ok) throw new Error(`treasury csv ${y} ${r.status}`);
    return parseTenYearParYield(await r.text());
  };
  //: Early January: the current year's file can be empty or only a couple of
  //  rows deep (Treasury hasn't published this year's data yet), so fall back
  //  to the previous year's file rather than returning nothing for weeks.
  let hit = await fetchYear(year);
  if (!hit) hit = await fetchYear(year - 1);
  if (!hit) throw new Error("treasury par yield curve: no usable 10Yr row found");
  return { rf: hit.pct / 100,
           rfSource: `US TREASURY PAR YIELD CURVE · 10Y · ${hit.iso}` };
}

//: OECD long-term (10Y) government bond yield via FRED. % p.a. monthly.
async function fredYield(series) {
  const key = process.env.FRED_API_KEY;
  if (!key) return null;                       // no key -> baseline fallback
  const url = "https://api.stlouisfed.org/fred/series/observations?series_id=" +
    series + `&api_key=${key}&file_type=json&sort_order=desc&limit=1`;
  const r = await fetch(url, { signal: T(8000) });
  if (!r.ok) throw new Error(`fred ${r.status}`);
  const obs = (await r.json()).observations?.[0];
  if (!obs || obs.value === ".") return null;
  return { rf: parseFloat(obs.value) / 100,
           rfSource: `FRED/OECD 10Y GOVT YIELD · ${obs.date}` };
}

//: Country currency per 1 USD (keyless, ECB-style daily fix).
async function fxPerUsd(ccy) {
  if (ccy === "USD") return { fx: 1, fxDate: null };
  const r = await fetch("https://open.er-api.com/v6/latest/USD", { signal: T(8000) });
  if (!r.ok) throw new Error(`er-api ${r.status}`);
  const j = await r.json();
  return { fx: j.rates && j.rates[ccy] ? j.rates[ccy] : null,
           fxDate: j.time_last_update_utc || null };
}

const { clientIp, withinLimitLayered } = require("./_lib/net");

module.exports = async (req, res) => {
  const url = new URL(req.url || "/", "http://internal");
  const cc = String(url.searchParams.get("cc") || "US").toUpperCase();
  const m = MARKETS[cc];
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Access-Control-Allow-Origin", "*");
  if (!m) return res.status(400).json({ error: `unknown market '${cc}'` });
  // The response is shared/CDN-cached for 6h, so this only ever matters
  // before the cache is warm — but a flood of concurrent first-hits still
  // fans out to FRED/Treasury/er-api, so cap it per caller too. Layered
  // with a global backstop since clientIp() is only as trustworthy as
  // whatever's in front of this function — see the comment in _lib/net.js.
  // Same steady-state rate as before (~6.7/s) but windowed at 5s instead
  // of 60s, so a burst that exhausts the shared budget locks out other
  // callers for a few seconds, not up to a minute.
  if (!(await withinLimitLayered(`rates:rl:${clientIp(req)}`, 20, 60, "rates:rl:global", 35, 5))) {
    return res.status(429).json({ error: "rate limited" });
  }
  res.setHeader("Cache-Control", "public, s-maxage=21600, stale-while-revalidate=86400");

  // Yield + FX fetched in parallel; each failure degrades independently —
  // a null rf tells the front end to keep its curated baseline.
  const [yld, fx] = await Promise.all([
    (cc === "US" ? usTreasury() : m.fred ? fredYield(m.fred) : Promise.resolve(null))
      .catch(() => null),
    fxPerUsd(m.ccy).catch(() => ({ fx: null, fxDate: null })),
  ]);

  res.status(200).json({
    cc, ccy: m.ccy,
    rf: yld ? yld.rf : null,
    rfSource: yld ? yld.rfSource : null,
    fx: fx.fx, fxDate: fx.fxDate,
    fredConfigured: !!process.env.FRED_API_KEY,
  });
};

//: Pure helper exported for scripts/test_*.js — tested against an inline CSV
//  fixture rather than a live fetch.
module.exports._internals = { parseTenYearParYield };
