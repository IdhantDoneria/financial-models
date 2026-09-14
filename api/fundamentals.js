// api/fundamentals.js — ticker → company financials, for the IB desk.
//
//   GET /api/fundamentals?ticker=AAPL
//     -> { ok, ticker, cik, company_name, fiscal_year, currency, fields{...},
//          sources[], missing[], notes[] }
//
// WHY THIS EXISTS
// ---------------
// The IB desk could only start from an uploaded 10-K/10-Q PDF, which means a
// visitor had to *already possess a filing* before the product could show them
// anything. That's the single biggest drop-off in the funnel: "go find a 10-K"
// is a much larger ask than "type a ticker".
//
// WHY SEC EDGAR AND NOT YAHOO
// ---------------------------
// Yahoo's quoteSummary endpoint (the usual shortcut for fundamentals) is now
// crumb-gated — it answers `{"error":{"code":"Unauthorized","description":
// "Invalid Crumb"}}` without a cookie/crumb handshake, and it is an
// undocumented private endpoint that can change without notice. Building the
// product's core data path on that would be a launch-day outage waiting to
// happen. EDGAR is the primary source those aggregators themselves scrape:
// official, keyless, documented, and legally obliged to stay up.
//
// Yahoo's *chart* endpoint is still used for the live share price only — it is
// keyless, unauthenticated, and is the same call api/quotes.js already relies
// on for the ticker tape. A price is also the one figure EDGAR cannot give us
// (it's a market fact, not a disclosure), and if it's unavailable the field is
// reported missing rather than guessed.
//
// SCALE: EDGAR reports raw currency units (Apple's FY25 revenue is
// 416161000000, not 416161). src/pipeline/pdf_extractor.py normalises scraped
// PDF figures to "a canonical dollar amount (not millions)" too, so the two
// paths agree and NOTHING is rescaled here. Changing either side without the
// other would silently misvalue every company by 1e6.
//
// LIMITATION, STATED LOUDLY: EDGAR only covers SEC registrants — US-listed
// companies. A non-US ticker gets an explicit 404 telling the caller to upload
// the filing instead. It must never fall back to a guess.

const SEC_UA = process.env.SEC_USER_AGENT
  || "FINMODELS Terminal finmodels10@gmail.com";
const TICKERS_URL = "https://www.sec.gov/files/company_tickers.json";
const FACTS_URL = (cik) => `https://data.sec.gov/api/xbrl/companyfacts/CIK${cik}.json`;

//: The ticker→CIK map is ~1 MB and changes rarely. Cached per warm lambda so a
//  burst of lookups costs SEC one fetch, not one per request.
let tickerMap = null;
let tickerMapAt = 0;
const TICKER_TTL = 24 * 3600 * 1000;

const secFetch = (url) =>
  fetch(url, { headers: { "User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate" },
               signal: AbortSignal.timeout(12_000) });

async function loadTickerMap() {
  if (tickerMap && Date.now() - tickerMapAt < TICKER_TTL) return tickerMap;
  const r = await secFetch(TICKERS_URL);
  if (!r.ok) throw new Error(`SEC ticker map ${r.status}`);
  const raw = await r.json();
  const map = new Map();
  for (const row of Object.values(raw)) {
    // CIK must be zero-padded to 10 digits for the companyfacts path.
    map.set(String(row.ticker).toUpperCase(),
            { cik: String(row.cik_str).padStart(10, "0"), title: row.title });
  }
  tickerMap = map; tickerMapAt = Date.now();
  return map;
}

/**
 * Resolve a user-typed ticker against SEC's directory.
 *
 * Share classes are the trap here. SEC writes them with a HYPHEN (BRK-B,
 * BF-B, MOG-A — 543 of them), while Yahoo, Bloomberg and essentially every
 * finance site a user will have copied from write a DOT (BRK.B). Looking up
 * only what was typed means "BRK.B" — one of the most-searched tickers there
 * is — reports "not an SEC registrant", which is both wrong and exactly the
 * kind of failure a new user reads as "this product is broken".
 *
 * Returns the matched entry plus the SYMBOL THAT MATCHED, because the price
 * lookup downstream needs SEC's spelling too, not the user's.
 */
function resolveTicker(map, raw) {
  for (const candidate of [raw, raw.replace(/\./g, "-"), raw.replace(/-/g, ".")]) {
    const hit = map.get(candidate);
    if (hit) return { ...hit, symbol: candidate };
  }
  return null;
}

