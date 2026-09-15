// Unit tests for the ticker-load data mapping (api/fundamentals.js).
//
//     node scripts/test_fundamentals.js
//
// Offline by design — every fixture below is a trimmed copy of the SHAPE SEC
// actually returns (verified against live companyfacts for AAPL, JPM and
// BRK-B), so this runs in CI without depending on SEC being reachable or on
// Apple's numbers staying the same.
//
// These cover the three things that are easy to get subtly wrong here and
// impossible to spot by eyeballing a live response:
//   1. share-class ticker spelling (SEC uses BRK-B, humans type BRK.B),
//   2. restatements — the same period reported twice by different filings,
//   3. period alignment when deriving FCF from two separate statements.
// Each of those produces a plausible-looking wrong number rather than an
// error, which is the failure mode this whole product exists to avoid.

const { _internals } = require("../api/fundamentals.js");
const { resolveTicker, pickInstant, pickAnnualSeries, detectReportingCurrency,
        benchmarkFor, monthlyReturns, MIN_BETA_OBSERVATIONS,
        ADR_LOCAL_LISTINGS, ADR_RATIO_CANDIDATES, ADR_RATIO_TOLERANCE,
        computeSplitAdjustment, SPLIT_ALLOTMENT_LAG_MS } = _internals;

let passed = 0, failed = 0;
function ok(cond, label, detail) {
  if (cond) { passed++; console.log(`  ✔ ${label}`); }
  else { failed++; console.log(`  ✘ ${label}${detail !== undefined ? " — " + detail : ""}`); }
}
const eq = (a, b, label) => ok(a === b, label, `got ${a}, want ${b}`);

/* ----------------------------- ticker spelling --------------------------- */
console.log("· Ticker resolution — SEC spells share classes with a hyphen, humans type a dot");
{
  const map = new Map([
    ["AAPL", { cik: "0000320193", title: "Apple Inc." }],
    ["BRK-B", { cik: "0001067983", title: "BERKSHIRE HATHAWAY INC" }],
    ["BF-B", { cik: "0000014693", title: "Brown-Forman Corporation" }],
  ]);
  eq(resolveTicker(map, "AAPL").symbol, "AAPL", "plain ticker resolves");
  eq(resolveTicker(map, "BRK.B").symbol, "BRK-B", "BRK.B (dot, as typed) resolves to BRK-B");
  eq(resolveTicker(map, "BRK-B").symbol, "BRK-B", "BRK-B (hyphen, as SEC writes it) still resolves");
  eq(resolveTicker(map, "BF.B").symbol, "BF-B", "BF.B resolves to BF-B");
  ok(resolveTicker(map, "RELIANCE") === null, "a non-registrant resolves to null, not a guess");
  ok(resolveTicker(map, "ZZZZ") === null, "an unknown ticker resolves to null");
  // The resolved symbol is what the price lookup uses downstream; returning
  // the user's spelling instead would 404 against Yahoo for all 543 of these.
  eq(resolveTicker(map, "BRK.B").cik, "0001067983", "resolved entry carries the right CIK");
}

/* ------------------------------ restatements ----------------------------- */
console.log("\n· Restatements — the same period filed twice must not be double-counted");
{
  // Real shape: an FY2025 10-K restates FY2024, so both rows carry fy:2025.
  // Keying on fy (the FILING's year) instead of the period's own end date is
  // the trap; these two rows describe DIFFERENT periods.
  const facts = {
    Revenues: {
      units: {
        USD: [
          { start: "2023-10-01", end: "2024-09-28", val: 391035000000, fy: 2024, form: "10-K", filed: "2024-11-01" },
          { start: "2023-10-01", end: "2024-09-28", val: 391035000000, fy: 2025, form: "10-K", filed: "2025-10-30" },
          { start: "2024-09-29", end: "2025-09-27", val: 416161000000, fy: 2025, form: "10-K", filed: "2025-10-30" },
        ],
      },
    },
  };
  const got = pickAnnualSeries(facts, ["Revenues"]);
  eq(got.series.length, 2, "two distinct periods, not three rows");
  eq(got.series[0].val, 391035000000, "older period first");
  eq(got.series[1].val, 416161000000, "newest period last");
}
{
  // When a period IS restated to a different value, the latest filing wins.
  const facts = {
    Revenues: {
      units: {
        USD: [
          { start: "2023-01-01", end: "2023-12-31", val: 100, fy: 2023, form: "10-K", filed: "2024-02-01" },
          { start: "2023-01-01", end: "2023-12-31", val: 111, fy: 2024, form: "10-K", filed: "2025-02-01" },
        ],
      },
    },
  };
  const got = pickAnnualSeries(facts, ["Revenues"]);
  eq(got.series.length, 1, "one period after collapsing the restatement");
  eq(got.series[0].val, 111, "the most recently FILED value wins");
}

