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
        computeSplitAdjustment, SPLIT_ALLOTMENT_LAG_MS,
        annualizedVol, regressionStats,
        pickLeaseLiability, debtTagIncludesLeases, NET_INTEREST_TAGS,
        interestExpenseFromRow, seriesAlignedTo, ifrsInterestClassification } = _internals;

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
  ok(computeSplitAdjustment({}, "not-a-date") === null,
    "an unparseable as-of date returns null rather than guessing");
  ok(computeSplitAdjustment({}, null) === null,
    "a missing as-of date returns null rather than guessing");
}
{
  // null means the fetch FAILED (unknown), which is a different thing from
  // {} (fetched fine, genuinely no splits) -- collapsing the two would let a
  // network hiccup silently present an unverified share count as checked.
  // Regression case: an earlier version of this function conflated them,
  // which broke the handler's "could not check" disclosure entirely (it
  // could never fire because the collapsed case always looked like success).
  ok(computeSplitAdjustment(null, "2025-03-31") === null,
    "a failed split lookup (null) propagates as null, not as \"no splits found\"");
  ok(computeSplitAdjustment({}, "2025-03-31") !== null,
    "a successful lookup that found nothing ({}) is NOT the same as a failed one");
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
{
  // A per-share dividend has the OPPOSITE exposure to a split from the share
  // count -- more shares now split the same payout -- so restating a
  // pre-bonus dividend uses the SAME ratio computeSplitAdjustment returns,
  // applied as a division. Real case: HDFC Bank disclosed $0.26/ADS for the
  // period ending 2025-03-31, five months before its Aug-2025 1:1 bonus;
  // left unadjusted this doubles the apparent yield against the (correctly
  // split-adjusted) current price, and feeds the Gordon Growth Model
  // (assumptions.py) as `dividend`, which scales linearly with it.
  const hdfcSplits = {
    "1753986600": { date: 1756179900, numerator: 2, denominator: 1, splitRatio: "2:1" },
  };
  const adj = computeSplitAdjustment(hdfcSplits, "2025-03-31");
  eq(adj.events.length, 1, "the bonus postdates the dividend's own period end");
  const restated = 0.26 / adj.ratio;
  ok(Math.abs(restated - 0.13) < 1e-9,
    "dividing (not multiplying) restates the dividend to a comparable per-current-share basis",
    String(restated));
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

/* --------------------- realised volatility & correlation ------------------ *
 * regressionStats() is the pure core of computeBeta(), split out precisely so
 * it can be checked here against a fixture whose beta/vol/correlation were
 * computed independently (a plain seeded synthetic series, verified with a
 * standalone script, not by calling the function under test) rather than
 * only asserting self-consistency.
 *
 * Downstream models (options pricing, Heston, VaR, MPT) fell back to fixed
 * guesses (25% stock vol, 18% market vol, 0.6 correlation) because these
 * numbers, despite being computed from the same regression as beta, were
 * never returned. This is the regression test for that gap.                  */
console.log("\n· Realised volatility & correlation: same regression inputs as beta, independently checked");
{
  // 30 months of a market series (xs) and a stock series (ys) generated from
  // ys = 1.3*xs + idiosyncratic noise, so a real relationship exists between
  // them. Expected beta/vol/correlation were computed with a standalone
  // script using textbook sample-variance / population-covariance formulas,
  // not by calling regressionStats() itself.
  const xs = [0.011585, 0.038195, 0.025246, 0.006689, -0.031075, -0.012011, 0.021832, -0.006049,
              -0.026634, 0.032986, 0.001097, 0.032326, 0.026288, 0.029401, -0.008038, -0.030521,
              0.005397, -0.013289, -0.034779, 0.009412, 0.021161, 0.030023, 0.031875, 0.038424,
              0.032717, -0.033592, -0.032946, 0.039967, -0.008962, 0.023651];
  const ys = [0.017042, 0.069525, 0.051764, -0.026122, -0.067766, 0.034089, 0.04552, -0.032231,
              0.005321, 0.076857, -0.020456, -0.003427, 0.080492, -0.000274, 0.004506, -0.062773,
              0.033853, -0.015614, -0.037485, 0.027382, 0.062871, 0.012029, 0.034229, 0.013899,
              0.021522, -0.044798, -0.077421, 0.026048, -0.000224, 0.039213];
  const got = regressionStats(xs, ys);
  ok(got !== null, "30 aligned observations clears the 24-month floor");
  eq(got.observations, 30, "observation count matches the input length");
  ok(Math.abs(got.beta - 1.2486033641448067) < 1e-9, "beta matches the independently-computed value", String(got.beta));
  ok(Math.abs(got.stockVol - 0.14592011294086862) < 1e-9,
    "stock (ys) annualised vol matches the independently-computed value", String(got.stockVol));
  ok(Math.abs(got.marketVol - 0.08759191030541419) < 1e-9,
    "market (xs) annualised vol matches the independently-computed value", String(got.marketVol));
  ok(Math.abs(got.correlation - 0.7495029415412352) < 1e-9,
    "correlation matches the independently-computed value", String(got.correlation));
}
{
  // TSLA-shape sanity check per the task brief: vol 58.1%, corr 0.46 against
  // a 15.4%-vol S&P 500 — high idiosyncratic noise relative to the market
  // factor should still clear the (0, 3] / [-1, 1] guards and come back
  // non-null, not be rejected as "too extreme".
  const n = 30;
  const xs = [], ys = [];
  let seed = 7;
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
  for (let i = 0; i < n; i++) {
    const zm = (rnd() - 0.5) * 2;
    const rm = 0.15 / 12 + (0.154 / Math.sqrt(12)) * zm;
    const zi = (rnd() - 0.5) * 2;
    const rs = 0.9 * rm + 0.16 * zi;         // large idiosyncratic term -> low corr, high vol
    xs.push(rm); ys.push(rs);
  }
  const got = regressionStats(xs, ys);
  ok(got !== null, "a high-idiosyncratic-vol series still produces a result");
  ok(got.stockVol === null || (got.stockVol > 0 && got.stockVol <= 3),
    "stock vol, if returned, is within the (0, 3] guard");
}

console.log("\n· Realised volatility & correlation: null/guard cases");
{
  const short = Array.from({ length: MIN_BETA_OBSERVATIONS - 1 }, (_, i) => 0.01 * (i % 3 - 1));
  ok(regressionStats(short, short) === null,
    "fewer than MIN_BETA_OBSERVATIONS pairs returns null, not a thin-sample estimate");
}
{
  ok(regressionStats([0.01, 0.02], [0.01, 0.02, 0.03]) === null,
    "mismatched-length arrays return null rather than silently misaligning");
}
{
  // A perfectly flat market (zero variance) has no beta and therefore no
  // vol/correlation either — the same "flat market has no beta" guard.
  const flat = new Array(MIN_BETA_OBSERVATIONS).fill(0);
  const stock = Array.from({ length: MIN_BETA_OBSERVATIONS }, (_, i) => 0.01 * (i % 2));
  ok(regressionStats(flat, stock) === null, "a flat (zero-variance) market series returns null");
}
{
  // A beta outside [-3, 5] is rejected as a data artifact — vol/correlation
  // must come back null too, not be split off and reported anyway.
  const xs = Array.from({ length: MIN_BETA_OBSERVATIONS }, (_, i) => (i % 2 === 0 ? 0.001 : -0.001));
  const ys = xs.map((v) => v * 10);   // beta = 10, well outside the guard
  ok(regressionStats(xs, ys) === null,
    "an implausible beta (10) rejects the whole result, including vol/correlation");
}
{
  ok(annualizedVol([0.01]) === null, "a single observation cannot yield a standard deviation");
  ok(annualizedVol([]) === null, "an empty series returns null, not NaN");
  const v = annualizedVol([0.02, -0.01, 0.03, 0.00]);
  ok(Number.isFinite(v) && v > 0, "a normal short series yields a finite positive vol");
}

/* --------------------------- lease liabilities ---------------------------- */
console.log("\n· Lease liabilities: total-tag preference, current+noncurrent sum, double-count guard");
{
  // A filer that tags a total AND the split must use the total, not sum on
  // top of it (which would double the figure).
  const facts = {
    OperatingLeaseLiability: { units: { USD: [
      { end: "2025-09-27", val: 9_150_000_000, form: "10-K", filed: "2025-11-01" },
    ] } },
    OperatingLeaseLiabilityCurrent: { units: { USD: [
      { end: "2025-09-27", val: 1_500_000_000, form: "10-K", filed: "2025-11-01" },
    ] } },
    OperatingLeaseLiabilityNoncurrent: { units: { USD: [
      { end: "2025-09-27", val: 7_650_000_000, form: "10-K", filed: "2025-11-01" },
    ] } },
  };
  const got = pickLeaseLiability(facts, ["OperatingLeaseLiability"],
    ["OperatingLeaseLiabilityCurrent"], ["OperatingLeaseLiabilityNoncurrent"]);
  eq(got.value, 9_150_000_000, "the total tag wins over summing current+noncurrent");
  eq(got.source, "total", "source is reported as 'total'");
}
{
  // SBUX-shape: no total tag, only current + noncurrent — must sum, not
  // report only one half.
  const facts = {
    OperatingLeaseLiabilityCurrent: { units: { USD: [
      { end: "2025-09-27", val: 1_100_000_000, form: "10-K", filed: "2025-11-01" },
    ] } },
    OperatingLeaseLiabilityNoncurrent: { units: { USD: [
      { end: "2025-09-27", val: 8_050_000_000, form: "10-K", filed: "2025-11-01" },
    ] } },
  };
  const got = pickLeaseLiability(facts, ["OperatingLeaseLiability"],
    ["OperatingLeaseLiabilityCurrent"], ["OperatingLeaseLiabilityNoncurrent"]);
  eq(got.value, 9_150_000_000, "current+noncurrent sums to the real total when no total tag exists");
  eq(got.source, "sum", "source is reported as 'sum'");
}
{
  // AAPL-shape: only a noncurrent tag (no current lease-liability tag at
  // all) — accepted, not refused, but must say so via its `source`.
  const facts = {
    OperatingLeaseLiabilityNoncurrent: { units: { USD: [
      { end: "2025-09-27", val: 10_912_000_000, form: "10-K", filed: "2025-10-30" },
    ] } },
  };
  const got = pickLeaseLiability(facts, ["OperatingLeaseLiability"],
    ["OperatingLeaseLiabilityCurrent"], ["OperatingLeaseLiabilityNoncurrent"]);
  eq(got.value, 10_912_000_000, "noncurrent-only is accepted as a caveated figure");
  eq(got.source, "noncurrent-only", "source flags that only the noncurrent portion was found");
}
{
  ok(pickLeaseLiability({}, ["OperatingLeaseLiability"], ["OperatingLeaseLiabilityCurrent"],
    ["OperatingLeaseLiabilityNoncurrent"]) === null,
    "no lease tags at all yields null, never 0");
}
{
  // Double-count guard: a debt tag whose name already says "Lease" already
  // folds finance leases into total_debt.
  ok(debtTagIncludesLeases("LongTermDebtAndCapitalLeaseObligations"),
    "a debt tag naming leases is detected");
  ok(debtTagIncludesLeases("FinanceLeaseLiabilityNoncurrent"),
    "a lease-liability tag used as a 'debt' tag is also detected");
  ok(!debtTagIncludesLeases("LongTermDebtNoncurrent"),
    "an ordinary debt tag is not flagged");
  ok(!debtTagIncludesLeases(null) && !debtTagIncludesLeases(undefined),
    "a missing debt tag (no debt data at all) is not flagged");
}

/* ------------------------------ series alignment --------------------------- */
console.log("\n· Series alignment: element i always refers to the same period as free_cash_flows[i]");
{
  const ends = ["2023-12-31", "2024-12-31", "2025-12-31"];
  // interest_expense has no row for 2024 — must come back null there, not
  // shifted so 2025's value lands in the 2024 slot.
  const interestSeries = {
    tag: "InterestExpense",
    series: [
      { end: "2023-12-31", val: 100 },
      { end: "2025-12-31", val: 300 },
    ],
  };
  const got = seriesAlignedTo(interestSeries, ends);
  eq(got.length, 3, "output length matches the reference ends, not the source series");
  eq(got[0], 100, "2023 aligns correctly");
  eq(got[1], null, "the missing 2024 year is null, not shifted from 2025");
  eq(got[2], 300, "2025 still lands in its own slot, not shifted");
}
{
  ok(seriesAlignedTo(null, ["2024-12-31", "2025-12-31"]).every((v) => v === null),
    "a missing flow (no tag matched at all) yields an all-null series of the right length");
}
{
  // transform is applied per row (e.g. Math.abs for SBC).
  const sbcSeries = { tag: "ShareBasedCompensation", series: [
    { end: "2024-12-31", val: -50 }, { end: "2025-12-31", val: 60 },
  ] };
  const got = seriesAlignedTo(sbcSeries, ["2024-12-31", "2025-12-31"], (v) => Math.abs(v));
  eq(got[0], 50, "transform is applied to each aligned value");
  eq(got[1], 60, "transform is applied to each aligned value (already positive)");
}

/* ------------------------- interest net-tag sign rule ---------------------- */
console.log("\n· Interest expense: a positive net-tag value is net INCOME, not expense");
{
  // Badger Meter FY2025 (CIK 9092): +$5.124M net, no InterestExpense tag —
  // the unguarded Math.abs() reported that income as a $5.124M expense.
  eq(interestExpenseFromRow(5_124_000, "InterestIncomeExpenseNet"), null,
    "a positive InterestIncomeExpenseNet is missing, not flipped into an expense");
  eq(interestExpenseFromRow(-5_124_000, "InterestIncomeExpenseNet"), 5_124_000,
    "a negative InterestIncomeExpenseNet (real net expense) becomes its absolute value");
  eq(interestExpenseFromRow(-5_124_000, "InterestExpense"), 5_124_000,
    "a direct InterestExpense tag is taken as-is (abs), no sign-rule applied");
  eq(interestExpenseFromRow(5_124_000, "InterestExpense"), 5_124_000,
    "a positive InterestExpense tag is a real expense, unaffected by the net-tag rule");
  eq(interestExpenseFromRow(null, "InterestIncomeExpenseNet"), null,
    "a missing value stays null");
  ok(NET_INTEREST_TAGS.includes("InterestIncomeExpenseNet"),
    "InterestIncomeExpenseNet is the tag the sign rule guards");
}

/* --------------------------- IFRS interest classification ------------------ */
console.log("\n· IFRS interest-paid classification: operating vs financing vs undisclosed");
{
  const facts = { InterestPaidClassifiedAsOperatingActivities: { units: { pure: [{ end: "2025-03-31", val: 1 }] } } };
  eq(ifrsInterestClassification(facts), "operating",
    "an operating-classification disclosure reports 'operating' (e.g. Infosys)");
}
{
  const facts = { InterestPaidClassifiedAsFinancingActivities: { units: { pure: [{ end: "2025-03-31", val: 1 }] } } };
  eq(ifrsInterestClassification(facts), "financing",
    "a financing-classification disclosure reports 'financing'");
}
{
  eq(ifrsInterestClassification({}), null,
    "no classification tag at all (e.g. HDB/WIT often omit it) reports null, not a guess");
}
{
  // Operating takes precedence when (implausibly) both are present, since
  // it's the ASC-230-equivalent default and the more common IFRS choice.
  const facts = {
    InterestPaidClassifiedAsOperatingActivities: { units: { pure: [{ end: "2025-03-31", val: 1 }] } },
    InterestPaidClassifiedAsFinancingActivities: { units: { pure: [{ end: "2025-03-31", val: 1 }] } },
  };
  eq(ifrsInterestClassification(facts), "operating",
    "operating is checked before financing");
}

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
