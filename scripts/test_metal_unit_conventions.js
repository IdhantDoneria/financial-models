// Regression test for per-country gold/silver quoting conventions in
// convertTapeQuote() (public/assets/terminal.js). Each market's convention
// was verified against its own exchange or major retail bullion dealers —
// see the comment above METAL_UNIT_CONVENTIONS for sourcing — rather than
// assumed uniform. This test proves each verified conversion is applied
// correctly, that an unverified market is deliberately left untouched, and
// that the pre-existing India behaviour (gold/10g, silver/kg) is unchanged.
//
//     node scripts/test_metal_unit_conventions.js

const fs = require("fs");
const path = require("path");

let passed = 0, failed = 0;
function ok(cond, label, detail) {
  if (cond) { passed++; console.log(`  ✔ ${label}`); }
  else { failed++; console.log(`  ✘ ${label}${detail !== undefined ? " — " + detail : ""}`); }
}

const src = fs.readFileSync(
  path.join(__dirname, "../public/assets/terminal.js"), "utf8");

// Extract TROY_OZ_TO_G, METAL_UNIT_CONVENTIONS and convertTapeQuote()
// verbatim and eval them against a minimal `state` stand-in — the same
// extraction approach test_xss_escaping.js uses for esc(), so this proves
// the actual shipped function, not a reimplementation of it.
function extract(name, re) {
  const m = src.match(re);
  if (!m) throw new Error(`${name} not found in terminal.js`);
  return m[0];
}
const troyDecl = extract("TROY_OZ_TO_G", /const TROY_OZ_TO_G = [\d.]+;/);
const convDecl = extract("METAL_UNIT_CONVENTIONS",
  /const METAL_UNIT_CONVENTIONS = \{[\s\S]*?\n\};/);
const fnDecl = extract("convertTapeQuote",
  /function convertTapeQuote\(q, country\) \{[\s\S]*?\n\}/);

const state = { ib: { fx: null } };
// eslint-disable-next-line no-eval
const convertTapeQuote = eval(
  `(function () { ${troyDecl}\n${convDecl}\n${fnDecl}\nreturn convertTapeQuote; })()`);

const TROY_OZ_TO_G = 31.1034768;
function quote(label, usdPerOz) { return { label, price: usdPerOz, money: true }; }

// Real gold ~$2,650/oz, silver ~$31/oz as of when this was written — exact
// spot doesn't matter, only that the unit arithmetic is right.
const GOLD_USD_OZ = 2650, SILVER_USD_OZ = 31;

function convert(code, ccy, fx, label, usdPerOz) {
  state.ib.fx = fx;
  return convertTapeQuote(quote(label, usdPerOz), { code, ccy });
}