/* --------------------------- period classification ----------------------- */
console.log("\n· Only annual periods enter the series — quarters must not leak in");
{
  const facts = {
    Revenues: {
      units: {
        USD: [
          { start: "2024-01-01", end: "2024-03-31", val: 90, fy: 2024, form: "10-Q", filed: "2024-04-20" },
          { start: "2024-01-01", end: "2024-06-30", val: 180, fy: 2024, form: "10-Q", filed: "2024-07-20" },
          { start: "2024-01-01", end: "2024-12-31", val: 400, fy: 2024, form: "10-K", filed: "2025-02-01" },
        ],
      },
    },
  };
  const got = pickAnnualSeries(facts, ["Revenues"]);
  eq(got.series.length, 1, "a 3-month and a 6-month period are excluded");
  eq(got.series[0].val, 400, "only the ~365-day period survives");
}
{
  // A 52/53-week retailer's fiscal year is 371 days; rejecting it as
  // "not a year" would silently drop a real annual figure.
  const facts = {
    Revenues: { units: { USD: [
      { start: "2024-01-28", end: "2025-02-01", val: 555, fy: 2025, form: "10-K", filed: "2025-03-20" },
    ] } },
  };
  eq(pickAnnualSeries(facts, ["Revenues"]).series[0].val, 555,
    "a 53-week (371-day) fiscal year still counts as annual");
}

