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
  //: Dividends are the sharpest annual-vs-quarterly trap in this file. Apple
  //  tags CommonStockDividendsPerShareDeclared 61 times at annual length AND
  //  187 times at quarterly length in the same series; taking "the latest
  //  row" yields $0.27 (one quarter) instead of $1.02 (FY2025) — a silent 4x
  //  understatement that still looks like a perfectly ordinary dividend.
  //  pickAnnualSeries()'s ~365-day duration filter is what makes this safe,
  //  which is exactly why period length, not recency, decides.
  dividends_per_share: ["CommonStockDividendsPerShareDeclared",
                        "CommonStockDividendsPerShareCashPaid"],
  pretax_income: ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                  "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
};

/* ------------------------------- IFRS filers ------------------------------ *
 * A foreign private issuer files a 20-F under the `ifrs-full` taxonomy, not
 * `us-gaap` — Infosys, Wipro, HDFC Bank and ICICI all do. They are genuine SEC
 * registrants with real, current filings; reading only `us-gaap` reported
 * "no filing data" for every one of them, which is both wrong and exactly the
 * set of companies most relevant to this project's India angle.
 *
 * These filings are denominated in USD (the 20-F reports in USD), and the ADR
 * price fetched alongside is USD too, so the two are coherent. The ADR RATIO
 * is the caveat that cannot be resolved from EDGAR — see the note attached in
 * the handler.                                                              */
const IFRS_FLOW_TAGS = {
  revenue: ["RevenueFromContractsWithCustomers", "Revenue"],
  net_income: ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
  operating_cash_flow: ["CashFlowsFromUsedInOperatingActivities"],
  capital_expenditures: ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                         "PurchaseOfPropertyPlantAndEquipment"],
  depreciation_amortization: ["DepreciationAndAmortisationExpense",
                              "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"],
  rd_expense: ["ResearchAndDevelopmentExpense"],
  interest_expense: ["InterestExpense", "FinanceCosts"],
  income_tax_expense: ["IncomeTaxExpenseContinuingOperations"],
  dividends_per_share: ["DividendsPaidOrdinarySharePerShare", "DividendsRecognisedAsDistributionsToOwnersOfParentPerShare"],
  pretax_income: ["ProfitLossBeforeTax"],
};

