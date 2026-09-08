// Regression test for the stored-XSS fix in the admin desk and the
// terminal's user-identity/PDF-derived-text render sinks.
//
// The vulnerability: EMAIL_RE only forbids whitespace and "@", so an
// anonymous signup can carry HTML metacharacters in its email or display
// name, and an uploaded PDF's extracted company name is attacker-controlled
// too. Several innerHTML sinks interpolated these values with no escaping —
// in admin.html this is a real privilege-escalation path (anonymous visitor
// -> admin session takeover via localStorage exfiltration), not just
// self-XSS.
//
// This test has two layers:
//   1. Extract each file's esc() helper and prove it actually neutralises
//      a real payload (both tag-injection and attribute-breakout forms),
//      and that it round-trips harmless characters unchanged.
//   2. Statically assert every line identified during the fix still wraps
//      the user-controlled variable in esc(...) — a targeted guard against
//      silently dropping the call in a future edit.
//
//     node scripts/test_xss_escaping.js

const fs = require("fs");
const path = require("path");

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

function extractEsc(source, label) {
  const m = source.match(/const esc = \(s\) => String\(s\)\.replace\([\s\S]*?\}\[c\]\)\);/);
  if (!m) throw new Error(`esc() helper not found in ${label}`);
  // eslint-disable-next-line no-eval
  return eval(`(function () { ${m[0]} return esc; })()`);
}

const adminSrc = fs.readFileSync(path.join(__dirname, "../public/admin.html"), "utf8");
const termSrc = fs.readFileSync(path.join(__dirname, "../public/assets/terminal.js"), "utf8");

console.log("· esc() helper neutralises real payloads");
for (const [label, src] of [["admin.html", adminSrc], ["terminal.js", termSrc]]) {
  const esc = extractEsc(src, label);
  const tagPayload = "<img src=x onerror=alert(1)>";
  const escapedTag = esc(tagPayload);
  check(`${label}: tag-injection payload is neutralised`,
    !escapedTag.includes("<img") && escapedTag.includes("&lt;img"),
    `got ${escapedTag}`);

  const attrPayload = 'a@b.com" onmouseover="alert(1)';
  const escapedAttr = esc(attrPayload);
  check(`${label}: attribute-breakout quote is escaped`,
    !escapedAttr.includes('"') && escapedAttr.includes("&quot;"),
    `got ${escapedAttr}`);

  check(`${label}: harmless email round-trips losslessly through decode`,
    (() => {
      // Simulate what the browser does: set as an HTML attribute value,
      // then read back via the DOM's automatic entity-decoding — here
      // approximated by encoding then reversing the same 5-char map,
      // since no DOM is available in this plain Node test.
      const decodeMap = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
      const roundTripped = esc("o'brien&co@example.com")
        .replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g, (e) => decodeMap[e]);
      return roundTripped === "o'brien&co@example.com";
    })());
}

console.log("· every known sink still wraps its user-controlled value in esc(...)");

const adminSinks = [
  /<b>\$\{esc\(r\.email\)\}<\/b>/,
  /esc\(r\.name \|\| .—.\)/,
  /data-resetpw="\$\{esc\(r\.email\)\}"/,
  /data-revoke="\$\{esc\(r\.email\)\}"/,
  /data-grant="\$\{esc\(r\.email\)\}"/,
  /<td>\$\{esc\(c\.code\)\}<\/td>/,
];
adminSinks.forEach((re, i) => check(`admin.html sink ${i + 1}/${adminSinks.length} still escaped`,
  re.test(adminSrc), `pattern not found: ${re}`));

const terminalSinks = [
  /who\.innerHTML = `◉ USER <b>\$\{esc\(String\(u\.name\)\.toUpperCase\(\)\.slice\(0, 24\)\)\}<\/b>`/,
  /esc\(String\(u\.name \|\| u\.uid\)\.toUpperCase\(\)\.slice\(0, 24\)\)/,
  /esc\(String\(u\.name \|\| u\.uid\)\.toUpperCase\(\)\.slice\(0, 22\)\)/,
  /esc\(String\(u\.name \|\| u\.uid\)\.toUpperCase\(\)\.slice\(0, 28\)\)/,
  /\$\{list\.length\} — \$\{esc\(who\)\}/,
  /esc\(\(h\.company \|\| "UNTITLED"\)\.toUpperCase\(\)\)/,
  /REPORT — \$\{esc\(company\.toUpperCase\(\)\)\}/,
  /if \(typeof v === "string"\) return esc\(v\.toUpperCase\(\)\);/,
];
terminalSinks.forEach((re, i) => check(`terminal.js sink ${i + 1}/${terminalSinks.length} still escaped`,
  re.test(termSrc), `pattern not found: ${re}`));

console.log(`\n${passed} passed · ${failed} failed`);
if (failed > 0) process.exit(1);