/* ------------------------------ tag cascade ------------------------------ */
console.log("\n· Tag cascade — companies tag the same concept differently");
{
  const facts = {
    // The preferred tag exists but is empty; the cascade must fall through.
    RevenueFromContractWithCustomerExcludingAssessedTax: { units: { USD: [] } },
    SalesRevenueNet: { units: { USD: [
      { start: "2024-01-01", end: "2024-12-31", val: 777, fy: 2024, form: "10-K", filed: "2025-02-01" },
    ] } },
  };
  const got = pickAnnualSeries(facts, [
    "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]);
  eq(got.val || got.series[0].val, 777, "falls through an empty tag to a populated one");
  eq(got.tag, "SalesRevenueNet", "reports which tag actually supplied the figure");
}
{
  ok(pickAnnualSeries({}, ["Revenues"]) === null,
    "a company with none of the candidate tags yields null, never 0");
  ok(pickInstant({}, ["CashAndCashEquivalentsAtCarryingValue"]) === null,
    "a missing balance-sheet tag yields null, never 0");
}

/* ------------------------------ instants --------------------------------- */
console.log("\n· Balance-sheet instants take the latest period, restatement-aware");
{
  const facts = {
    CashAndCashEquivalentsAtCarryingValue: { units: { USD: [
      { end: "2023-09-30", val: 29965000000, form: "10-K", filed: "2023-11-03" },
      { end: "2025-09-27", val: 35935000000, form: "10-K", filed: "2025-10-30" },
      { end: "2024-09-28", val: 29943000000, form: "10-K", filed: "2024-11-01" },
    ] } },
  };
  const got = pickInstant(facts, ["CashAndCashEquivalentsAtCarryingValue"]);
  eq(got.value, 35935000000, "latest period end wins regardless of array order");
  eq(got.end, "2025-09-27", "reports the period it came from");
}
{
  // shares are a `shares` unit, not USD — picking units[0] blindly would
  // return the wrong series on a tag that carries both.
  const facts = {
    CommonStockSharesOutstanding: { units: { shares: [
      { end: "2025-10-17", val: 14608963000, form: "10-K", filed: "2025-10-30" },
    ] } },
  };
  const got = pickInstant(facts, ["CommonStockSharesOutstanding"]);
  eq(got.value, 14608963000, "share counts read from the shares unit");
  eq(got.unit, "shares", "unit is reported, not assumed USD");
}

/* ------------------------------ scale ------------------------------------ */
console.log("\n· Scale — EDGAR reports raw currency units and must NOT be rescaled");
{
  // src/pipeline/pdf_extractor.py normalises scraped figures to raw dollars
  // too ("a canonical dollar amount (not millions)"). If either side ever
  // starts reporting millions, every valuation silently moves by 1e6.
  const facts = {
    Revenues: { units: { USD: [
      { start: "2024-09-29", end: "2025-09-27", val: 416161000000, fy: 2025, form: "10-K", filed: "2025-10-30" },
    ] } },
  };
  const v = pickAnnualSeries(facts, ["Revenues"]).series[0].val;
  ok(v > 1e11, "Apple-scale revenue stays in raw dollars (~4.2e11), not millions", String(v));
  eq(v, 416161000000, "value passes through untouched");
}

/* --------------------------- tag migration / staleness ------------------- */
console.log("\n· Cascade prefers the FRESHEST tag, not the first one listed");
{
  // Infosys tagged `Revenue` until IFRS 15 (2018) and
  // `RevenueFromContractsWithCustomers` after. First-match order returned
  // FY2018's $10.9bn as if it were current — seven-year-old revenue in a
  // perfectly ordinary shape.
  const facts = {
    Revenue: { units: { USD: [
      { start: "2017-04-01", end: "2018-03-31", val: 10_939_000_000, form: "20-F", filed: "2018-06-01" },
    ] } },
    RevenueFromContractsWithCustomers: { units: { USD: [
      { start: "2024-04-01", end: "2025-03-31", val: 19_277_000_000, form: "20-F", filed: "2025-06-01" },
    ] } },
  };
  // Cascade deliberately lists the newer tag FIRST here, but the guarantee is
  // recency, so it must also hold with the stale tag listed first.
  const got = pickAnnualSeries(facts, ["Revenue", "RevenueFromContractsWithCustomers"]);
  eq(got.tag, "RevenueFromContractsWithCustomers", "abandoned tag loses to the current one");
  eq(got.series[0].val, 19_277_000_000, "returns FY2025 revenue, not FY2018");
}
{
  // Ties on fiscal year fall back to cascade order, so one stray datapoint in
  // a less-preferred tag cannot hijack a well-maintained one.
  const facts = {
    Revenues: { units: { USD: [
      { start: "2024-01-01", end: "2024-12-31", val: 500, form: "10-K", filed: "2025-02-01" },
    ] } },
    SalesRevenueNet: { units: { USD: [
      { start: "2024-01-01", end: "2024-12-31", val: 999, form: "10-K", filed: "2025-02-01" },
    ] } },
  };
  eq(pickAnnualSeries(facts, ["Revenues", "SalesRevenueNet"]).val
     || pickAnnualSeries(facts, ["Revenues", "SalesRevenueNet"]).series[0].val, 500,
    "same-year tie keeps the preferred tag");
}

/* ------------------------------ currency -------------------------------- */
console.log("\n· One reporting currency, enforced as a hard constraint");
{
  // Wipro: RevenueFromContractsWithCustomers is INR-only, Revenue has both.
  // Treating the INR figure as USD produced $890bn of revenue for a company
  // that earns about $10bn — an 82x error that looks like a big normal number.
  const facts = {
    RevenueFromContractsWithCustomers: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 890_880_000_000, form: "20-F", filed: "2025-06-01" },
    ] } },
    Revenue: { units: {
      INR: [{ start: "2024-04-01", end: "2025-03-31", val: 890_880_000_000, form: "20-F", filed: "2025-06-01" }],
      USD: [{ start: "2024-04-01", end: "2025-03-31", val: 10_430_000_000, form: "20-F", filed: "2025-06-01" }],
    } },
  };
  const tags = ["RevenueFromContractsWithCustomers", "Revenue"];
  eq(detectReportingCurrency(facts, tags), "USD", "USD is preferred when the filer offers it");
  const got = pickAnnualSeries(facts, tags, 6, "USD");
  eq(got.unit, "USD", "picked series is in the pinned currency");
  eq(got.series[0].val, 10_430_000_000, "returns the USD figure, not the INR one");
  eq(got.tag, "Revenue", "skips the INR-only tag entirely rather than falling back to its unit");
}
{
  // An INR-only filer: the currency stands, and nothing is silently converted.
  const facts = { Revenue: { units: { INR: [
    { start: "2024-04-01", end: "2025-03-31", val: 890_880_000_000, form: "20-F", filed: "2025-06-01" },
  ] } } };
  eq(detectReportingCurrency(facts, ["Revenue"]), "INR", "a single-currency filer reports its own");
  eq(pickAnnualSeries(facts, ["Revenue"], 6, "INR").series[0].val, 890_880_000_000,
    "INR figures pass through when INR is the pinned currency");
  ok(pickAnnualSeries(facts, ["Revenue"], 6, "USD") === null,
    "pinning USD on an INR-only filer yields NOTHING, never a mislabelled number");
}
{
  ok(detectReportingCurrency({}, ["Revenue"]) === null, "no revenue tags -> no currency claim");
}

