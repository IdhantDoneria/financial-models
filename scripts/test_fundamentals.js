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
        ADR_LOCAL_LISTINGS, ADR_RATIO_CANDIDATES, ADR_RATIO_TOLERANCE, STOCK_TAGS, FLOW_TAGS, IFRS_FLOW_TAGS,
        computeSplitAdjustment, SPLIT_ALLOTMENT_LAG_MS,
        annualizedVol, regressionStats,
        pickLeaseLiability, debtTagIncludesLeases, NET_INTEREST_TAGS,
        interestExpenseFromRow, seriesAlignedTo, ifrsInterestClassification,
        dedupeMonthlyCloses, monthKey, monthKeyToLabel, currentMonthKey, monthlyReturnPairs,
        inferIfrsLeaseInterestClassification, LEASE_INTEREST_FINANCING_RATIO,
        LEASE_INTEREST_OPERATING_RATIO } = _internals;

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

/* --------------- IFRS interest classification: lease-evidence fallback ---- */
console.log("\n· IFRS interest-paid classification: inferred from lease cash-flow evidence when undisclosed");
{
  // INFY FY2025-03-31 shape: CashOutflowForLeases and the financing-lease
  // payments tag both report 278,000,000 for the same period — payments
  // cover the whole total, so lease interest must be inside financing too.
  const facts = {
    CashOutflowForLeases: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 278_000_000, filed: "2025-05-15" },
    ] } },
    PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 278_000_000, filed: "2025-05-15" },
    ] } },
  };
  eq(inferIfrsLeaseInterestClassification(facts), "financing",
    "payments covering ~100% of the total infers 'financing' (verified live: INFY FY2025)");
}
{
  // Financing payments materially below the total means the missing piece
  // (interest) is booked in operating activities instead.
  const facts = {
    CashOutflowForLeases: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 100_000_000, filed: "2025-05-15" },
    ] } },
    PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 70_000_000, filed: "2025-05-15" },
    ] } },
  };
  eq(inferIfrsLeaseInterestClassification(facts), "operating",
    "financing payments well below the total infers 'operating'");
}
{
  // A ratio between the two thresholds is ambiguous from this evidence alone.
  const facts = {
    CashOutflowForLeases: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 100_000_000, filed: "2025-05-15" },
    ] } },
    PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 95_000_000, filed: "2025-05-15" },
    ] } },
  };
  eq(inferIfrsLeaseInterestClassification(facts), null,
    "a ratio between the operating and financing thresholds stays null rather than guessing");
  ok(LEASE_INTEREST_OPERATING_RATIO < 0.95 && 0.95 < LEASE_INTEREST_FINANCING_RATIO,
    "0.95 is genuinely inside the ambiguous band this fixture exercises");
}
{
  ok(inferIfrsLeaseInterestClassification({}) === null,
    "neither lease tag disclosed at all yields null, not a guess");
  const onlyTotal = { CashOutflowForLeases: { units: { INR: [
    { start: "2024-04-01", end: "2025-03-31", val: 100, filed: "2025-05-15" },
  ] } } };
  ok(inferIfrsLeaseInterestClassification(onlyTotal) === null,
    "only one of the two lease tags disclosed yields null (nothing to compare)");
}
{
  // Different periods for the two tags aren't a reliable comparison.
  const facts = {
    CashOutflowForLeases: { units: { INR: [
      { start: "2023-04-01", end: "2024-03-31", val: 100_000_000, filed: "2024-05-15" },
    ] } },
    PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities: { units: { INR: [
      { start: "2024-04-01", end: "2025-03-31", val: 100_000_000, filed: "2025-05-15" },
    ] } },
  };
  ok(inferIfrsLeaseInterestClassification(facts) === null,
    "mismatched latest period ends yield null rather than comparing unrelated years");
}

/* ------------------------------ monthly returns ---------------------------- */
console.log("\n· monthlyReturns: Yahoo's extra live current-month point is deduped and dropped");
{
  // Mirrors the real chart shape: one bar per calendar month, stamped at the
  // month start, PLUS one extra live point inside the still-open current
  // month (e.g. a Sep-01 bar followed by a live Sep-23 quote) — the exact
  // pattern that used to overwrite a real monthly return with a near-zero
  // month-to-date figure.
  const monthStart = (monthsAgoFromNow) => {
    const d = new Date();
    d.setUTCDate(1);
    d.setUTCHours(4, 0, 0, 0);
    d.setUTCMonth(d.getUTCMonth() - monthsAgoFromNow);
    return Math.floor(d.getTime() / 1000);
  };
  const DAY = 86_400;
  // 6 monthly bars: 5 completed months plus the current (still-open) one.
  const closes = [100, 110, 121, 133.1, 146.41, 161.051];  // +10% each month
  const series = closes.map((c, i) => ({ t: monthStart(5 - i), c }));
  // The extra live point: same calendar month as the last bar (the current
  // one), stamped later, with a materially different close — this is the
  // duplicate that corrupted a completed month's return before the fix.
  series.push({ t: series[series.length - 1].t + 20 * DAY, c: 140 });

  const deduped = dedupeMonthlyCloses(series);
  eq(deduped.length, 6, "one close survives per calendar month, not one per raw point");
  eq(deduped[5].t, series[6].t,
    "the LAST observation in the current month wins the dedupe (the live point)");

  const r = monthlyReturns(series);
  eq(r.size, 4, "n bars (5 completed + 1 open) yield n-2 completed-month returns: the open month is dropped");
  ok(!r.has(currentMonthKey()),
    "the still-open current month never appears in the returned Map");
  const vals = [...r.values()];
  ok(vals.every((v) => Math.abs(v - 0.10) < 1e-9),
    "every completed month's return is the real +10%, undisturbed by the current month's live point");
}

