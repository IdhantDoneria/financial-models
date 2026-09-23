// Unit tests for the Treasury par-yield CSV parser (api/rates.js).
//
//     node scripts/test_rates.js
//
// Offline by design — the fixture below is a trimmed copy of the SHAPE
// Treasury's daily-treasury-rates.csv endpoint actually returns (verified
// live 2026-09-23: newest-first rows, quoted header cells, a "1.5 Month"
// column wedged in between "1 Mo" and "2 Mo"), so this runs without depending
// on home.treasury.gov being reachable or its numbers staying the same.
//
// This covers the four things that are easy to get subtly wrong here and
// impossible to spot by eyeballing a live response:
//   1. the "10 Yr" column must be located BY NAME, not a fixed index —
//      Treasury has added columns before (e.g. "1.5 Month"),
//   2. rows are newest-first, so the first finite value wins,
//   3. a blank cell (market holiday, unpublished maturity) must be skipped
//      rather than parsed as 0 or NaN,
//   4. a value outside a plausible 10Y yield range must throw, not silently
//      feed a nonsense risk-free rate into every downstream model.

const { _internals } = require("../api/rates.js");
const { parseTenYearParYield } = _internals;

let passed = 0, failed = 0;
function ok(cond, label, detail) {
  if (cond) { passed++; console.log(`  ✔ ${label}`); }
  else { failed++; console.log(`  ✘ ${label}${detail !== undefined ? " — " + detail : ""}`); }
}
const eq = (a, b, label) => ok(a === b, label, `got ${a}, want ${b}`);

/* ------------------------- column located by name ------------------------- */
console.log("· \"10 Yr\" is located by header name, not a fixed column index");
{
  // Real shape: header carries a "1.5 Month" column between "1 Mo" and
  // "2 Mo" that a hardcoded index would not expect.
  const csv =
    'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    + "09/22/2026,3.97,4.04,4.09,4.16,4.26,4.26,4.43,4.71,4.81,4.83,4.89,4.96,5.33,5.29\n"
    + "09/21/2026,3.96,4.02,4.10,4.17,4.26,4.27,4.45,4.76,4.82,4.83,4.89,4.96,5.33,5.29\n";
  const got = parseTenYearParYield(csv);
  ok(got !== null, "a well-formed CSV parses");
  eq(got.pct, 4.96, "the 10 Yr value is read from the correctly-named column, not a fixed offset");
  eq(got.iso, "2026-09-22", "the date is reformatted to ISO");
}
{
  // If the columns were ever reordered, locating by name still finds the
  // right one where a fixed index would silently read the wrong maturity.
  const csv =
    'Date,"10 Yr","1 Mo","2 Mo"\n'
    + "01/02/2026,4.50,3.90,4.00\n";
  eq(parseTenYearParYield(csv).pct, 4.50, "reordered columns still resolve by name");
}

/* ---------------------------- newest row wins ------------------------------ */
console.log("\n· Rows are newest-first — the first finite reading wins");
{
  const csv =
    'Date,"1 Mo","10 Yr"\n'
    + "09/22/2026,3.97,4.96\n"
    + "09/21/2026,3.96,4.90\n"
    + "09/18/2026,3.97,5.01\n";
  eq(parseTenYearParYield(csv).pct, 4.96, "the newest row's value is returned, not the max or the oldest");
}

/* ------------------------------- blank cells -------------------------------- */
console.log("\n· A blank cell (holiday / unpublished maturity) is skipped, not parsed as 0");
{
  const csv =
    'Date,"1 Mo","10 Yr"\n'
    + "09/22/2026,3.97,\n"          // blank 10Yr — must be skipped
    + "09/21/2026,3.96,4.90\n";
  eq(parseTenYearParYield(csv).pct, 4.90, "a blank newest-row cell falls through to the next row");
  eq(parseTenYearParYield(csv).iso, "2026-09-21", "and reports that row's date, not the blank one's");
}
{
  ok(parseTenYearParYield('Date,"1 Mo","10 Yr"\n09/22/2026,3.97,\n') === null,
    "all-blank 10Yr column yields null rather than a fabricated value");
}

/* ---------------------------- percent -> decimal ---------------------------- */
console.log("\n· The caller (usTreasury) converts the parsed percent to a decimal");
{
  const csv = 'Date,"10 Yr"\n09/22/2026,4.96\n';
  const got = parseTenYearParYield(csv);
  eq(got.pct, 4.96, "parser itself returns the raw percent");
  ok(Math.abs(got.pct / 100 - 0.0496) < 1e-9, "divided by 100 it lands on the expected decimal rf");
}

/* ------------------------------ out-of-range -------------------------------- */
console.log("\n· A value outside a plausible 10Y yield range throws rather than parsing");
{
  let threw = false;
  try { parseTenYearParYield('Date,"10 Yr"\n09/22/2026,45.6\n'); }
  catch (e) { threw = true; }
  ok(threw, "a wildly out-of-range value (45.6%) throws instead of being silently used");
}
{
  let threw = false;
  try { parseTenYearParYield('Date,"10 Yr"\n09/22/2026,-1.0\n'); }
  catch (e) { threw = true; }
  ok(threw, "a negative value throws too");
}
{
  // Boundary: values at the edge of the documented 0-20% band must still parse.
  const got = parseTenYearParYield('Date,"10 Yr"\n09/22/2026,19.99\n');
  ok(got !== null && got.pct === 19.99, "a value just inside the 0-20% band is accepted");
}

/* -------------------------------- empty file --------------------------------- */
console.log("\n· An empty or header-only file (early January) yields null, not a crash");
{
  ok(parseTenYearParYield("") === null, "an empty string yields null");
  ok(parseTenYearParYield('Date,"1 Mo","10 Yr"\n') === null, "a header-only file yields null");
}
{
  ok(parseTenYearParYield('Date,"1 Mo","2 Mo"\n09/22/2026,3.97,4.09\n') === null,
    "a CSV missing the 10 Yr column entirely yields null rather than guessing a column");
}

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