console.log("· India (pre-existing behaviour, must be unchanged)");
{
  const g = convert("IN", "INR", 88, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD (10G)", "gold relabelled 10G", g.label);
  ok(Math.abs(g.price - (GOLD_USD_OZ * 88 / TROY_OZ_TO_G) * 10) < 0.01,
    "gold price = USD/oz * fx / troy-oz-to-g * 10", g.price);
  const s = convert("IN", "INR", 88, "SILVER", SILVER_USD_OZ);
  ok(s.label === "SILVER (KG)", "silver relabelled KG", s.label);
  ok(Math.abs(s.price - (SILVER_USD_OZ * 88 / TROY_OZ_TO_G) * 1000) < 0.01,
    "silver price = USD/oz * fx / troy-oz-to-g * 1000", s.price);
}

console.log("· China: gold per gram, silver per KILOGRAM (SGE benchmark units)");
{
  const g = convert("CN", "CNY", 7.1, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD (G)", "gold relabelled G", g.label);
  ok(Math.abs(g.price - (GOLD_USD_OZ * 7.1 / TROY_OZ_TO_G)) < 0.01,
    "gold price = USD/oz * fx / troy-oz-to-g (1 gram)", g.price);
  const s = convert("CN", "CNY", 7.1, "SILVER", SILVER_USD_OZ);
  ok(s.label === "SILVER (KG)", "silver relabelled KG (matches SGE, not gold's gram)", s.label);
  ok(Math.abs(s.price - (SILVER_USD_OZ * 7.1 / TROY_OZ_TO_G) * 1000) < 0.01,
    "silver price uses the kilogram factor", s.price);
}

console.log("· Hong Kong: gold per tael (37.429g); silver deliberately NOT converted");
{
  const g = convert("HK", "HKD", 7.8, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD (TAEL)", "gold relabelled TAEL", g.label);
  ok(Math.abs(g.price - (GOLD_USD_OZ * 7.8 / TROY_OZ_TO_G) * 37.429) < 0.01,
    "gold price uses the CGSE 37.429g tael, not the generic 37.799g figure", g.price);
  const s = convert("HK", "HKD", 7.8, "SILVER", SILVER_USD_OZ);
  ok(s.label === "SILVER", "silver label untouched (no confirmed local convention)", s.label);
  ok(Math.abs(s.price - SILVER_USD_OZ * 7.8) < 0.01,
    "silver price stays in troy ounces, just FX-converted", s.price);
}

console.log("· South Korea: gold per don (3.75g), silver per gram — NOT the same unit");
{
  const g = convert("KR", "KRW", 1390, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD (DON)", "gold relabelled DON", g.label);
  ok(Math.abs(g.price - (GOLD_USD_OZ * 1390 / TROY_OZ_TO_G) * 3.75) < 1,
    "gold price uses the 3.75g don, not silver's 1g", g.price);
  const s = convert("KR", "KRW", 1390, "SILVER", SILVER_USD_OZ);
  ok(s.label === "SILVER (G)", "silver relabelled G (diverges from gold's DON)", s.label);
  ok(Math.abs(s.price - (SILVER_USD_OZ * 1390 / TROY_OZ_TO_G)) < 1,
    "silver price uses the 1g factor, not 3.75g", s.price);
}

console.log("· Eurozone/Swiss retail bullion markets: gold AND silver both per gram");
for (const [code, ccy, fx] of [["DE", "EUR", 0.92], ["FR", "EUR", 0.92],
                                 ["NL", "EUR", 0.92], ["CH", "CHF", 0.88]]) {
  const g = convert(code, ccy, fx, "GOLD", GOLD_USD_OZ);
  const s = convert(code, ccy, fx, "SILVER", SILVER_USD_OZ);
  ok(g.label === "GOLD (G)" && s.label === "SILVER (G)",
    `${code}: both metals relabelled (G)`, `${g.label} / ${s.label}`);
}

console.log("· Markets confirmed as troy-ounce already (UK, Canada, Australia): untouched");
for (const [code, ccy, fx] of [["GB", "GBP", 0.79], ["CA", "CAD", 1.39], ["AU", "AUD", 1.53]]) {
  const g = convert(code, ccy, fx, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD" && Math.abs(g.price - GOLD_USD_OZ * fx) < 0.01,
    `${code}: gold stays troy oz, only FX-converted`, `${g.label} ${g.price}`);
}

console.log("· Taiwan: deliberately left on troy oz — convention genuinely ambiguous, not guessed");
{
  const g = convert("TW", "TWD", 32, "GOLD", GOLD_USD_OZ);
  ok(g.label === "GOLD" && Math.abs(g.price - GOLD_USD_OZ * 32) < 0.01,
    "TW gold stays troy oz (no entry in METAL_UNIT_CONVENTIONS)", `${g.label} ${g.price}`);
}

console.log("· Non-metal quotes and index levels are never touched by the metal table");
{
  state.ib.fx = 88;
  const btc = convertTapeQuote({ label: "BITCOIN", price: 65000, money: true }, { code: "IN", ccy: "INR" });
  ok(btc.label === "BITCOIN" && Math.abs(btc.price - 65000 * 88) < 0.01,
    "BITCOIN is FX-converted but never unit-converted", btc.price);
  const spx = convertTapeQuote({ label: "S&P 500", price: 5800, money: false }, { code: "IN", ccy: "INR" });
  ok(spx.price === 5800, "an index level (money: false) is never FX-multiplied", spx.price);
}

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