/* -------------------------------- month labelling --------------------------- */
console.log("\n· Month labelling: a return is labelled by the month its LATER close falls in");
{
  // Two bars one month apart — 2020-06-01 (June) and 2020-07-01 (July),
  // safely in the past so neither is ever the still-open current month —
  // with monthKey computed straight from each bar's own timestamp, exactly
  // as Yahoo stamps them (month start, but the close is that month's
  // month-end close).
  const june = Date.UTC(2020, 5, 1, 4, 0, 0) / 1000;
  const july = Date.UTC(2020, 6, 1, 4, 0, 0) / 1000;
  eq(monthKeyToLabel(monthKey(july)), 202007,
    "a bar dated 2020-07-01 labels as July 2020 (202007), not June");
  eq(monthKeyToLabel(monthKey(june)), 202006,
    "a bar dated 2020-06-01 labels as June 2020 (202006)");
  // The return spanning June's close to July's close is labelled by July —
  // the LATER of the pair — matching the "return from the July bar to the
  // August bar is August's return" convention confirmed against KO's own
  // fetched closes above.
  const r = monthlyReturns([{ t: june, c: 100 }, { t: july, c: 110 }]);
  const [[label, ret]] = [...r.entries()].map(([k, v]) => [monthKeyToLabel(k), v]);
  eq(label, 202007, "the return is keyed to the later bar's month");
  ok(Math.abs(ret - 0.10) < 1e-9, "and its value is the simple return between the two closes");
}

/* --------------------------- [YYYYMM, r] pair shape ------------------------- */
console.log("\n· monthlyReturnPairs: the exact [YYYYMM, r] shape exposed as `monthly_returns`");
{
  const r = new Map([[2026 * 12 + 6, 0.0123456789], [2026 * 12 + 5, -0.02]]); // Jul then Jun, out of order
  const pairs = monthlyReturnPairs(r);
  eq(pairs.length, 2, "one pair per Map entry");
  ok(Array.isArray(pairs[0]) && pairs[0].length === 2, "each entry is a 2-element [YYYYMM, r] pair");
  eq(pairs[0][0], 202606, "pairs are sorted ascending by month, oldest first");
  eq(pairs[1][0], 202607, "...newest last");
  eq(pairs[1][1], 0.012346, "r is rounded to 6 decimal places");
  eq(pairs[0][1], -0.02, "a negative return round-trips correctly");
}

/* ------------------- transient upstream failures & caching ------------------ */
console.log("\n· A transient SEC failure must never be CDN-cached as the answer");
{
  const t = new Error("x"); t.name = "TimeoutError";
  ok(_internals.isTransient(t), "a timeout is transient");
  ok(_internals.isTransient(new Error("companyfacts 503")), "a 5xx is transient");
  ok(_internals.isTransient(new Error("companyfacts 429")), "a 429 is transient");
  ok(_internals.isTransient(new TypeError("fetch failed")), "a dropped connection is transient");
  ok(!_internals.isTransient(new Error("companyfacts 404")), "a 404 is permanent");
  ok(!_internals.isTransient(new Error("no us-gaap or ifrs-full facts")), "no XBRL is permanent");
}

