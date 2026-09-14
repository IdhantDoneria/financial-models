// SEO invariants for every public page — the things that silently rot.
//
//     node scripts/test_seo.js
//
// This exists because SEO defects don't throw. A canonical pointing at the
// wrong URL, a second <h1>, a title that grew past the SERP truncation point,
// a JSON-LD @id that references a node nobody declares, a page missing from
// the sitemap — every one of these renders perfectly in a browser and is only
// visible in a crawler's index weeks later. Each check below corresponds to a
// real defect that was actually found on this site, not a hypothetical one.
//
// Deliberately offline: no network calls, so it can run in CI. Reachability of
// EXTERNAL links is a separate concern (they can break without us changing
// anything); this asserts only what this repo controls.

const fs = require("fs");
const path = require("path");

const PUBLIC = path.join(__dirname, "..", "public");
const SITE = "https://financial-models-six.vercel.app";

let passed = 0, failed = 0;
function ok(cond, label, detail) {
  if (cond) { passed++; console.log(`  ✔ ${label}`); }
  else { failed++; console.log(`  ✘ ${label}${detail !== undefined ? " — " + detail : ""}`); }
}

const read = (f) => fs.readFileSync(path.join(PUBLIC, f), "utf8");
const htmlFiles = fs.readdirSync(PUBLIC).filter((f) => f.endsWith(".html"));

//: Pages that must NOT be indexed, and the robots directive each must carry.
//  admin is the operator console; 404 must never rank for anything.
const NOINDEX = { "admin.html": "noindex", "404.html": "noindex" };
//: Everything else is a real, indexable page and must be fully wired.
const INDEXABLE = htmlFiles.filter((f) => !(f in NOINDEX));

//: cleanUrls is on in vercel.json, so /about serves public/about.html and the
//  canonical must be the extensionless form. Index is the bare origin.
const canonicalFor = (file) =>
  file === "index.html" ? `${SITE}/` : `${SITE}/${file.replace(/\.html$/, "")}`;

/** Strip comments + script/style so checks never match commented-out markup. */
function strip(html) {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "");
}

const attr = (html, re) => { const m = html.match(re); return m ? m[1] : null; };

console.log("· Robots directives — non-public pages must be excluded");
for (const [file, directive] of Object.entries(NOINDEX)) {
  const robots = attr(read(file), /<meta\s+name="robots"\s+content="([^"]*)"/i);
  ok(robots !== null && robots.includes(directive),
    `${file} carries "${directive}"`, String(robots));
}
for (const file of INDEXABLE) {
  const robots = attr(read(file), /<meta\s+name="robots"\s+content="([^"]*)"/i);
  ok(robots === null || !/noindex/i.test(robots),
    `${file} is NOT accidentally noindexed`, String(robots));
}

console.log("\n· Titles and descriptions — present, unique, within SERP limits");
const titles = new Map();
for (const file of INDEXABLE) {
  const html = read(file);
  const title = attr(html, /<title>([\s\S]*?)<\/title>/i);
  const desc = attr(html, /<meta\s+name="description"\s+content="([^"]*)"/i);
  ok(!!title && title.trim().length > 0, `${file} has a <title>`);
  // 60 chars is where Google starts truncating; 65 leaves a little slack for
  // the em-dashes this site's titles use.
  ok(!!title && title.length <= 65, `${file} title within 65 chars`,
    title ? `${title.length}: ${title}` : "missing");
  ok(!!desc, `${file} has a meta description`);
  ok(!!desc && desc.length <= 165, `${file} description within 165 chars`,
    desc ? String(desc.length) : "missing");
  if (title) {
    ok(!titles.has(title), `${file} title is unique`, `duplicate of ${titles.get(title)}`);
    titles.set(title, file);
  }
}

console.log("\n· Canonicals must be self-referential (a wrong one de-indexes the page)");
for (const file of INDEXABLE) {
  const canon = attr(read(file), /<link\s+rel="canonical"\s+href="([^"]*)"/i);
  ok(canon === canonicalFor(file), `${file} canonical is self-referential`,
    `${canon} != ${canonicalFor(file)}`);
}

console.log("\n· Exactly one <h1> per page, and no two pages share one");
const h1s = new Map();
for (const file of htmlFiles) {
  const body = strip(read(file));
  const found = body.match(/<h1[\s>]/gi) || [];
  ok(found.length === 1, `${file} has exactly 1 <h1>`, `found ${found.length}`);
  const text = (body.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i) || [])[1];
  if (text && !(file in NOINDEX)) {
    const norm = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    ok(!h1s.has(norm), `${file} <h1> is not a duplicate`, `same as ${h1s.get(norm)}`);
    h1s.set(norm, file);
  }
}