/* ----------------------------- per-share units --------------------------- */
console.log("\n· Per-share amounts are denominated <CCY>/shares, not <CCY>");
{
  // Dividends per share are tagged USD/shares. Matching the pinned currency
  // exactly and nothing else silently dropped every dividend on every filer.
  const facts = {
    CommonStockDividendsPerShareDeclared: { units: { "USD/shares": [
      { start: "2024-09-29", end: "2025-09-27", val: 1.02, form: "10-K", filed: "2025-10-30" },
      { start: "2026-03-29", end: "2026-06-27", val: 0.27, form: "10-Q", filed: "2026-07-31" },
    ] } },
  };
  const got = pickAnnualSeries(facts, ["CommonStockDividendsPerShareDeclared"], 6, "USD");
  ok(got !== null, "a USD/shares tag is found when USD is the pinned currency");
  eq(got.unit, "USD/shares", "the per-share unit is reported as such");
  eq(got.series[0].val, 1.02, "annual declaration wins over the quarterly one (1.02, not 0.27)");
  eq(got.series.length, 1, "the quarterly row is excluded by the duration filter");
}
{
  // ...but the pinning must still bite: a rupee-per-share amount is not a
  // dollar-per-share amount just because both end in "/shares".
  const facts = {
    CommonStockDividendsPerShareDeclared: { units: { "INR/shares": [
      { start: "2024-04-01", end: "2025-03-31", val: 11.0, form: "20-F", filed: "2025-06-01" },
    ] } },
  };
  ok(pickAnnualSeries(facts, ["CommonStockDividendsPerShareDeclared"], 6, "USD") === null,
    "INR/shares is still rejected when USD is pinned");
  eq(pickAnnualSeries(facts, ["CommonStockDividendsPerShareDeclared"], 6, "INR").series[0].val, 11.0,
    "INR/shares is accepted when INR is pinned");
}

/* ------------------------------- ADR ratios ------------------------------- */
console.log("\n· Depositary ratios: derived and verified, never assumed");
{
  // Only listings whose ratio has been reconciled against live quotes belong
  // in the map; an unreconcilable entry is refused at request time anyway.
  const known = Object.keys(ADR_LOCAL_LISTINGS);
  ok(known.includes("HDB") && known.includes("IBN") && known.includes("INFY"),
    "the verified Indian ADRs are mapped to their home listings");
  eq(ADR_LOCAL_LISTINGS.HDB, "HDFCBANK.NS", "HDB maps to its NSE listing");
  eq(ADR_LOCAL_LISTINGS.WIT, "WIPRO.NS", "the ADR ticker is NOT assumed to match the local one");
  ok(!known.includes("AAPL"), "an ordinary US listing is not treated as a depositary receipt");
}
{
  // Tolerance has to admit real-world noise (non-simultaneous closes, FX
  // timing: measured 0.1-3.1%) while still rejecting a genuinely wrong ratio.
  // Adjacent candidates never sit closer than 25% apart, so 8% cannot
  // accidentally snap one onto its neighbour.
  ok(ADR_RATIO_TOLERANCE >= 0.03 && ADR_RATIO_TOLERANCE <= 0.12,
    "tolerance admits quote noise without reaching the next candidate",
    String(ADR_RATIO_TOLERANCE));
  const sorted = [...ADR_RATIO_CANDIDATES].sort((a, b) => a - b);
  let tightest = Infinity;
  for (let i = 1; i < sorted.length; i++) {
    tightest = Math.min(tightest, (sorted[i] - sorted[i - 1]) / sorted[i]);
  }
  ok(tightest > ADR_RATIO_TOLERANCE * 2,
    "no two candidate ratios are close enough for the tolerance to confuse them",
    `tightest gap ${(tightest * 100).toFixed(0)}%`);
}