/* ------------------- balance sheet: one date, all debt lines -------------- */
//  Values are the real filed figures from the 2026-09 audit (SEC companyfacts
//  on 2026-09-24), trimmed to the rows that matter.
console.log("\n· Balance sheet — every line from one date, debt summed by role");
{
  const { pickBalanceSheet, pickCoverShares, mergeFacts, PREDECESSOR_CIKS,
          ADR_PINNED_RATIOS, ADR_PINNED_BAND, MINOR_UNIT_QUOTES } = _internals;
  const inst = (end, val, filed = "2026-08-01") => ({ end, val, form: "10-Q", filed });
  const T = (rows, unit = "USD") => ({ units: { [unit]: rows } });

  // KO: LongTermDebt abandoned after Q1 2024; the 2026 lines are the
  // CapitalLeaseObligations family plus commercial paper.
  const ko = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2025-12-31", 10.27e9), inst("2026-04-03", 10.574e9)]),
    MarketableSecuritiesCurrent: T([inst("2020-12-31", 2.348e9)]),
    LongTermDebtNoncurrent: T([inst("2024-03-29", 35.104e9)]),
    LongTermDebt: T([inst("2024-03-29", 36.496e9)]),
    LongTermDebtCurrent: T([inst("2024-03-29", 1.392e9)]),
    LongTermDebtAndCapitalLeaseObligations: T([inst("2026-04-03", 39.065e9)]),
    LongTermDebtAndCapitalLeaseObligationsCurrent: T([inst("2026-04-03", 4.493e9)]),
    CommercialPaper: T([inst("2026-04-03", 0.25e9)]),
  };
  const k = pickBalanceSheet(ko, "us-gaap", "USD");
  eq(k.anchor, "2026-04-03", "KO: anchored on the latest cash date");
  eq(Math.round(k.debt / 1e6), 43808, "KO: $43.808bn, not the March-2024 $36.5bn");
  ok(k.sti === null, "KO: a 2020 marketable-securities figure is not added to 2026 cash");

  // XOM: DebtCurrent + LongTermDebtAndCapitalLeaseObligations only.
  const xom = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2026-06-30", 10.588e9)]),
    DebtCurrent: T([inst("2026-06-30", 10.139e9)]),
    LongTermDebtAndCapitalLeaseObligations: T([inst("2026-06-30", 32.229e9)]),
  };
  const x = pickBalanceSheet(xom, "us-gaap", "USD");
  eq(Math.round(x.debt / 1e6), 42368, "XOM: long-term bonds counted ($42.4bn, not $10.1bn)");
  ok(x.debtParts.some((p) => debtTagIncludesLeases(p.tag)),
    "XOM: the lease-inclusive tag is visible to the finance-lease double-count guard");

  // AAPL: commercial paper is its own line beside current maturities.
  const aapl = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2026-06-27", 39.544e9)]),
    MarketableSecuritiesCurrent: T([inst("2026-06-27", 22.855e9)]),
    LongTermDebtNoncurrent: T([inst("2026-06-27", 71.34e9)]),
    LongTermDebt: T([inst("2026-06-27", 82.3e9)]),
    LongTermDebtCurrent: T([inst("2026-06-27", 11.007e9)]),
    CommercialPaper: T([inst("2026-06-27", 1.997e9)]),
    OtherShortTermBorrowings: T([inst("2020-06-27", 11.166e9)]),
  };
  const a = pickBalanceSheet(aapl, "us-gaap", "USD");
  eq(Math.round(a.debt / 1e6), 84344, "AAPL: commercial paper included, 2020 borrowings not");
  eq(Math.round((a.cash + a.sti) / 1e6), 62399, "AAPL: cash + marketable securities on the same date");

  // LongTermDebt already includes current maturities — never add them again.
  const totalOnly = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2026-06-30", 1e9)]),
    LongTermDebt: T([inst("2026-06-30", 10e9)]),
    LongTermDebtCurrent: T([inst("2026-06-30", 2e9)]),
  };
  eq(pickBalanceSheet(totalOnly, "us-gaap", "USD").debt, 10e9,
    "LongTermDebt is not summed with its own current portion");

  // No debt tag on the date: missing, never zero.
  // A company that has borrowed (maturity schedule) but whose face lines are
  // unrecognisable (Berkshire's custom tags): missing, never zero.
  const noDebt = { CashAndCashEquivalentsAtCarryingValue: T([inst("2026-06-30", 1e9)]),
                   LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo: T([inst("2025-12-31", 6.6e9)]) };
  ok(pickBalanceSheet(noDebt, "us-gaap", "USD").debt === null, "no debt line -> null, not 0");

  // TSMC: bonds are a separate IFRS line, and current bonds exist only in TWD.
  const tsm = {
    CashAndCashEquivalents: { units: {
      USD: [inst("2024-12-31", 64.886e9)], TWD: [inst("2024-12-31", 2127.627e9)] } },
    LongtermBorrowings: { units: { USD: [inst("2024-12-31", 0.971e9)] } },
    NoncurrentPortionOfNoncurrentBondsIssued: { units: { USD: [inst("2024-12-31", 28.259e9)] } },
    CurrentPortionOfLongtermBorrowings: { units: { USD: [inst("2024-12-31", 1.825e9)] } },
    ShorttermBorrowings: { units: { USD: [inst("2021-12-31", 4.143e9)] } },
    CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued: { units: { TWD: [inst("2024-12-31", 57.148e9)] } },
  };
  const t = pickBalanceSheet(tsm, "ifrs-full", "USD");
  const rate = 64.886 / 2127.627;
  ok(Math.abs(t.debt - (0.971e9 + 28.259e9 + 1.825e9 + 57.148e9 * rate)) < 1e3,
    "TSM: loans + bonds, TWD-only current bonds at the filer's own rate; 2021 borrowings excluded",
    String(t.debt));
  eq(t.translated.length, 1, "TSM: the translated line is reported");

  // A foreign-currency-only line with no same-date cash pair is left out.
  const noRate = { ...tsm, CashAndCashEquivalents: { units: { USD: [inst("2024-12-31", 64.886e9)] } } };
  ok(pickBalanceSheet(noRate, "ifrs-full", "USD").translated.length === 0,
    "no filer-implied rate -> the TWD line is not guessed into USD");

  // Currency: the currency reaching the newest fiscal year wins (SAP).
  const yr = (y, v) => ({ start: `${y}-01-01`, end: `${y}-12-31`, val: v, form: "20-F", filed: `${y + 1}-03-01` });
  const sap = { Revenue: { units: { USD: [yr(2017, 28.205e9)], EUR: [yr(2017, 23.461e9), yr(2025, 36.8e9)] } } };
  eq(detectReportingCurrency(sap, ["Revenue"]), "EUR", "SAP: EUR (FY2025), not USD frozen at FY2017");

  // Shares: an ISSUED count is not a substitute for a staler OUTSTANDING one.
  const jpm = {
    CommonStockSharesOutstanding: { units: { shares: [inst("2025-12-31", 2.6962e9)] } },
    CommonStockSharesIssued: { units: { shares: [inst("2026-06-30", 4.104933895e9)] } },
  };
  eq(pickInstant(jpm, STOCK_TAGS.shares_outstanding, null, 0.01, false).value, 2.6962e9,
    "JPM: shares outstanding, not 4.1bn issued (treasury included)");

  // Cover-page shares: usable single-class, refused multi-class.
  const dei1 = { EntityCommonStockSharesOutstanding: { units: { shares: [
    { end: "2026-06-30", val: 2658186195, filed: "2026-08-06" }] } } };
  eq(pickCoverShares(dei1).value, 2658186195, "single-class cover count is read");
  const brk = { EntityCommonStockSharesOutstanding: { units: { shares: [
    { end: "2026-07-20", val: 552000, filed: "2026-08-03" },
    { end: "2026-07-20", val: 1300000000, filed: "2026-08-03" }] } } };
  ok(pickCoverShares(brk) === null, "Berkshire's two classes on one date -> refused");

  // Predecessor facts merge beneath the successor's.
  eq(PREDECESSOR_CIKS["0002115436"], "0000034088", "XOM holdings maps to Exxon Mobil Corp");
  const merged = mergeFacts(
    { "us-gaap": { Revenues: { units: { USD: [yr(2025, 332.238e9)] } } } },
    { "us-gaap": { Revenues: { units: { USD: [{ start: "2026-01-01", end: "2026-06-30", val: 201e9, filed: "2026-08-03" }] } } } });
  const rev = pickAnnualSeries(merged["us-gaap"], ["Revenues"]);
  eq(rev.series[rev.series.length - 1].val, 332.238e9, "XOM: FY2025 revenue comes through the merge");

  // ADRs.
  eq(ADR_PINNED_RATIOS.TSM.ratio, 5, "TSM carries its contractual 1 ADS = 5 shares");
  // Implied ratios carry TSM's ~15% premium whatever the ratio is, so a real
  // change to 6 or 10 shares per ADS would imply ~6.9 or ~11.5.
  ok(Math.abs(5.735 / 5 - 1) <= ADR_PINNED_BAND, "TSM's live 5.735 (15% premium) is accepted");
  ok(Math.abs(6 * 1.147 / 5 - 1) > ADR_PINNED_BAND && Math.abs(10 * 1.147 / 5 - 1) > ADR_PINNED_BAND,
    "a changed ratio (6 or 10) at the same premium is refused");
  ok(ADR_RATIO_CANDIDATES.includes(8) && ADR_RATIO_CANDIDATES.includes(0.25),
    "BABA (8) and POSCO (0.25) ratios are candidates");
  ok(["BABA", "TM", "SHEL", "SAP", "BHP"].every((s) => ADR_LOCAL_LISTINGS[s]),
    "major non-Indian ADRs map to home listings");
  eq(MINOR_UNIT_QUOTES.GBp.div, 100, "London pence quotes normalise to pounds");

  // Spotify: a component revenue tag must not beat the total on a tie.
  const { latestAnnualEnd, latestFilingForm } = _internals;
  const spot = {
    RevenueFromContractsWithCustomers: { units: { EUR: [yr(2025, 0.665e9)] } },
    Revenue: { units: { EUR: [yr(2025, 17.186e9)] } },
  };
  eq(pickAnnualSeries(spot, IFRS_FLOW_TAGS.revenue, 6, "EUR", true).series[0].val, 17.186e9,
    "SPOT: EUR 17.2bn total revenue, not the EUR 0.67bn component");
  eq(pickAnnualSeries(spot, IFRS_FLOW_TAGS.revenue, 6, "EUR").series[0].val, 0.665e9,
    "(the tie-break only applies where asked — other concepts keep cascade order)");

  // Toyota/Sony: the taxonomy reaching the newest year wins.
  const toyotaGaap = { Revenues: { units: { JPY: [{ start: "2019-04-01", end: "2020-03-31", val: 29.9e12 }] } } };
  const toyotaIfrs = { Revenue: { units: { JPY: [{ start: "2024-04-01", end: "2025-03-31", val: 48.0e12 }] } } };
  ok(latestAnnualEnd(toyotaIfrs, IFRS_FLOW_TAGS.revenue) > latestAnnualEnd(toyotaGaap, FLOW_TAGS.revenue),
    "TM: IFRS (FY2025) is fresher than the abandoned US GAAP (FY2020)");

  // Berkshire: a 2011 cover count is not a current one (the handler's
  // recency window); the helper itself still reads it.
  const brkOld = { EntityCommonStockSharesOutstanding: { units: { shares: [
    { end: "2011-04-29", val: 941481, form: "10-Q", filed: "2011-05-06" }] } } };
  eq(pickCoverShares(brkOld).end, "2011-04-29", "BRK's last plain cover count is from 2011");

  // SMFG: no revenue tags, but the filing form still identifies a 20-F filer.
  const smfgDei = { EntityCommonStockSharesOutstanding: { units: { shares: [
    { end: "2026-03-31", val: 3.9e9, form: "20-F", filed: "2026-06-27" }] } } };
  eq(latestFilingForm([smfgDei, {}]), "20-F", "SMFG: form read from the cover facts");

  // Alibaba: no role tag, three face-line components on the anchor date.
  const { hasEverBorrowed, pickWeightedAverageShares } = _internals;
  const baba = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2026-03-31", 19.07e9)]),
    LongTermLoansFromBank: T([inst("2026-03-31", 6.879e9)]),
    SeniorLongTermNotes: T([inst("2026-03-31", 17.03e9)]),
    ConvertibleDebtNoncurrent: T([inst("2026-03-31", 8.098e9)]),
    SeniorNotesCurrent: T([inst("2025-03-31", 0)]),
  };
  const bb = pickBalanceSheet(baba, "us-gaap", "USD");
  eq(Math.round(bb.debt / 1e6), 32007, "BABA: bank loans + senior notes + convertibles");
  ok(bb.fromComponents, "BABA: flagged as summed from components");
  // A role tag on the date wins; components are never added on top of it.
  const both = { ...aapl, SeniorLongTermNotes: T([inst("2026-06-27", 60e9)]) };
  eq(Math.round(pickBalanceSheet(both, "us-gaap", "USD").debt / 1e6), 84344,
    "components are ignored when a role tag exists (no double count)");

  // Infosys: nothing borrowed, ever — a 4.5% rate tag is not a borrowing.
  const infy = {
    CashAndCashEquivalents: { units: { USD: [inst("2025-03-31", 2.861e9)] } },
    BankBorrowingsUndiscountedCashFlows: { units: { USD: [inst("2025-03-31", 0)] } },
    WeightedAverageLesseesIncrementalBorrowingRateAppliedToLeaseLiabilitiesRecognisedAtDateOfInitialApplicationOfIFRS16:
      { units: { pure: [inst("2019-04-01", 0.045)] } },
  };
  const ib = pickBalanceSheet(infy, "ifrs-full", "USD");
  ok(ib.debt === 0 && ib.neverBorrowed, "INFY: debt-free, reported as 0 with the reason");
  // PDD repaid its convertibles in 2025 and tags no debt line now: unknown,
  // not zero — it HAS borrowed before.
  const pdd = {
    CashAndCashEquivalentsAtCarryingValue: T([inst("2025-12-31", 8e9)]),
    ConvertibleDebtCurrent: T([inst("2024-12-31", 0.727e9)]),
  };
  ok(pickBalanceSheet(pdd, "us-gaap", "USD").debt === null, "PDD: a past borrower with no line now is missing, not 0");
  ok(hasEverBorrowed({ LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo: T([inst("2025-12-31", 6.6e9)]) }),
    "a maturity schedule alone (Berkshire) proves borrowing");
  ok(!hasEverBorrowed({ AvailableForSaleSecuritiesDebtSecurities: T([inst("2026-06-30", 17e9)]) }),
    "debt securities HELD are not borrowings");

  // META: the quarter's weighted average, not year-to-date, at the same end.
  const meta = { WeightedAverageNumberOfSharesOutstandingBasic: { units: { shares: [
    { start: "2026-01-01", end: "2026-06-30", val: 2.551e9, filed: "2026-07-30" },
    { start: "2026-04-01", end: "2026-06-30", val: 2.543e9, filed: "2026-07-30" },
    { start: "2025-01-01", end: "2025-12-31", val: 2.59e9, filed: "2026-01-29" }] } } };
  const w = pickWeightedAverageShares(meta, "us-gaap");
  ok(w.value === 2.543e9 && w.start === "2026-04-01", "META: latest quarter's basic weighted average");
  ok(IFRS_FLOW_TAGS.dividends_per_share.includes("DividendsPaidOrdinarySharesPerShare"),
    "Shell/AstraZeneca's plural dividend tag is read");
  // Novo: a bare "DKK" unit of monthly rows beside the real DKK/shares figure.
  const nvo = { DividendsPaidOrdinarySharesPerShare: { units: {
    DKK: [{ start: "2020-01-01", end: "2020-12-31", val: 9.1 }, { start: "2020-08-01", end: "2020-08-31", val: 3.25 }],
    "DKK/shares": [yr(2024, 11.4), yr(2025, 11.7)] } } };
  eq(pickAnnualSeries(nvo, IFRS_FLOW_TAGS.dividends_per_share, 6, "DKK").series.pop().val, 11.7,
    "NVO: DKK/shares 11.70, not the mislabelled bare-DKK rows");
  // Sony: cash paid in the year (a ¥10 interim) must not beat the ¥95 recognised.
  const sony = {
    DividendsPaidOrdinarySharesPerShare: { units: { "JPY/shares": [{ start: "2024-04-01", end: "2025-03-31", val: 10 }] } },
    DividendsRecognisedAsDistributionsToOwnersPerShare: { units: { "JPY/shares": [{ start: "2024-04-01", end: "2025-03-31", val: 95 }] } },
  };
  eq(pickAnnualSeries(sony, IFRS_FLOW_TAGS.dividends_per_share, 6, "JPY").series.pop().val, 95,
    "SONY: recognised dividend wins a same-year tie over cash paid");
}