console.log("\n· Open Graph — every indexable page needs an image for link unfurls");
for (const file of INDEXABLE) {
  const html = read(file);
  ok(/<meta\s+property="og:title"/i.test(html), `${file} has og:title`);
  ok(/<meta\s+property="og:image"/i.test(html), `${file} has og:image`);
  const img = attr(html, /<meta\s+property="og:image"\s+content="([^"]*)"/i);
  ok(!img || fs.existsSync(path.join(PUBLIC, img.replace(SITE, "").replace(/^\//, ""))),
    `${file} og:image file exists on disk`, String(img));
}

console.log("\n· JSON-LD — valid JSON, and every @id reference resolves");
const declaredIds = new Set(), referencedIds = new Set();
for (const file of htmlFiles) {
  const blocks = read(file).match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g) || [];
  blocks.forEach((b, i) => {
    const json = b.replace(/<script type="application\/ld\+json">/, "").replace(/<\/script>/, "");
    let parsed = null;
    try { parsed = JSON.parse(json); } catch (e) { /* reported below */ }
    ok(parsed !== null, `${file} JSON-LD block ${i + 1} parses`);
    if (!parsed) return;
    // A node with a @type AND an @id declares that id; a bare {"@id": ...}
    // with no @type is a reference to one declared elsewhere.
    const walk = (node) => {
      if (Array.isArray(node)) return node.forEach(walk);
      if (!node || typeof node !== "object") return;
      if (node["@id"]) (node["@type"] ? declaredIds : referencedIds).add(node["@id"]);
      Object.values(node).forEach(walk);
    };
    walk(parsed);
  });
}
for (const id of referencedIds) {
  ok(declaredIds.has(id), `@id "${id}" is declared somewhere`, "dangling graph reference");
}

console.log("\n· Internal links and assets must resolve to a real file");
const resolve = (href) => {
  const clean = href.split("#")[0].split("?")[0].replace(/^\//, "");
  if (!clean) return true;                                   // "/" -> index
  const p = path.join(PUBLIC, clean);
  return fs.existsSync(p) || fs.existsSync(p + ".html");     // cleanUrls
};
for (const file of htmlFiles) {
  const html = read(file);
  const refs = [...html.matchAll(/(?:href|src)="([^"]+)"/g)].map((m) => m[1])
    .filter((h) => !/^(https?:|mailto:|data:|tel:|#)/.test(h));
  const broken = refs.filter((h) => !resolve(h));
  ok(broken.length === 0, `${file} has no broken internal links`, broken.join(", "));
}

console.log("\n· Sitemap must cover every indexable page, and nothing else");
const sitemap = read("sitemap.xml");
const locs = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);
for (const file of INDEXABLE) {
  ok(locs.includes(canonicalFor(file)), `sitemap lists ${file}`, canonicalFor(file));
}
for (const file of Object.keys(NOINDEX)) {
  ok(!locs.includes(canonicalFor(file)), `sitemap excludes ${file}`);
}
ok(new Set(locs).size === locs.length, "sitemap has no duplicate <loc> entries");
for (const loc of locs) {
  const rel = loc.replace(SITE, "").replace(/^\//, "") || "index.html";
  ok(fs.existsSync(path.join(PUBLIC, rel)) || fs.existsSync(path.join(PUBLIC, rel + ".html")),
    `sitemap entry resolves: ${loc}`);
}

console.log("\n· robots.txt must block the console and point at the sitemap");
const robotsTxt = read("robots.txt");
ok(/Disallow:\s*\/admin/.test(robotsTxt), "robots.txt disallows /admin");
ok(/Disallow:\s*\/api\//.test(robotsTxt), "robots.txt disallows /api/");
ok(robotsTxt.includes(`Sitemap: ${SITE}/sitemap.xml`), "robots.txt names the sitemap");

console.log("\n· Landing pages must deep-link into a model the terminal knows");
const terminalJs = fs.readFileSync(path.join(PUBLIC, "assets", "terminal.js"), "utf8");
const knownMnemonics = [...terminalJs.matchAll(/mn:\s*"([A-Z0-9]+)"/g)].map((m) => m[1]);
ok(knownMnemonics.length >= 12, `terminal.js exposes >=12 mnemonics`, String(knownMnemonics.length));
for (const file of htmlFiles) {
  for (const m of read(file).matchAll(/href="\/\?m=([^"]+)"/g)) {
    ok(knownMnemonics.includes(m[1]), `${file} deep-links to a real model (?m=${m[1]})`);
  }
}

console.log(`\n${passed} passed · ${failed} failed`);
process.exit(failed ? 1 : 0);