/* ------------------------------ XBRL picking ----------------------------- *
 * Companies tag the same economic concept differently (and change tags between
 * years), so every concept below is a CASCADE of candidate tags tried in order
 * — the same shape as the PDF extractor's PyMuPDF→pdfplumber→pypdf backend
 * cascade, for the same reason: one source is never enough on real filings. */

//: Duration (flow) concepts — income statement and cash flow.
const FLOW_TAGS = {
  revenue: ["RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"],
  net_income: ["NetIncomeLoss", "ProfitLoss",
               "NetIncomeLossAvailableToCommonStockholdersBasic"],
  operating_cash_flow: ["NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
  capital_expenditures: ["PaymentsToAcquirePropertyPlantAndEquipment",
                         "PaymentsToAcquireProductiveAssets"],
  depreciation_amortization: ["DepreciationDepletionAndAmortization",
                              "DepreciationAmortizationAndAccretionNet",
                              "DepreciationAndAmortization", "Depreciation"],
  rd_expense: ["ResearchAndDevelopmentExpense"],
  interest_expense: ["InterestExpense", "InterestExpenseDebt",
                     "InterestIncomeExpenseNet"],
  income_tax_expense: ["IncomeTaxExpenseBenefit"],
  pretax_income: ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                  "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
};

//: Instant (stock) concepts — balance sheet.
const STOCK_TAGS = {
  cash_and_equivalents: ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
  short_term_investments: ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                           "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
  long_term_debt: ["LongTermDebtNoncurrent", "LongTermDebt"],
  short_term_debt: ["LongTermDebtCurrent", "DebtCurrent",
                    "ShortTermBorrowings", "OtherShortTermBorrowings"],
  shares_outstanding: ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
};

/** Latest non-null numeric, restatement-aware. */
function pickInstant(facts, tags) {
  for (const tag of tags) {
    const f = facts[tag];
    if (!f) continue;
    const unit = Object.keys(f.units).find((u) => u === "USD" || u === "shares")
              || Object.keys(f.units)[0];
    const rows = (f.units[unit] || []).filter((r) => typeof r.val === "number");
    if (!rows.length) continue;
    // Two rows can share an `end` when a later filing restates it. Sort by end,
    // then by `filed`, and take the last — i.e. the most recently filed view of
    // the most recent period.
    rows.sort((a, b) => (a.end < b.end ? -1 : a.end > b.end ? 1
                        : (a.filed || "") < (b.filed || "") ? -1 : 1));
    const last = rows[rows.length - 1];
    return { value: last.val, end: last.end, tag, unit };
  }
  return null;
}

/**
 * Annual series for a flow concept, newest last.
 *
 * `fy`/`fp` describe the FILING, not the period — an FY2025 10-K restates
 * FY2024 figures under fy:2025 — so periods are identified by their own
 * start/end span instead: a ~365-day duration is an annual period. Filtering
 * on fy would double-count restatements and silently corrupt the FCF series.
 */
function pickAnnualSeries(facts, tags, maxYears = 6) {
  for (const tag of tags) {
    const f = facts[tag];
    if (!f) continue;
    const unit = Object.keys(f.units).find((u) => u === "USD") || Object.keys(f.units)[0];
    const rows = (f.units[unit] || []).filter((r) => {
      if (typeof r.val !== "number" || !r.start || !r.end) return false;
      const days = (Date.parse(r.end) - Date.parse(r.start)) / 86_400_000;
      return days >= 340 && days <= 400;          // a fiscal year, however offset
    });
    if (!rows.length) continue;
    // Collapse restatements: one entry per period-end, the latest filed wins.
    const byEnd = new Map();
    for (const r of rows) {
      const prev = byEnd.get(r.end);
      if (!prev || (r.filed || "") > (prev.filed || "")) byEnd.set(r.end, r);
    }
    const series = [...byEnd.values()].sort((a, b) => (a.end < b.end ? -1 : 1));
    if (series.length) return { series: series.slice(-maxYears), tag, unit };
  }
  return null;
}

/** Live share price — the one figure EDGAR structurally cannot provide. */
async function fetchPrice(ticker) {
  try {
    const r = await fetch(
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1d&range=1d`,
      { headers: { "User-Agent": "Mozilla/5.0 (compatible; FinModelsTerminal/1.0)" },
        signal: AbortSignal.timeout(8000) });
    if (!r.ok) return null;
    const j = await r.json();
    const meta = j?.chart?.result?.[0]?.meta;
    const px = meta?.regularMarketPrice;
    return typeof px === "number" && px > 0
      ? { price: px, currency: meta.currency || "USD" } : null;
  } catch { return null; }
}

module.exports = async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  // Fundamentals change quarterly at most; cache hard at the CDN so SEC sees
  // roughly one request per ticker per hour no matter how many visitors.
  res.setHeader("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400");

  //: Parsed from req.url rather than req.query — same as api/rates.js and
  //  api/quotes.js. Vercel populates req.query but the local dev server
  //  (scripts/dev_auth_server.js) hands over a raw Node request, and an
  //  endpoint that only works in production is an endpoint nobody tests.
  const url = new URL(req.url || "/", "http://internal");
  const ticker = String(url.searchParams.get("ticker") || "").trim().toUpperCase();
  if (!/^[A-Z0-9.\-]{1,12}$/.test(ticker)) {
    return res.status(400).json({ ok: false, error: "PASS A TICKER, e.g. ?ticker=AAPL" });
  }

  let entry;
  try {
    entry = resolveTicker(await loadTickerMap(), ticker);
  } catch (err) {
    return res.status(502).json({ ok: false, error: "SEC ticker directory unreachable — try again, or upload the filing." });
  }
  if (!entry) {
    // Explicit and honest: this is the US-only boundary. Never guess past it.
    return res.status(404).json({
      ok: false, notFound: true,
      error: `"${ticker}" is not an SEC registrant. EDGAR covers US-listed companies only — upload the filing as a PDF instead.`,
    });
  }

  let facts, entityName;
  try {
    const r = await secFetch(FACTS_URL(entry.cik));
    if (!r.ok) throw new Error(`companyfacts ${r.status}`);
    const j = await r.json();
    entityName = j.entityName || entry.title;
    facts = (j.facts && j.facts["us-gaap"]) || null;
    if (!facts) throw new Error("no us-gaap facts");
  } catch (err) {
    return res.status(502).json({
      ok: false,
      error: `SEC filing data unavailable for ${ticker} (${String(err.message).slice(0, 80)}) — upload the filing as a PDF instead.`,
    });
  }

  const notes = [];
  const flows = {}, stocks = {};
  for (const [k, tags] of Object.entries(FLOW_TAGS)) flows[k] = pickAnnualSeries(facts, tags);
  for (const [k, tags] of Object.entries(STOCK_TAGS)) stocks[k] = pickInstant(facts, tags);

  const latestFlow = (k) => {
    const s = flows[k];
    return s && s.series.length ? s.series[s.series.length - 1].val : null;
  };

  // --- free cash flow series: operating cash flow − capex, year by year ----
  // Built by aligning on period END rather than by index: a company can report
  // OCF for a year it didn't report capex, and zipping positionally would
  // silently pair mismatched years.
  let freeCashFlows = [];
  if (flows.operating_cash_flow) {
    const capexByEnd = new Map(
      (flows.capital_expenditures?.series || []).map((r) => [r.end, r.val]));
    freeCashFlows = flows.operating_cash_flow.series.map((r) => {
      const capex = capexByEnd.get(r.end);
      // Capex is reported as a positive outflow in the cash-flow statement.
      return capex == null ? null : r.val - Math.abs(capex);
    }).filter((v) => v !== null);
    if (flows.capital_expenditures
        && freeCashFlows.length < flows.operating_cash_flow.series.length) {
      notes.push("Some years had operating cash flow but no matching capital-expenditure disclosure; those years are omitted from the FCF series rather than assumed zero.");
    }
    if (!flows.capital_expenditures) {
      freeCashFlows = [];
      notes.push("No capital-expenditure tag found, so free cash flow could not be derived from operating cash flow.");
    }
  }

  // --- debt and cash ------------------------------------------------------
  const ltd = stocks.long_term_debt?.value ?? null;
  const std = stocks.short_term_debt?.value ?? null;
  const totalDebt = ltd === null && std === null ? null : (ltd || 0) + (std || 0);
  if (totalDebt !== null && (ltd === null || std === null)) {
    notes.push("Total debt combines only the debt components this company tags; a missing current- or non-current-debt tag means the figure may understate total borrowings.");
  }

  const cash = stocks.cash_and_equivalents?.value ?? null;
  const sti = stocks.short_term_investments?.value ?? null;

  // --- derived ratios -----------------------------------------------------
  const revenue = latestFlow("revenue");
  const netIncome = latestFlow("net_income");
  const tax = latestFlow("income_tax_expense");
  const pretax = latestFlow("pretax_income");
  //: Effective rate only when the denominator is a real positive profit —
  //  a loss-making year produces a meaningless (often negative) rate.
  const taxRate = tax != null && pretax != null && pretax > 0
    ? Math.min(0.60, Math.max(0, tax / pretax)) : null;

  //: Revenue CAGR from the actual series, not a single year-over-year jump.
  let revenueGrowth = null;
  const revSeries = flows.revenue?.series || [];
  if (revSeries.length >= 2) {
    const first = revSeries[0].val, last = revSeries[revSeries.length - 1].val;
    const yrs = revSeries.length - 1;
    if (first > 0 && last > 0) revenueGrowth = Math.pow(last / first, 1 / yrs) - 1;
  }

  //: Operating margin proxied from net income when no operating-income tag is
  //  picked. Labelled in `notes` so it is never mistaken for a true operating
  //  margin — the pipeline's own sector fallback may well be the better figure.
  let operatingMargin = null;
  if (revenue && netIncome != null && revenue > 0) {
    operatingMargin = netIncome / revenue;
    notes.push("Operating margin shown is a net-income margin derived from the income statement, not a tagged operating margin.");
  }

  //: entry.symbol, not the raw input — Yahoo also spells share classes
  //  with a hyphen, so a user's "BRK.B" must become "BRK-B" here too.
  const priceInfo = await fetchPrice(entry.symbol);
  if (!priceInfo) {
    notes.push("Live share price unavailable; enter it manually or the models will use their documented fallback.");
  }

  const latestEnd = revSeries.length ? revSeries[revSeries.length - 1].end
                                     : (stocks.cash_and_equivalents?.end || null);

  //: Field names mirror ExtractedFinancials exactly (see
  //  src/pipeline/pdf_extractor.py) so web_bridge.load_fundamentals() can
  //  rehydrate them without a second translation layer.
  const fields = {
    company_name: entityName,
    ticker: entry.symbol,
    fiscal_year: latestEnd ? Number(latestEnd.slice(0, 4)) : null,
    revenue,
    free_cash_flows: freeCashFlows,
    net_income: netIncome,
    total_debt: totalDebt,
    cash_and_equivalents: cash === null && sti === null ? null : (cash || 0) + (sti || 0),
    shares_outstanding: stocks.shares_outstanding?.value ?? null,
    current_price: priceInfo ? priceInfo.price : null,
    depreciation_amortization: latestFlow("depreciation_amortization"),
    rd_expense: latestFlow("rd_expense"),
    capital_expenditures: (() => {
      const v = latestFlow("capital_expenditures");
      return v == null ? null : Math.abs(v);
    })(),
    interest_expense: (() => {
      const v = latestFlow("interest_expense");
      return v == null ? null : Math.abs(v);
    })(),
    tax_rate: taxRate,
    revenue_growth: revenueGrowth,
    operating_margin: operatingMargin,
    currency: priceInfo ? priceInfo.currency : "USD",
    statement_basis: "annual",
    //: Provenance. assumptions.py reads this to decide whether a figure may be
    //  described as coming from a filing, and WHICH kind — it must never claim
    //  "scraped from PDF" for an XBRL fact.
    backends_used: ["sec-edgar-xbrl"],
  };

  if (cash !== null && sti !== null) {
    notes.push("Cash includes short-term investments, consistent with how net debt is normally computed.");
  }

  const missing = Object.entries(fields)
    .filter(([k, v]) => v === null || (Array.isArray(v) && v.length === 0))
    .map(([k]) => k);

  return res.status(200).json({
    ok: true,
    ticker: entry.symbol,
    cik: entry.cik,
    company_name: entityName,
    fiscal_year: fields.fiscal_year,
    currency: fields.currency,
    period_end: latestEnd,
    fields,
    missing,
    notes,
    sources: [
      { name: "SEC EDGAR XBRL companyfacts", url: `https://data.sec.gov/api/xbrl/companyfacts/CIK${entry.cik}.json` },
      ...(priceInfo ? [{ name: "Share price", url: "Yahoo Finance chart API" }] : []),
    ],
  });
};

//: Pure helpers exported for scripts/test_fundamentals.js. They carry the
//  parts that are easy to get subtly wrong (share-class spelling, restatement
//  dedup, period alignment) and impossible to check by eyeballing a live
//  response, so they are tested directly rather than only through the handler.
module.exports._internals = { resolveTicker, pickInstant, pickAnnualSeries, FLOW_TAGS, STOCK_TAGS };