const IFRS_STOCK_TAGS = {
  cash_and_equivalents: ["CashAndCashEquivalents"],
  short_term_investments: ["OtherCurrentFinancialAssets", "CurrentInvestments"],
  long_term_debt: ["NoncurrentPortionOfNoncurrentBorrowings", "Borrowings"],
  short_term_debt: ["CurrentPortionOfNoncurrentBorrowings", "ShorttermBorrowings"],
  shares_outstanding: ["NumberOfSharesOutstanding", "NumberOfSharesIssuedAndFullyPaid"],
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

/**
 * The currency a filing actually reports in.
 *
 * Read off the revenue tags, which every operating company has. USD is
 * preferred when offered so that the USD ADR price stays comparable with the
 * financials; otherwise the filer's own currency is returned and the caller
 * must not attach a price denominated in anything else.
 */
function detectReportingCurrency(facts, revenueTags) {
  const seen = [];
  for (const tag of revenueTags) {
    const f = facts[tag];
    if (!f) continue;
    for (const unit of Object.keys(f.units)) {
      if (/^[A-Z]{3}$/.test(unit) && !seen.includes(unit)) seen.push(unit);
    }
  }
  if (!seen.length) return null;
  return seen.includes("USD") ? "USD" : seen[0];
}

/** Latest non-null numeric, restatement-aware. */
function pickInstant(facts, tags, prefer = null) {
  for (const tag of tags) {
    const f = facts[tag];
    if (!f) continue;
    //: Same hard constraint as pickAnnualSeries — see the note there.
    let unit;
    if (prefer) {
      if (!f.units[prefer]) continue;
      unit = prefer;
    } else {
      unit = Object.keys(f.units).find((u) => u === "USD" || u === "shares")
          || Object.keys(f.units)[0];
    }
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
function pickAnnualSeries(facts, tags, maxYears = 6, prefer = null) {
  const candidates = [];
  for (let rank = 0; rank < tags.length; rank++) {
    const f = facts[tags[rank]];
    if (!f) continue;
    //: `prefer` pins ONE reporting currency across every concept, and is a
    //  HARD constraint, not a hint. Wipro tags
    //  RevenueFromContractsWithCustomers in INR only while Revenue carries
    //  both INR and USD; falling back to "whatever unit this tag has" yielded
    //  ₹890bn of revenue labelled $890bn — an 82x error that reads as a
    //  perfectly ordinary large number. A tag that cannot answer in the
    //  filing's reporting currency is SKIPPED, so the concept comes back
    //  missing (and is reported as missing) rather than in the wrong unit.
    let unit;
    if (prefer) {
      //: A per-share amount is denominated "<CCY>/shares", not "<CCY>" —
      //  dividends per share are tagged USD/shares. Matching the pinned
      //  currency exactly and nothing else silently dropped every dividend.
      //  Both forms are accepted, and only for the pinned currency: INR/shares
      //  is still rejected when USD is pinned, which is the point.
      unit = [prefer, `${prefer}/shares`].find((u) => f.units[u]);
      if (!unit) continue;
    } else {
      unit = Object.keys(f.units).find((u) => u === "USD")
          || Object.keys(f.units).find((u) => u.startsWith("USD"))
          || Object.keys(f.units)[0];
    }
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
    if (series.length) {
      candidates.push({ series: series.slice(-maxYears), tag: tags[rank], unit, rank,
                        latest: series[series.length - 1].end });
    }
  }
  if (!candidates.length) return null;
  //: Freshest tag wins, NOT the first one in the cascade.
  //
  //  Companies migrate tags and abandon the old one. Infosys tagged `Revenue`
  //  until IFRS 15 in 2018 and `RevenueFromContractsWithCustomers` after, so
  //  a first-match cascade returned FY2018's $10.9bn as though it were
  //  current — seven-year-old revenue, in the right shape, with nothing to
  //  signal it was stale. us-gaap filers do the same thing across ASC 606.
  //  Ties on fiscal year fall back to cascade order, so the preferred tag
  //  still wins when both are equally current and a single stray datapoint
  //  in a less-preferred tag can't hijack the result.
  candidates.sort((a, b) => {
    const ya = a.latest.slice(0, 4), yb = b.latest.slice(0, 4);
    if (ya !== yb) return ya < yb ? 1 : -1;
    return a.rank - b.rank;
  });
  const best = candidates[0];
  return { series: best.series, tag: best.tag, unit: best.unit };
}

/* --------------------------------- beta ---------------------------------- *
 * Beta is NOT an XBRL concept and never will be. Searching Apple's 503
 * us-gaap tags for "beta" returns nothing, because beta is not a disclosure —
 * it is a statistic about how a stock's returns co-move with its market.
 * Companies don't file it; data vendors compute it.
 *
 * So it is computed here, the way it is defined: an OLS regression of the
 * stock's periodic returns on the market index's, over five years of monthly
 * observations — the same window Bloomberg's default beta uses. That is a
 * real, checkable figure and strictly better than the sector-median fallback
 * the pipeline would otherwise apply.
 *
 * The alternative (scraping a vendor's precomputed beta) would reintroduce
 * exactly the dependency on an undocumented private endpoint that this file's
 * header rejects for fundamentals.                                          */

//: The index each market's beta is measured against. Beta is meaningless
//: without naming its benchmark — a stock's beta vs the Nifty and vs the S&P
//: are different numbers, and reporting one as the other is a wrong answer
//: that looks entirely normal.
const BENCHMARKS = {
  ".NS": { symbol: "^NSEI", name: "NIFTY 50" },
  ".BO": { symbol: "^BSESN", name: "BSE SENSEX" },
  "": { symbol: "^GSPC", name: "S&P 500" },
};

function benchmarkFor(symbol) {
  for (const suffix of [".NS", ".BO"]) {
    if (symbol.toUpperCase().endsWith(suffix)) return BENCHMARKS[suffix];
  }
  return BENCHMARKS[""];
}

/** Monthly closes for a symbol, as {t: unixSeconds, c: close} sorted by time. */
async function fetchMonthlyCloses(symbol, range = "5y") {
  const r = await fetch(
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}`
      + `?interval=1mo&range=${range}`,
    { headers: { "User-Agent": "Mozilla/5.0 (compatible; FinModelsTerminal/1.0)" },
      signal: AbortSignal.timeout(10_000) });
  if (!r.ok) return null;
  const j = await r.json();
  const res = j?.chart?.result?.[0];
  if (!res || !Array.isArray(res.timestamp)) return null;
  //: Adjusted close where available — an unadjusted series treats a stock
  //  split as a -50% month, which would corrupt every return after it.
  const adj = res.indicators?.adjclose?.[0]?.adjclose;
  const close = adj || res.indicators?.quote?.[0]?.close;
  if (!Array.isArray(close)) return null;
  const out = [];
  for (let i = 0; i < res.timestamp.length; i++) {
    const c = close[i];
    if (typeof c === "number" && c > 0) out.push({ t: res.timestamp[i], c });
  }
  return out.sort((a, b) => a.t - b.t);
}

//: Monthly bars can be stamped a day or two apart between two symbols, so
//  they're bucketed to their calendar month before joining. Zipping the two
//  arrays positionally instead would silently pair a stock's March with the
//  index's April the moment one series is missing a month.
const monthKey = (unixSeconds) => {
  const d = new Date(unixSeconds * 1000);
  return d.getUTCFullYear() * 12 + d.getUTCMonth();
};

/** Simple returns from a close series, keyed by the month they END in. */
function monthlyReturns(series) {
  const byMonth = new Map();
  for (let i = 1; i < series.length; i++) {
    const prev = series[i - 1].c, cur = series[i].c;
    if (prev > 0) byMonth.set(monthKey(series[i].t), cur / prev - 1);
  }
  return byMonth;
}

//: A beta needs enough observations to mean anything. 24 monthly points is
//  the floor; below that the estimate swings wildly on one outlier month, and
//  returning a confidently-wrong 2.4 is worse than returning nothing and
//  letting the documented sector median apply.
const MIN_BETA_OBSERVATIONS = 24;

async function computeBeta(symbol) {
  const bench = benchmarkFor(symbol);
  const [stock, market] = await Promise.all([
    fetchMonthlyCloses(symbol).catch(() => null),
    fetchMonthlyCloses(bench.symbol).catch(() => null),
  ]);
  if (!stock || !market) return null;

  const rs = monthlyReturns(stock), rm = monthlyReturns(market);
  const xs = [], ys = [];
  for (const [m, sr] of rs) {
    const mr = rm.get(m);
    if (typeof mr === "number") { ys.push(sr); xs.push(mr); }
  }
  if (xs.length < MIN_BETA_OBSERVATIONS) return null;

  const n = xs.length;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let cov = 0, varm = 0;
  for (let i = 0; i < n; i++) {
    cov += (xs[i] - mx) * (ys[i] - my);
    varm += (xs[i] - mx) ** 2;
  }
  if (varm <= 0) return null;                 // a flat market has no beta
  const beta = cov / varm;
  //: A beta outside this range is almost always a data artifact (a bad
  //  split adjustment, a near-dead ticker), not a real risk profile. Reject
  //  rather than pass a number the DCF would take at face value.
  if (!Number.isFinite(beta) || beta < -3 || beta > 5) return null;
  return { beta, observations: n, benchmark: bench.name, benchmarkSymbol: bench.symbol };
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

/* --------------------------- non-US: market data only --------------------- *
 * Outside EDGAR's coverage this endpoint deliberately returns LESS, and says
 * so, rather than reaching for a scraped fundamentals source. What it can
 * still supply honestly is the two figures that are market facts rather than
 * disclosures — the current price and a computed beta — and those are exactly
 * the two an uploaded filing cannot give you, because they aren't in it.
 *
 * So for an Indian user the flow is: type the ticker to pull price and beta,
 * upload the annual report for the financials. That is a genuine improvement
 * on today (both of those were manual) without pretending to a data source
 * this project does not have.                                               */

//: Exchange suffixes to try for a bare symbol, mirroring api/quotes.js's
//  resolveQuote(). Kept deliberately parallel to that function rather than
//  shared, because quotes.js runs on a different request path with its own
//  rate limiting; the comment there records why suffix resolution must never
//  be gated on the UI's selected market.
const MARKET_SUFFIXES = ["", ".NS", ".BO"];

async function marketOnly(ticker) {
  for (const suffix of MARKET_SUFFIXES) {
    const symbol = ticker + suffix;
    const price = await fetchPrice(symbol);
    if (!price) continue;
    const betaInfo = await computeBeta(symbol).catch(() => null);
    const notes = [
      "Only market data is available for this listing: SEC EDGAR covers US registrants, "
      + "and there is no equivalent free, official filings API for this market.",
      "Upload the company's annual report as a PDF to extract revenue, cash flows, debt "
      + "and the rest — the extractor handles Ind AS filings and lakh/crore scaling.",
    ];
    if (betaInfo) {
      notes.push(`Beta ${betaInfo.beta.toFixed(2)} is computed by regressing `
        + `${betaInfo.observations} monthly returns against the ${betaInfo.benchmark}.`);
    }
    return {
      ok: true,
      partial: true,                 // the UI must not present this as a full extraction
      ticker: symbol,
      company_name: null,
      fiscal_year: null,
      currency: price.currency,
      period_end: null,
      fields: {
        ticker: symbol,
        current_price: price.price,
        beta: betaInfo ? Number(betaInfo.beta.toFixed(3)) : null,
        currency: price.currency,
        free_cash_flows: [],
        backends_used: ["market-data"],
      },
      //: Everything a full extraction would have carried is named here, so
      //  the caller reports real gaps instead of inferring them from absence.
      missing: ["company_name", "revenue", "free_cash_flows", "net_income", "total_debt",
                "cash_and_equivalents", "shares_outstanding", "dividend_per_share",
                "revenue_growth", "operating_margin", "tax_rate",
                "depreciation_amortization", "rd_expense", "capital_expenditures"],
      notes,
      beta: betaInfo || null,
      sources: [
        { name: "Share price", url: "Yahoo Finance chart API" },
        ...(betaInfo ? [{ name: `Beta vs ${betaInfo.benchmark} (${betaInfo.observations} monthly returns)`,
                          url: "Computed by OLS from price history" }] : []),
      ],
    };
  }
  return null;
}

module.exports = async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  // Fundamentals change quarterly at most, so the CDN caches hard and SEC sees
  // roughly one request per ticker per hour however many visitors there are.
  //
  // `max-age=0` is the important half: without it the BROWSER also caches
  // (heuristically, since only s-maxage was set), and this payload carries a
  // live share price alongside the annual figures — a visitor would have been
  // served an hour-old quote, and re-loading the ticker after a correction
  // would keep returning the stale body. The shared cache still absorbs the
  // load; only the client is forced to revalidate.
  res.setHeader("Cache-Control", "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400");

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
    //: Not an SEC registrant. Before giving up, see whether it's a listed
    //  security we can still supply MARKET data for — price and beta — which
    //  is the whole of what we can honestly offer outside the US.
    //
    //  Why there is no non-US fundamentals path: there is no durable keyless
    //  equivalent of EDGAR. NSE's API answers 403 to anything without a
    //  browser session, MCA's portal likewise; BSE's endpoint only responds
    //  to a spoofed Referer and is undocumented; the aggregators that do
    //  work are third parties whose terms forbid this use. Every one of them
    //  is the same category as Yahoo's crumb-gated quoteSummary that this
    //  file's header rejects, and shipping one as a core path would be a
    //  launch-day outage with someone else's terms attached. The PDF route
    //  already handles these markets properly — the extractor understands
    //  lakh/crore scaling and Ind AS filings — so that is where this points.
    const market = await marketOnly(ticker);
    if (market) return res.status(200).json(market);
    return res.status(404).json({
      ok: false, notFound: true,
      error: `"${ticker}" was not found as an SEC registrant or as a listed security. `
           + `Check the symbol, or upload the filing as a PDF.`,
    });
  }

  let facts, entityName, taxonomy = "us-gaap";
  try {
    const r = await secFetch(FACTS_URL(entry.cik));
    if (!r.ok) throw new Error(`companyfacts ${r.status}`);
    const j = await r.json();
    entityName = j.entityName || entry.title;
    facts = (j.facts && j.facts["us-gaap"]) || null;
    if (!facts && j.facts && j.facts["ifrs-full"]) {
      facts = j.facts["ifrs-full"];
      taxonomy = "ifrs-full";
    }
    if (!facts) throw new Error("no us-gaap or ifrs-full facts");
  } catch (err) {
    //: Registered with SEC but no usable XBRL (a 404 on companyfacts, or a
    //  taxonomy we don't read). Market data is still real and still useful,
    //  so degrade to it rather than dead-ending the user.
    const market = await marketOnly(ticker);
    if (market) {
      market.notes.unshift(`No machine-readable XBRL filing data for ${ticker} `
        + `(${String(err.message).slice(0, 60)}), so only market data is shown.`);
      return res.status(200).json(market);
    }
    return res.status(502).json({
      ok: false,
      error: `SEC filing data unavailable for ${ticker} (${String(err.message).slice(0, 80)}) — upload the filing as a PDF instead.`,
    });
  }

  const notes = [];
  const flows = {}, stocks = {};
  const flowTags = taxonomy === "ifrs-full" ? IFRS_FLOW_TAGS : FLOW_TAGS;
  const stockTags = taxonomy === "ifrs-full" ? IFRS_STOCK_TAGS : STOCK_TAGS;

  //: Establish the filing's reporting currency BEFORE reading any figure, and
  //  hold every concept to it. USD wins when the filer offers it (the ADR
  //  price is USD, so that keeps price and financials on one basis);
  //  otherwise the filing's own currency stands and the price is dropped
  //  below rather than silently mixed in.
  const reportingCurrency = detectReportingCurrency(facts, flowTags.revenue);
  for (const [k, tags] of Object.entries(flowTags)) {
    flows[k] = pickAnnualSeries(facts, tags, 6, reportingCurrency);
  }
  for (const [k, tags] of Object.entries(stockTags)) {
    stocks[k] = pickInstant(facts, tags, k === "shares_outstanding" ? null : reportingCurrency);
  }
  if (taxonomy === "ifrs-full") {
    notes.push("Figures come from a Form 20-F filed under IFRS and denominated in USD, "
      + "as is the ADR price shown. Share count is total ordinary shares, while the price "
      + "is per ADR — confirm the ADR ratio before relying on per-share or market-cap "
      + "outputs. Enterprise-value results are unaffected.");
  }

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
  //: Price and beta are both market facts, fetched together. Beta needs two
  //  chart calls of its own, so all three run concurrently rather than
  //  serially — this endpoint is already the slowest thing in the intake.
  const [priceInfo, betaInfo] = await Promise.all([
    fetchPrice(entry.symbol),
    computeBeta(entry.symbol).catch(() => null),
  ]);
  if (!priceInfo) {
    notes.push("Live share price unavailable; enter it manually or the models will use their documented fallback.");
  }
  const priceUsable = !!priceInfo
    && (!reportingCurrency || priceInfo.currency === reportingCurrency);
  if (priceInfo && !priceUsable) {
    notes.push(`Share price omitted: the quote is in ${priceInfo.currency} but this company `
      + `reports in ${reportingCurrency}. Mixing the two would corrupt every per-share and `
      + `market-implied result, so enter the price manually on the filing's own basis.`);
  }
  if (betaInfo) {
    notes.push(`Beta ${betaInfo.beta.toFixed(2)} is computed here by regressing `
      + `${betaInfo.observations} monthly returns against the ${betaInfo.benchmark}, `
      + `not taken from a filing — beta is a market statistic and is not disclosed in XBRL.`);
  } else {
    notes.push("Beta could not be computed from price history (too few observations or no price series); the sector-median fallback will apply instead.");
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
    //: Only when the quote and the filing are in the same currency. A USD ADR
    //  price beside INR financials would silently corrupt every per-share and
    //  market-implied result; reporting it missing is the honest outcome.
    current_price: priceUsable ? priceInfo.price : null,
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
    //: Annual figure only — see the dividends_per_share comment in FLOW_TAGS
    //  for why taking the most recent row instead would understate it 4x.
    dividend_per_share: latestFlow("dividends_per_share"),
    //: Always true for anything this endpoint returns: pickAnnualSeries only
    //  admits ~365-day periods, so a quarterly declaration can't leak through
    //  and be re-annualised a second time downstream.
    dividend_is_annual: true,
    beta: betaInfo ? Number(betaInfo.beta.toFixed(3)) : null,
    currency: reportingCurrency || (priceInfo ? priceInfo.currency : "USD"),
    statement_basis: "annual",
    //: Provenance. assumptions.py reads this to decide whether a figure may be
    //  described as coming from a filing, and WHICH kind — it must never claim
    //  "scraped from PDF" for an XBRL fact.
    backends_used: [taxonomy === "ifrs-full" ? "sec-edgar-xbrl-ifrs" : "sec-edgar-xbrl"],
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
    taxonomy,
    company_name: entityName,
    fiscal_year: fields.fiscal_year,
    currency: fields.currency,
    period_end: latestEnd,
    fields,
    missing,
    notes,
    beta: betaInfo || null,
    sources: [
      { name: "SEC EDGAR XBRL companyfacts", url: `https://data.sec.gov/api/xbrl/companyfacts/CIK${entry.cik}.json` },
      ...(priceInfo ? [{ name: "Share price", url: "Yahoo Finance chart API" }] : []),
      ...(betaInfo ? [{ name: `Beta vs ${betaInfo.benchmark} (${betaInfo.observations} monthly returns)`,
                       url: "Computed by OLS from price history" }] : []),
    ],
  });
};

//: Pure helpers exported for scripts/test_fundamentals.js. They carry the
//  parts that are easy to get subtly wrong (share-class spelling, restatement
//  dedup, period alignment) and impossible to check by eyeballing a live
//  response, so they are tested directly rather than only through the handler.
module.exports._internals = { resolveTicker, detectReportingCurrency, pickInstant, pickAnnualSeries, FLOW_TAGS, STOCK_TAGS,
                              IFRS_FLOW_TAGS, IFRS_STOCK_TAGS,
                              benchmarkFor, monthlyReturns, monthKey, BENCHMARKS, MIN_BETA_OBSERVATIONS };
