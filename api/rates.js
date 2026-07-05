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

//: US 10Y proxy — average marketable interest rate on Treasury Notes.
async function usTreasury() {
  const url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/" +
    "v2/accounting/od/avg_interest_rates?filter=security_desc:eq:Treasury%20Notes" +
    "&sort=-record_date&page%5Bsize%5D=1";
  const r = await fetch(url, { signal: T(8000) });
  if (!r.ok) throw new Error(`fiscaldata ${r.status}`);
  const row = (await r.json()).data[0];
  return { rf: parseFloat(row.avg_interest_rate_amt) / 100,
           rfSource: `US TREASURY FISCALDATA · NOTES AVG ${row.record_date}` };
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

module.exports = async (req, res) => {
  const url = new URL(req.url || "/", "http://internal");
  const cc = String(url.searchParams.get("cc") || "US").toUpperCase();
  const m = MARKETS[cc];
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Access-Control-Allow-Origin", "*");
  if (!m) return res.status(400).json({ error: `unknown market '${cc}'` });
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