/* --------------------- share counts: fragments and staleness -------------- */
console.log("\n· Share counts: a tranche row is not a total");
{
  // Wipro has no NumberOfSharesOutstanding, so the cascade reaches
  // NumberOfSharesIssuedAndFullyPaid — which carries a 1,274,805 issuance
  // tranche dated one day AFTER... i.e. whichever row happens to be latest
  // decides between the real count and one 4,000x too small.
  const facts = {
    NumberOfSharesIssuedAndFullyPaid: { units: { shares: [
      { end: "2024-12-04", val: 5_232_094_402, form: "20-F", filed: "2025-05-22" },
      { end: "2024-12-05", val: 1_274_805, form: "20-F", filed: "2025-05-22" },
    ] } },
  };
  eq(pickInstant(facts, ["NumberOfSharesIssuedAndFullyPaid"], null, 0.01).value,
    5_232_094_402, "the tranche row is skipped and the real total is returned");
  // Without the guard the bug reproduces — proving the guard is load-bearing.
  eq(pickInstant(facts, ["NumberOfSharesIssuedAndFullyPaid"]).value, 1_274_805,
    "unguarded, the later tranche row wins (this is the bug)");
}
{
  // A guard that eats real data is worse than none: an ordinary series with
  // no fragments must be untouched, including a genuine large buyback.
  const facts = {
    CommonStockSharesOutstanding: { units: { shares: [
      { end: "2024-09-28", val: 15_408_000_000, form: "10-K", filed: "2024-11-01" },
      { end: "2025-09-27", val: 14_608_963_000, form: "10-K", filed: "2025-10-30" },
    ] } },
  };
  eq(pickInstant(facts, ["CommonStockSharesOutstanding"], null, 0.01).value, 14_608_963_000,
    "a 5% buyback is still a total and is kept");
}

/* ------------------------ share-count freshness --------------------------- *
 * Fixtures below are real `events.splits` payloads pulled live from Yahoo's
 * chart endpoint (verified 2026-09-15) for the tickers that motivated this:
 * Wipro's 1:1 bonus issue (Dec 2024) and HDFC Bank's 1:1 bonus (Aug 2025)
 * each doubled shares outstanding after EDGAR's on-file count — confirmed
 * against each company's real post-bonus share count via public reporting,
 * not assumed.                                                              */