(async () => {
  //: Real regression: one SEC timeout on KO was served to every visitor for
  //  an hour as a CDN HIT ("market data only", no cash flows). Simulate it.
  const handler = require("../api/fundamentals.js");
  const realFetch = global.fetch;
  const months = 30, now = Math.floor(Date.now() / 1000);
  const chart = {
    chart: { result: [{
      meta: { regularMarketPrice: 88, regularMarketTime: now, currency: "USD" },
      timestamp: Array.from({ length: months }, (_, i) => now - (months - i) * 30 * 86400),
      indicators: { quote: [{ close: Array.from({ length: months }, (_, i) => 80 + (i % 5)) }],
                    adjclose: [{ adjclose: Array.from({ length: months }, (_, i) => 80 + (i % 5)) }] },
      events: {},
    }] },
  };
  const json = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
  const run = async (factsBehaviour) => {
    let factsCalls = 0;
    global.fetch = async (url) => {
      url = String(url);
      //: The handler caches this directory for 24h, so it lists every ticker
      //  the later handler scenarios resolve too.
      if (url.includes("company_tickers")) return json({
        0: { cik_str: 21344, ticker: "KO", title: "COCA COLA CO" },
        1: { cik_str: 1000184, ticker: "SAP", title: "SAP SE" },
        2: { cik_str: 9999999, ticker: "ZZADR", title: "Foreign Co" },
        3: { cik_str: 1144967, ticker: "HDB", title: "HDFC BANK LTD" },
        4: { cik_str: 2115436, ticker: "XOM", title: "ExxonMobil Holdings Corp" },
        5: { cik_str: 1094517, ticker: "TM", title: "TOYOTA MOTOR CORP" },
        6: { cik_str: 1000275, ticker: "RY", title: "ROYAL BANK OF CANADA" } });
      if (url.includes("companyfacts")) { factsCalls++; return factsBehaviour(); }
      if (url.includes("finance.yahoo.com")) return json(chart);
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    };
    const headers = {}; let status = 0, body = null;
    const res = {
      setHeader: (k, v) => { headers[k.toLowerCase()] = v; },
      status(c) { status = c; return this; },
      json(o) { body = o; return this; },
      end() { return this; },
    };
    await handler({ method: "GET", url: "/api/fundamentals?ticker=KO", query: { ticker: "KO" },
                    headers: { "x-forwarded-for": "203.0.113.9" } }, res);
    return { headers, status, body, factsCalls };
  };
  try {
    //: A real timeout only fires after secFetch's 12s budget; advance the
    //  clock by that much so the handler sees what production would.
    const realNow = Date.now; let skew = 0;
    Date.now = () => realNow() + skew;
    const timeout = () => { skew += 12_000; const e = new Error("The operation was aborted due to timeout"); e.name = "TimeoutError"; throw e; };
    const a = await run(timeout);
    Date.now = realNow;
    eq(a.headers["cache-control"], "no-store", "timed-out SEC fetch: degraded response is not cacheable");
    ok(a.body && /didn't load this time/.test((a.body.notes || []).join(" ")), "and tells the user it's temporary");
    eq(a.factsCalls, 1, "a timeout is not retried inside the same request (budget already spent)");

    const flaky = (() => { let n = 0; return () => (n++ === 0 ? { ok: false, status: 503, json: async () => ({}) } : json({ entityName: "COCA COLA CO", facts: {} })); })();
    const b = await run(flaky);
    eq(b.factsCalls, 2, "a fast 503 is retried once");

    const gone = () => ({ ok: false, status: 404, json: async () => ({}) });
    const c = await run(gone);
    ok(c.headers["cache-control"] !== "no-store", "a permanent 404 stays cacheable");
  } catch (e) {
    ok(false, "transient-failure simulation ran", e && e.stack);
  } finally {
    global.fetch = realFetch;
  }

  //: End-to-end through the handler: currency basis, depositary basis and a
  //  predecessor registrant, with SEC / Yahoo / FX mocked from real shapes.
  console.log("\n· Handler — price, shares and dividend on the listing's basis");
  const yr = (y, v, form = "20-F") => ({ start: `${y}-01-01`, end: `${y}-12-31`, val: v, form, filed: `${y + 1}-03-01` });
  const inst = (end, val, form = "20-F") => ({ end, val, form, filed: "2026-03-01" });
  const chartFor = (price, currency) => ({ chart: { result: [{
    meta: { regularMarketPrice: price, regularMarketTime: now, currency },
    timestamp: Array.from({ length: months }, (_, i) => now - (months - i) * 30 * 86400),
    indicators: { quote: [{ close: Array.from({ length: months }, (_, i) => price + (i % 5)) }],
                  adjclose: [{ adjclose: Array.from({ length: months }, (_, i) => price + (i % 5)) }] },
    events: {},
  }] } });
  const runCase = async ({ ticker, cik, factsByCik, quotes, rates = {}, sic = null }) => {
    global.fetch = async (url) => {
      url = String(url);
      if (url.includes("company_tickers")) return json({ 0: { cik_str: Number(cik), ticker, title: ticker } });
      if (url.includes("/submissions/")) {
        return sic ? json({ sic: String(sic), sicDescription: "x" }) : { ok: false, status: 404, json: async () => ({}) };
      }
      const m = url.match(/CIK(\d{10})\.json/);
      if (m) return factsByCik[m[1]] ? json(factsByCik[m[1]]) : { ok: false, status: 404, json: async () => ({}) };
      if (url.includes("open.er-api.com")) return json({ rates: { USD: 1, ...rates } });
      const sym = decodeURIComponent((url.match(/chart\/([^?]+)/) || [])[1] || "");
      if (url.includes("finance.yahoo.com")) {
        if (quotes[sym]) return json(chartFor(...quotes[sym]));
        if (sym.startsWith("^")) return json(chartFor(5000, "USD"));
      }
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    };
    let body = null;
    const res = { setHeader() {}, status() { return this; }, json(o) { body = o; return this; }, end() { return this; } };
    await handler({ method: "GET", url: `/api/fundamentals?ticker=${ticker}`, query: { ticker },
                    headers: { "x-forwarded-for": `198.51.100.${Math.floor(Math.random() * 200)}` } }, res);
    return body;
  };
  try {
    // SAP: EUR financials, USD ADR, 1:1 with SAP.DE, cover-page share count.
    const sap = await runCase({
      ticker: "SAP", cik: "0001000184",
      factsByCik: { "0001000184": { entityName: "SAP SE", facts: {
        dei: { EntityCommonStockSharesOutstanding: { units: { shares: [inst("2025-12-31", 1228504232)] } } },
        "ifrs-full": {
          Revenue: { units: { USD: [yr(2017, 28.205e9)], EUR: [yr(2024, 34.176e9), yr(2025, 36.8e9)] } },
          CashAndCashEquivalents: { units: { EUR: [inst("2025-12-31", 8.22e9)] } },
          Borrowings: { units: { EUR: [inst("2025-12-31", 6.15e9)] } },
        } } } },
      quotes: { SAP: [210.62, "USD"], "SAP.DE": [184.8, "EUR"] },
      rates: { EUR: 0.8774 },
    });
    const S = sap && sap.fields || {};
    eq(S.currency, "EUR", "SAP reports in EUR (FY2025), not USD frozen at 2017");
    eq(S.fiscal_year, 2025, "SAP's fiscal year is 2025");
    ok(Math.abs(S.current_price - 210.62 * 0.8774) < 1e-6, "USD ADR price converted to EUR at the live rate",
      String(S.current_price));
    eq(S.shares_outstanding, 1228504232, "cover-page share count used when no statement count exists");
    eq(S.total_debt, 6.15e9, "SAP debt from the same balance sheet");

    // An unmapped Form 20-F filer: shares and dividend withheld, not mixed.
    const unk = await runCase({
      ticker: "ZZADR", cik: "0009999999",
      factsByCik: { "0009999999": { entityName: "Foreign Co", facts: { "us-gaap": {
        Revenues: { units: { USD: [yr(2025, 5e9)] } },
        CommonStockSharesOutstanding: { units: { shares: [inst("2025-12-31", 8e9)] } },
        CommonStockDividendsPerShareDeclared: { units: { "USD/shares": [yr(2025, 0.4)] } },
        CashAndCashEquivalentsAtCarryingValue: { units: { USD: [inst("2025-12-31", 1e9)] } },
      } } } },
      quotes: { ZZADR: [40, "USD"] },
    });
    const U = unk && unk.fields || {};
    ok(U.shares_outstanding === null, "unmapped 20-F filer: ordinary share count withheld");
    ok(U.dividend_per_share === null, "unmapped 20-F filer: per-ordinary-share dividend withheld");
    eq(U.current_price, 40, "its price is still reported");

    // HDB-style ratio 3: shares divided, dividend multiplied.
    const hdb = await runCase({
      ticker: "HDB", cik: "0001144967",
      factsByCik: { "0001144967": { entityName: "HDFC BANK LTD", facts: { "ifrs-full": {
        Revenue: { units: { USD: [yr(2025, 20e9)] } },
        NumberOfSharesOutstanding: { units: { shares: [inst("2025-12-31", 15.3e9)] } },
        DividendsPaidOrdinarySharePerShare: { units: { "USD/shares": [yr(2025, 0.25)] } },
        CashAndCashEquivalents: { units: { USD: [inst("2025-12-31", 5e9)] } },
      } } } },
      quotes: { HDB: [70, "USD"], "HDFCBANK.NS": [70 * 95.8 / 3, "INR"] },
      rates: { INR: 95.8 },
    });
    const H = hdb && hdb.fields || {};
    eq(H.shares_outstanding, 5.1e9, "HDB: 15.3bn ordinary shares -> 5.1bn ADS");
    ok(Math.abs(H.dividend_per_share - 0.75) < 1e-9, "HDB: dividend restated per ADS (0.25 x 3)",
      String(H.dividend_per_share));

    // XOM: successor registrant with only 10-Qs; history from the predecessor.
    const xom = await runCase({
      ticker: "XOM", cik: "0002115436",
      factsByCik: {
        "0002115436": { entityName: "ExxonMobil Holdings Corp", facts: { "us-gaap": {
          Revenues: { units: { USD: [{ start: "2026-01-01", end: "2026-06-30", val: 201.155e9, form: "10-Q", filed: "2026-08-03" }] } },
          CashAndCashEquivalentsAtCarryingValue: { units: { USD: [inst("2026-06-30", 10.588e9, "10-Q")] } },
          DebtCurrent: { units: { USD: [inst("2026-06-30", 10.139e9, "10-Q")] } },
          LongTermDebtAndCapitalLeaseObligations: { units: { USD: [inst("2026-06-30", 32.229e9, "10-Q")] } },
        } } },
        "0000034088": { entityName: "EXXON MOBIL CORP", facts: { "us-gaap": {
          Revenues: { units: { USD: [yr(2024, 339.247e9, "10-K"), yr(2025, 332.238e9, "10-K")] } },
        } } },
      },
      quotes: { XOM: [161.23, "USD"] },
      sic: 2911,
    });
    const X = xom && xom.fields || {};
    eq(X.revenue, 332.238e9, "XOM: FY2025 revenue from the predecessor registrant");
    eq(Math.round(X.total_debt / 1e6), 42368, "XOM: 2026 debt from the successor's balance sheet");
    ok((xom.notes || []).some((n) => /predecessor registrant/.test(n)), "XOM: the merge is disclosed");
    eq(X.sic_code, 2911, "XOM: SIC code from EDGAR submissions (petroleum refining)");
    ok(S.sic_code === null && !(sap.missing || []).includes("sic_code"),
      "a failed SIC lookup is null and not counted as a missing figure");

    // Toyota-style: OCF and D&A tagged, no capex line -> OCF − D&A, flagged.
    const tm = await runCase({
      ticker: "TM", cik: "0001094517",
      factsByCik: { "0001094517": { entityName: "TOYOTA MOTOR CORP", facts: { "ifrs-full": {
        Revenue: { units: { JPY: [2022, 2023, 2024].map((y) => yr(y, 40e12)) } },
        CashFlowsFromUsedInOperatingActivities: { units: { JPY: [yr(2022, 3.0e12), yr(2023, 3.5e12), yr(2024, 3.7e12)] } },
        DepreciationAndAmortisationExpense: { units: { JPY: [yr(2022, 2.0e12), yr(2023, 2.1e12), yr(2024, 2.25e12)] } },
        CashAndCashEquivalents: { units: { JPY: [inst("2024-12-31", 15e12)] } },
      } } } },
      quotes: { TM: [190, "USD"], "7203.T": [190 * 158 / 10, "JPY"] },
      rates: { JPY: 158 },
    });
    const TF = tm && tm.fields || {};
    eq(TF.fcf_basis, "ocf_minus_da", "TM: FCF built as OCF − D&A and flagged");
    eq(JSON.stringify(TF.free_cash_flows), JSON.stringify([1.0e12, 1.4e12, 1.45e12]),
      "TM: each year's OCF minus that year's D&A");
    ok(!(tm.notes || []).some((n) => n.startsWith("No capital-expenditure tag found")),
      "TM: no contradictory 'could not be derived' note");

    // A deposit-taking bank never gets the proxy (its OCF is loan/deposit flow).
    const bank = await runCase({
      ticker: "RY", cik: "0001000275",
      factsByCik: { "0001000275": { entityName: "ROYAL BANK OF CANADA", facts: { "ifrs-full": {
        Revenue: { units: { CAD: [2022, 2023, 2024].map((y) => yr(y, 60e9, "40-F")) } },
        CashFlowsFromUsedInOperatingActivities: { units: { CAD: [yr(2022, -20e9, "40-F"), yr(2023, 45e9, "40-F"), yr(2024, 12e9, "40-F")] } },
        DepreciationAndAmortisationExpense: { units: { CAD: [2022, 2023, 2024].map((y) => yr(y, 2e9, "40-F")) } },
        DepositsFromBanks: { units: { CAD: [inst("2024-12-31", 30e9, "40-F")] } },
        CashAndCashEquivalents: { units: { CAD: [inst("2024-12-31", 50e9, "40-F")] } },
      } } } },
      quotes: { RY: [200, "USD"] },
      rates: { CAD: 1.4 },
    });
    ok(bank && bank.fields && !bank.fields.free_cash_flows.length && bank.fields.fcf_basis === null,
      "RY: no OCF − D&A proxy for a bank");
  } catch (e) {
    ok(false, "handler basis simulation ran", e && e.stack);
  } finally {
    global.fetch = realFetch;
  }
  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
