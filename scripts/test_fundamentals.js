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
const { resolveTicker, pickInstant, pickAnnualSeries } = _internals;

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

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