console.log("\n· Share-count freshness: corporate actions since the filing date are applied, not just flagged");
{
  // Wipro: bonus record date 2024-12-03, one day BEFORE EDGAR's own as-of
  // date of 2024-12-04 — yet the filed count (5,232,094,402) was still the
  // pre-bonus figure. A same-side date comparison would miss this entirely;
  // the allotment-lag buffer is what catches it.
  const wiproSplits = {
    "1733197500": { date: 1733197500, numerator: 2, denominator: 1, splitRatio: "2:1" },
  };
  const adj = computeSplitAdjustment(wiproSplits, "2024-12-04");
  ok(adj !== null && adj.events.length === 1, "the bonus is found despite filing 1 day after the record date");
  eq(adj.ratio, 2, "1:1 bonus doubles the multiplier");
  const adjusted = Math.round(5_232_094_402 * adj.ratio);
  ok(Math.abs(adjusted - 10_472_085_808) / 10_472_085_808 < 0.01,
    "adjusted Wipro count lands within 1% of its real post-bonus count", String(adjusted));
}
{
  // HDFC Bank: two splits on file, only the second (Aug 2025) postdates the
  // Mar-2025 filing. Only that one may apply — applying both would double
  // the count a second time on top of an already-correct earlier split.
  const hdfcSplits = {
    "1567276200": { date: 1568864700, numerator: 2, denominator: 1, splitRatio: "2:1" }, // 2019, must be ignored
    "1753986600": { date: 1756179900, numerator: 2, denominator: 1, splitRatio: "2:1" }, // 2025, must apply
  };
  const adj = computeSplitAdjustment(hdfcSplits, "2025-03-31");
  eq(adj.events.length, 1, "only the split after the filing date is applied, not the 2019 one too");
  eq(adj.ratio, 2, "the 2019 split does not compound into the multiplier");
  const adjustedOrdinary = Math.round(7_652_221_674 * adj.ratio);
  ok(Math.abs(adjustedOrdinary - 15_354_079_522) / 15_354_079_522 < 0.01,
    "adjusted HDFC Bank ordinary count lands within 1% of its real post-bonus count",
    String(adjustedOrdinary));
}
{
  // Dr Reddy's: split (Oct 2024) predates its own filing's as-of date
  // (Mar 2025) by 5 months, well outside the allotment-lag buffer — the
  // filed count already reflects it, so applying it again would be wrong.
  const rdySplits = {
    "1727721000": { date: 1730087100, numerator: 5, denominator: 1, splitRatio: "5:1" },
  };
  const adj = computeSplitAdjustment(rdySplits, "2025-03-31");
  eq(adj.events.length, 0, "a split well before the filing date is not re-applied");
  eq(adj.ratio, 1, "no adjustment when the filing already postdates the split");
}
{
  // No corporate action at all — the common case (Infosys, AAPL) — must be
  // silent: ratio 1, no events, nothing for the caller to narrate.
  const adj = computeSplitAdjustment({}, "2025-03-31");
  eq(adj.ratio, 1, "an empty split history yields no adjustment");
  eq(adj.events.length, 0, "and reports no events to describe");
}
{
  // Malformed inputs must degrade safely rather than throw or silently
  // fabricate a ratio.
  ok(computeSplitAdjustment({ a: {} }, "2025-03-31").ratio === 1,
    "a split entry missing numerator/denominator is skipped, not NaN-multiplied");
  ok(computeSplitAdjustment(null, "2025-03-31").ratio === 1,
    "no split data at all yields no adjustment, not a crash");
  ok(computeSplitAdjustment({}, "not-a-date") === null,
    "an unparseable as-of date returns null rather than guessing");
  ok(computeSplitAdjustment({}, null) === null,
    "a missing as-of date returns null rather than guessing");
}
{
  // A reverse split must shrink the count, not just be ignored for having
  // numerator < denominator.
  const reverseSplit = { "1": { date: Math.floor(Date.now() / 1000) - 86400,
                                 numerator: 1, denominator: 5, splitRatio: "1:5" } };
  const yesterday = new Date(Date.now() - 2 * 86_400_000).toISOString().slice(0, 10);
  const adj = computeSplitAdjustment(reverseSplit, yesterday);
  eq(adj.ratio, 0.2, "a 1:5 reverse split shrinks the multiplier to a fifth");
}
{
  ok(SPLIT_ALLOTMENT_LAG_MS >= 7 * 86_400_000 && SPLIT_ALLOTMENT_LAG_MS <= 21 * 86_400_000,
    "the allotment-lag buffer is on the order of the real record-to-credit gap (about 2 weeks)",
    String(SPLIT_ALLOTMENT_LAG_MS / 86_400_000) + " days");
}

/* -------------------------------- beta ----------------------------------- */
console.log("\n· Beta is computed, benchmarked per market, and refuses thin samples");
{
  eq(benchmarkFor("AAPL").symbol, "^GSPC", "US listings benchmark against the S&P 500");
  eq(benchmarkFor("RELIANCE.NS").symbol, "^NSEI", "NSE listings benchmark against the NIFTY 50");
  eq(benchmarkFor("RELIANCE.BO").symbol, "^BSESN", "BSE listings benchmark against the SENSEX");
  // Reporting a Nifty-relative beta as an S&P-relative one would be a
  // different number presented under the wrong name.
  ok(benchmarkFor("TCS.NS").name !== benchmarkFor("AAPL").name,
    "an Indian listing never silently inherits the US benchmark");
}
{
  // Returns are joined on calendar month, not by array position: a series
  // missing one month would otherwise pair March against April forever after.
  const mk = (startUnix, vals) => vals.map((c, i) => ({ t: startUnix + i * 2_678_400, c }));
  const r = monthlyReturns(mk(1_600_000_000, [100, 110, 121]));
  eq(r.size, 2, "n closes yield n-1 returns");
  const vals = [...r.values()];
  ok(Math.abs(vals[0] - 0.10) < 1e-9 && Math.abs(vals[1] - 0.10) < 1e-9,
    "simple returns computed correctly");
}
{
  ok(MIN_BETA_OBSERVATIONS >= 24,
    "beta needs at least two years of monthly points before it means anything");
}

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
