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
const SUBMISSIONS_URL = (cik) => `https://data.sec.gov/submissions/CIK${cik}.json`;

//: The registrant's SIC code and newest annual report from EDGAR
//  submissions. The pipeline uses the SIC code to recognise commodity
//  producers and lenders (assumptions.py); the annual-report date lets the
//  handler say when the structured data trails the latest filing. A failed
//  lookup only loses those refinements, so it never fails the request.
async function fetchFilerInfo(cik) {
  const none = { sic: null, latestAnnual: null };
  try {
    const r = await secFetch(SUBMISSIONS_URL(cik));
    if (!r.ok) return none;
    const j = await r.json();
    const sic = parseInt(j.sic, 10);
    //: The newest annual report on file (10-K, 20-F or 40-F, not an
    //  amendment). companyfacts can lag it by months for foreign filers, so
    //  the handler compares its period to the data it actually found.
    const rec = (j.filings && j.filings.recent) || {};
    const forms = rec.form || [];
    let latestAnnual = null;
    for (let i = 0; i < forms.length; i++) {
      if (!/^(10-K|20-F|40-F)$/.test(forms[i]) || !rec.reportDate?.[i]) continue;
      if (!latestAnnual || rec.reportDate[i] > latestAnnual.reportDate) {
        latestAnnual = { form: forms[i], reportDate: rec.reportDate[i], filed: rec.filingDate?.[i] || null };
      }
    }
    return { sic: Number.isFinite(sic) && sic > 0 ? sic : null, latestAnnual };
  } catch {
    return none;
  }
}

//: Successor registrant CIK -> predecessor CIK, for reorganisations that move
//  a ticker to a new filer with no annual XBRL history. EDGAR's JSON APIs do
//  not link the two, so each is listed explicitly. Verified 2026-09-24:
//  ExxonMobil Holdings Corp (2115436) took over XOM in July 2026; EXXON MOBIL
//  CORP (34088) holds FY2009-FY2025.
const PREDECESSOR_CIKS = {
  "0002115436": "0000034088",
};

//: Union of two companyfacts `facts` objects, predecessor rows first. Rows
//  for the same period are resolved downstream by filing date, so nothing is
//  deduplicated here.
function mergeFacts(older, newer) {
  const out = {};
  for (const src of [older, newer]) {
    for (const [tax, tags] of Object.entries(src || {})) {
      out[tax] = out[tax] || {};
      for (const [tag, body] of Object.entries(tags)) {
        const dst = out[tax][tag] || (out[tax][tag] = { ...body, units: {} });
        for (const [unit, rows] of Object.entries(body.units || {})) {
          dst.units[unit] = (dst.units[unit] || []).concat(rows);
        }
      }
    }
  }
  return out;
}

//: The ticker→CIK map is ~1 MB and changes rarely. Cached per warm lambda so a
//  burst of lookups costs SEC one fetch, not one per request.
let tickerMap = null;
let tickerMapAt = 0;
const TICKER_TTL = 24 * 3600 * 1000;

const secFetch = (url) =>
  fetch(url, { headers: { "User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate" },
               signal: AbortSignal.timeout(12_000) });

//: A failure worth retrying — the upstream was slow or briefly unhealthy —
//  as opposed to a permanent "there is nothing here" (a 404, a filer with no
//  XBRL). Only the second kind may be cached as the answer.
function isTransient(err) {
  const msg = String(err && err.message || "");
  return err?.name === "TimeoutError" || err?.name === "AbortError"
    || err instanceof TypeError                 // fetch's network-failure shape
    || /\b(429|5\d\d)\b/.test(msg);
}

//: Cache policy for a response whose market data came back incomplete. The
//  default s-maxage=3600 turned ONE transient upstream failure into an hour
//  of degraded answers for every visitor (observed: a single SEC timeout on
//  KO served "market data only" — no cash flows — as a CDN HIT afterwards).
//  A response missing its price or beta is re-fetched after two minutes;
//  one degraded by an upstream error is never cached at all.
const CACHE_SHORT = "public, max-age=0, s-maxage=120";
const CACHE_NONE = "no-store";

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
  //: RevenuesNetOfInterestExpense is how Morgan Stanley, Wells Fargo and
  //  Goldman Sachs report total net revenue. None of them uses `Revenues`
  //  any more (MS stopped in 2014, WFC in 2019), so without this tag the
  //  freshest-tag rule picked the abandoned one and showed MS as FY2014.
  revenue: ["RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet",
            "RevenuesNetOfInterestExpense"],
  net_income: ["NetIncomeLoss", "ProfitLoss",
               "NetIncomeLossAvailableToCommonStockholdersBasic"],
  operating_cash_flow: ["NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
  //: The two "Other" tags are the capex line for Eli Lilly
  //  (PaymentsToAcquireOtherPropertyPlantAndEquipment, $7.8bn FY2025) and
  //  Verizon (PaymentsToAcquireOtherProductiveAssets, $17.0bn). Without them
  //  both fell back to OCF minus D&A, overstating Lilly's FCF by ~60%.
  //  They rank last, so a filer tagging the main line too still uses it.
  capital_expenditures: ["PaymentsToAcquirePropertyPlantAndEquipment",
                         "PaymentsToAcquireProductiveAssets",
                         "PaymentsToAcquireOtherPropertyPlantAndEquipment",
                         "PaymentsToAcquireOtherProductiveAssets"],
  depreciation_amortization: ["DepreciationDepletionAndAmortization",
                              "DepreciationAmortizationAndAccretionNet",
                              "DepreciationAndAmortization", "Depreciation"],
  rd_expense: ["ResearchAndDevelopmentExpense"],
  //: InterestExpenseNonoperating is the 2024-taxonomy tag MSFT and NVDA moved
  //  to; without it their interest stopped at FY2024 and was carried forward.
  interest_expense: ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                     "InterestIncomeExpenseNet"],
  income_tax_expense: ["IncomeTaxExpenseBenefit"],
  operating_income: ["OperatingIncomeLoss"],
  //: Positive-only expense. ShareBasedCompensation is by far the dominant
  //  tag; AllocatedShareBasedCompensationExpense is the fallback some filers
  //  use when the expense is broken out by segment/allocation instead.
  stock_based_compensation: ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
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
 * Many report in USD beside a USD ADR price; others (SAP in EUR, Novo in
 * DKK) do not, and their USD price is converted in the handler. The ADR
 * RATIO cannot be read from EDGAR — see ADR_LOCAL_LISTINGS.                 */
const IFRS_FLOW_TAGS = {
  revenue: ["RevenueFromContractsWithCustomers", "Revenue"],
  net_income: ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
  operating_cash_flow: ["CashFlowsFromUsedInOperatingActivities"],
  //: SAP tags capex only as the combined PP&E + intangibles purchase line —
  //  still capital expenditure, and without it SAP had no FCF at all.
  capital_expenditures: ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                         "PurchaseOfPropertyPlantAndEquipment",
                         "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
                         //: Shell's "Capital expenditure" line ($19.0bn FY2025).
                         "PurchaseOfOtherLongtermAssetsClassifiedAsInvestingActivities"],
  depreciation_amortization: ["DepreciationAndAmortisationExpense",
                              "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"],
  rd_expense: ["ResearchAndDevelopmentExpense"],
  interest_expense: ["InterestExpense", "FinanceCosts"],
  income_tax_expense: ["IncomeTaxExpenseContinuingOperations"],
  //: The plural "Paid…Shares" tag is last: it is cash paid IN the year, and
  //  Sony's holds only a ¥10 interim beside a ¥95 recognised total. Shell
  //  and others tag nothing else, which is the only time it should be read.
  dividends_per_share: ["DividendsPaidOrdinarySharePerShare",
                        "DividendsRecognisedAsDistributionsToOwnersOfParentPerShare",
                        "DividendsRecognisedAsDistributionsToOwnersPerShare",
                        "DividendsPaidOrdinarySharesPerShare"],
  pretax_income: ["ProfitLossBeforeTax"],
  operating_income: ["ProfitLossFromOperatingActivities"],
  stock_based_compensation: ["AdjustmentsForSharebasedPayments"],
};

const IFRS_STOCK_TAGS = {
  cash_and_equivalents: ["CashAndCashEquivalents"],
  short_term_investments: ["OtherCurrentFinancialAssets", "CurrentInvestments"],
  shares_outstanding: ["NumberOfSharesOutstanding", "NumberOfSharesIssuedAndFullyPaid"],
};

//: Instant (stock) concepts — balance sheet.
const STOCK_TAGS = {
  cash_and_equivalents: ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
  //: DebtSecuritiesCurrent is NVIDIA's marketable-securities line ($34bn).
  //  Only the first tag present on the anchor date is used, so a filer that
  //  tags two of these is never summed twice.
  short_term_investments: ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                           "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "DebtSecuritiesCurrent"],
  shares_outstanding: ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
};

/* ---------------------------------- debt ---------------------------------- *
 * Total debt is a SUM of balance-sheet lines, and filers split it in several
 * incompatible ways, so it is assembled from roles rather than read off two
 * independently chosen tags. The old two-tag cascade failed four ways on real
 * data (2026-09 audit):
 *   - XOM tags LongTermDebtAndCapitalLeaseObligations + DebtCurrent; neither
 *     long-term tag was in the cascade, so $32bn of bonds vanished and total
 *     debt read $10.1bn instead of $42.4bn.
 *   - KO abandoned LongTermDebt after Q1 2024; with no date check, its
 *     March-2024 figure was published beside 2026 cash as if current.
 *   - AAPL's commercial paper sat outside the one short-term tag picked.
 *   - LongTermDebt INCLUDES the current portion, so pairing it with
 *     LongTermDebtCurrent double-counted whenever the noncurrent tag was absent.
 * Every role is read at ONE balance-sheet date (see pickBalanceSheet).
 *
 * `noncurrent` excludes current maturities; `longTermTotal` includes them;
 * `debtCurrent` is ALL current debt (current maturities + short-term
 * borrowings); `shortTerm` is borrowings other than current maturities.
 * CommercialPaper and OtherShortTermBorrowings are disjoint by definition, and
 * are only summed when the filer tags no ShortTermBorrowings total. */
const DEBT_TAGS = {
  noncurrent: ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
  longTermTotal: ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
  currentMaturities: ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
  debtCurrent: ["DebtCurrent"],
  shortTermTotal: ["ShortTermBorrowings"],
  shortTermParts: ["CommercialPaper", "OtherShortTermBorrowings"],
};

//: Face-line components some filers use INSTEAD of any role tag above
//  (Alibaba: bank loans, senior notes and convertibles as separate lines,
//  no LongTermDebt at all). Read only when no role tag exists on the date,
//  and only one tag per slot, so a filer tagging both a total and these
//  footnote-level parts is never summed twice.
const DEBT_COMPONENT_SLOTS = [
  ["LongTermLoansFromBank", "LongTermLoansPayable"],
  ["SeniorLongTermNotes", "LongTermNotesPayable"],
  ["ConvertibleDebtNoncurrent"],
  ["ShortTermBankLoansAndNotesPayable", "LoansPayableCurrent"],
  ["SeniorNotesCurrent", "NotesPayableCurrent"],
  ["ConvertibleDebtCurrent"],
];

//: A tag name that denotes the company's OWN borrowing (not debt securities
//  it holds as assets). Used only to decide whether a filer has ever
//  borrowed — see hasEverBorrowed.
const BORROWING_NAME = /Debt|Borrowing|NotesPayable|LoansPayable|SeniorNotes|SeniorLongTermNotes|CommercialPaper|BondsIssued|LoansFromBank|ConvertibleNotes/;
const NOT_OWN_BORROWING = /Securities|Receivable|Investment|Asset|HeldToMaturity|AvailableForSale|Trading|Lessor/;

//: True when any borrowing-like tag in the filer's whole XBRL history has
//  carried a non-zero value. Infosys has never tagged one (its only match is
//  a liquidity table stating bank borrowings of 0), so it is genuinely
//  debt-free; a filer whose debt lives only in custom tags (Berkshire) still
//  tags a standard maturity schedule and so is NOT mistaken for debt-free.
function hasEverBorrowed(facts) {
  for (const [tag, body] of Object.entries(facts)) {
    if (!BORROWING_NAME.test(tag) || NOT_OWN_BORROWING.test(tag)) continue;
    //: Money units only — Infosys's IFRS 16 transition tag
    //  WeightedAverageLesseesIncrementalBorrowingRate… is a 4.5% RATE.
    for (const [unit, rows] of Object.entries(body.units || {})) {
      if (!/^[A-Z]{3}$/.test(unit)) continue;
      if (rows.some((r) => typeof r.val === "number" && r.val !== 0)) return true;
    }
  }
  return false;
}

//: IFRS: `Borrowings` is the total when tagged. Otherwise sum the noncurrent
//  loans and bonds with the current side. TSMC reports its NT$927bn of bonds
//  only as NoncurrentPortionOfNoncurrentBondsIssued, beside a separate
//  LongtermBorrowings line for bank loans — reading loans alone (the old
//  cascade) missed nine-tenths of its debt.
const IFRS_DEBT_TAGS = {
  total: ["Borrowings"],
  noncurrentParts: ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings",
                    "NoncurrentPortionOfNoncurrentBondsIssued"],
  currentTotal: ["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
  currentParts: ["CurrentPortionOfNoncurrentBorrowings", "CurrentPortionOfLongtermBorrowings",
                 "ShorttermBorrowings", "CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued"],
};

/* ---------------------------- lease liabilities ---------------------------- *
 * Leases sit outside FLOW_TAGS/STOCK_TAGS because they need a THIRD shape:
 * some filers disclose one total tag, others only a current/noncurrent pair,
 * and the right answer depends on which is present — see pickLeaseLiability().
 * IFRS 16 collapses the finance/operating distinction entirely (a single
 * on-balance-sheet lease liability), so IFRS filers are handled separately in
 * the handler rather than through this cascade. */
const LEASE_TAGS = {
  financeTotal: ["FinanceLeaseLiability"],
  financeCurrent: ["FinanceLeaseLiabilityCurrent"],
  financeNoncurrent: ["FinanceLeaseLiabilityNoncurrent"],
  operatingTotal: ["OperatingLeaseLiability"],
  operatingCurrent: ["OperatingLeaseLiabilityCurrent"],
  operatingNoncurrent: ["OperatingLeaseLiabilityNoncurrent"],
};

//: SBUX (CIK 829224) has no OperatingLeaseLiability total tag — only current
//  + noncurrent — while AAPL reports OperatingLeaseLiabilityNoncurrent plus a
//  current portion folded into "other current liabilities" (no current lease
//  tag at all), so the noncurrent-only branch below is a real, not
//  theoretical, code path.

/**
 * The currency a filing actually reports in.
 *
 * Read off the revenue tags, which every operating company has: the currency
 * whose ANNUAL revenue reaches the most recent fiscal year wins. USD breaks a
 * tie, so a filer that gives a USD convenience translation of its latest year
 * (Wipro, TSMC) stays comparable with its USD ADR price.
 *
 * "USD whenever offered" was the old rule, and it is wrong for a filer that
 * translated into USD once and then stopped: SAP carried USD revenue only for
 * FY2017, so every figure was pinned to 2017 — $28bn of eight-year-old
 * revenue presented as current, beside a 2026 price.
 */
function detectReportingCurrency(facts, revenueTags) {
  const latestByCcy = new Map();
  for (const tag of revenueTags) {
    const f = facts[tag];
    if (!f) continue;
    for (const [unit, rows] of Object.entries(f.units)) {
      if (!/^[A-Z]{3}$/.test(unit)) continue;
      for (const r of rows) {
        if (typeof r.val !== "number" || !r.start || !r.end) continue;
        const days = (Date.parse(r.end) - Date.parse(r.start)) / 86_400_000;
        if (days < 340 || days > 400) continue;
        if (!latestByCcy.has(unit) || r.end > latestByCcy.get(unit)) latestByCcy.set(unit, r.end);
      }
      if (!latestByCcy.has(unit)) latestByCcy.set(unit, "");   // quarterly-only: still a signal
    }
  }
  if (!latestByCcy.size) return null;
  const newest = Math.max(...[...latestByCcy.values()].map((e) => (e ? Date.parse(e) : 0)));
  const tied = [...latestByCcy.entries()]
    .filter(([, e]) => (e ? Date.parse(e) : 0) === newest).map(([c]) => c);
  return tied.includes("USD") ? "USD" : tied[0];
}

/**
 * Latest non-null numeric, restatement-aware.
 *
 * `minRelative` guards against a tag that mixes totals with fragments. Wipro
 * has no NumberOfSharesOutstanding at all, so the cascade falls to
 * NumberOfSharesIssuedAndFullyPaid — which carries a 1,274,805 row (an
 * issuance tranche) one day before the 5,232,094,402 total. Taking whichever
 * happens to be latest is a coin flip between the share count and a number
 * four thousand times too small, and nothing downstream would question it.
 * Rows below `minRelative` of the series maximum are therefore not totals and
 * are skipped.
 */
function pickInstant(facts, tags, prefer = null, minRelative = 0, freshest = true) {
  //: Freshest tag wins, ties to cascade order — the same rule, and the same
  //  reason, as pickAnnualSeries: filers abandon tags, and first-match
  //  returned KO's March-2024 long-term debt beside its 2026 balance sheet.
  //  `freshest = false` keeps the old first-match cascade for concepts whose
  //  later tags are NOT substitutes: JPM tags shares ISSUED every quarter but
  //  shares OUTSTANDING only yearly, and issued includes 1.4bn treasury
  //  shares — freshest-wins read 4.10bn shares instead of 2.70bn.
  let best = null;
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
    let rows = (f.units[unit] || []).filter((r) => typeof r.val === "number");
    if (!rows.length) continue;
    if (minRelative > 0) {
      const peak = Math.max(...rows.map((r) => Math.abs(r.val)));
      rows = rows.filter((r) => Math.abs(r.val) >= peak * minRelative);
      if (!rows.length) continue;
    }
    // Two rows can share an `end` when a later filing restates it. Sort by end,
    // then by `filed`, and take the last — i.e. the most recently filed view of
    // the most recent period.
    rows.sort((a, b) => (a.end < b.end ? -1 : a.end > b.end ? 1
                        : (a.filed || "") < (b.filed || "") ? -1 : 1));
    const last = rows[rows.length - 1];
    if (!freshest) return { value: last.val, end: last.end, tag, unit };
    if (!best || last.end > best.end) best = { value: last.val, end: last.end, tag, unit };
  }
  return best;
}

/**
 * The value a cascade of instant tags reports AT one exact date, or null.
 *
 * Balance-sheet lines must come from the same balance sheet: a component
 * whose newest row predates `end` is a line the filer no longer uses, and
 * adding it would mix a stale figure into a current total. When the pinned
 * currency has no row but another currency does, the value is translated at
 * `rates[ccy]` — the filer's OWN translation rate on that date (see
 * translationRates) — because a convenience-translating filer such as TSMC
 * gives USD for some lines (cash, long-term bonds) and not others (current
 * bonds). Without a rate the line is left out rather than mislabelled.
 */
function valueAt(facts, tags, end, currency, rates = {}) {
  for (const tag of tags) {
    const f = facts[tag];
    if (!f) continue;
    const at = (unit) => {
      const rows = (f.units[unit] || [])
        .filter((r) => r.end === end && !r.start && typeof r.val === "number")
        .sort((a, b) => ((a.filed || "") < (b.filed || "") ? -1 : 1));
      return rows.length ? rows[rows.length - 1].val : null;
    };
    const own = currency ? at(currency) : null;
    if (own !== null) return { value: own, tag, translatedFrom: null };
    for (const [ccy, rate] of Object.entries(rates)) {
      const v = at(ccy);
      if (v !== null) return { value: v * rate, tag, translatedFrom: ccy };
    }
  }
  return null;
}

//: The filer's own translation rate(s) into `currency` at `end`, from the cash
//  line it reports in both currencies on that date: USD cash / TWD cash is
//  exactly the rate TSMC used for its convenience translation. Only rates the
//  filing itself implies are returned; nothing is looked up externally.
function translationRates(facts, cashTags, end, currency) {
  const rates = {};
  if (!currency) return rates;
  for (const tag of cashTags) {
    const f = facts[tag];
    if (!f) continue;
    const valIn = (unit) => {
      const r = (f.units[unit] || []).filter((x) => x.end === end && !x.start && typeof x.val === "number");
      return r.length ? r[r.length - 1].val : null;
    };
    const base = valIn(currency);
    if (!base) continue;
    for (const unit of Object.keys(f.units)) {
      if (unit === currency || !/^[A-Z]{3}$/.test(unit) || rates[unit]) continue;
      const other = valIn(unit);
      if (other) rates[unit] = base / other;
    }
  }
  return rates;
}

/**
 * Cash, short-term investments and total debt, all from ONE balance sheet.
 *
 * The anchor is the date of the freshest cash figure (every balance sheet has
 * cash); with no cash tag at all, the freshest debt line. Every other line is
 * read at exactly that date — see valueAt. Returns the components used so the
 * handler can say which lines were summed and apply the lease double-count
 * guard to the actual tags.
 */
//: Form type of the most recently filed row among the cover (dei) facts and
//  the cash tags — enough to tell a 20-F filer from a 10-K filer when the
//  revenue tags are absent.
function latestFilingForm(sources) {
  let best = null;
  const cashTags = [...STOCK_TAGS.cash_and_equivalents, ...IFRS_STOCK_TAGS.cash_and_equivalents];
  for (const src of sources) {
    if (!src) continue;
    const tags = Object.keys(src).filter((t) => t.startsWith("Entity") || cashTags.includes(t));
    for (const t of tags) {
      for (const rows of Object.values(src[t].units || {})) {
        for (const r of rows) {
          if (r.form && (!best || (r.filed || "") > (best.filed || ""))) best = r;
        }
      }
    }
  }
  return best ? String(best.form) : "";
}

//: Newest ~365-day period end across a cascade of revenue tags, any unit.
function latestAnnualEnd(facts, tags) {
  let best = "";
  for (const tag of tags) {
    for (const rows of Object.values((facts[tag] && facts[tag].units) || {})) {
      for (const r of rows) {
        if (!r.start || !r.end) continue;
        const days = (Date.parse(r.end) - Date.parse(r.start)) / 86_400_000;
        if (days >= 340 && days <= 400 && r.end > best) best = r.end;
      }
    }
  }
  return best;
}

//: Latest basic weighted-average share count; at a shared end date the
//  shortest period (the quarter, not year-to-date) is the most current.
function pickWeightedAverageShares(facts, taxonomy) {
  const tag = taxonomy === "ifrs-full" ? "WeightedAverageShares" : "WeightedAverageNumberOfSharesOutstandingBasic";
  const rows = ((facts[tag] && facts[tag].units.shares) || [])
    .filter((r) => typeof r.val === "number" && r.val > 0 && r.start && r.end);
  if (!rows.length) return null;
  const end = rows.map((r) => r.end).sort().pop();
  const days = (r) => Date.parse(r.end) - Date.parse(r.start);
  const best = rows.filter((r) => r.end === end)
    .sort((a, b) => days(a) - days(b) || ((b.filed || "") > (a.filed || "") ? 1 : -1))[0];
  return { value: best.val, end: best.end, start: best.start, tag, unit: "shares" };
}

function pickCoverShares(dei) {
  const rows = (dei && dei.EntityCommonStockSharesOutstanding
    && dei.EntityCommonStockSharesOutstanding.units.shares) || [];
  const valid = rows.filter((r) => typeof r.val === "number" && r.val > 0 && r.end);
  if (!valid.length) return null;
  const end = valid.map((r) => r.end).sort().pop();
  const atEnd = valid.filter((r) => r.end === end);
  const filed = atEnd.map((r) => r.filed || "").sort().pop();
  const latest = atEnd.filter((r) => (r.filed || "") === filed);
  if (new Set(latest.map((r) => r.val)).size !== 1) return null;   // multi-class
  return { value: latest[0].val, end, tag: "dei:EntityCommonStockSharesOutstanding", unit: "shares" };
}

function pickBalanceSheet(facts, taxonomy, currency, anchorOverride = null) {
  const ifrs = taxonomy === "ifrs-full";
  const stockTags = ifrs ? IFRS_STOCK_TAGS : STOCK_TAGS;
  const debtTags = ifrs ? IFRS_DEBT_TAGS : DEBT_TAGS;
  const cashPick = anchorOverride ? null : pickInstant(facts, stockTags.cash_and_equivalents, currency);
  let anchor = anchorOverride || (cashPick ? cashPick.end : null);
  if (!anchor) {
    const allDebt = Object.values(debtTags).flat();
    anchor = pickInstant(facts, allDebt, currency)?.end || null;
  }
  const out = { anchor, cash: null, sti: null, debt: null, debtParts: [], translated: [] };
  if (!anchor) return out;
  const rates = translationRates(facts, stockTags.cash_and_equivalents, anchor, currency);
  const read = (tags) => valueAt(facts, tags, anchor, currency, rates);
  const noteTranslation = (v) => {
    if (v && v.translatedFrom) out.translated.push(`${v.tag} (${v.translatedFrom})`);
  };
  const cash = read(stockTags.cash_and_equivalents);
  noteTranslation(cash);
  out.cash = cash ? cash.value : null;
  const sti = read(stockTags.short_term_investments);
  noteTranslation(sti);
  out.sti = sti ? sti.value : null;

  const parts = [];
  const use = (v) => {
    if (v) { parts.push({ tag: v.tag, value: v.value }); noteTranslation(v); }
    return v;
  };
  if (ifrs) {
    const total = read(debtTags.total);
    if (total) use(total);
    else {
      for (const tag of debtTags.noncurrentParts) use(read([tag]));
      const curTotal = read(debtTags.currentTotal);
      if (curTotal) use(curTotal);
      else for (const tag of debtTags.currentParts) use(read([tag]));
    }
  } else {
    const nc = read(debtTags.noncurrent);
    const ltTotal = nc ? null : read(debtTags.longTermTotal);
    const debtCurrent = read(debtTags.debtCurrent);
    const curMat = read(debtTags.currentMaturities);
    let shortTerm = read(debtTags.shortTermTotal);
    const stParts = shortTerm ? [] : debtTags.shortTermParts.map((t) => read([t])).filter(Boolean);
    if (nc) {
      use(nc);
      if (debtCurrent) use(debtCurrent);          // current maturities + short-term borrowings
      else { use(curMat); use(shortTerm); stParts.forEach(use); }
    } else if (ltTotal) {
      use(ltTotal);                               // already includes current maturities
      if (shortTerm || stParts.length) { use(shortTerm); stParts.forEach(use); }
      else if (debtCurrent && curMat) {
        use({ tag: "DebtCurrent−LongTermDebtCurrent", value: debtCurrent.value - curMat.value });
      } else if (debtCurrent && !curMat) {
        //: DebtCurrent may or may not overlap the total's current maturities;
        //  leaving it out understates (flagged by the caller), adding it may
        //  double-count, and a silent double-count is the worse error.
        out.ambiguousCurrent = true;
      }
    } else {
      use(debtCurrent || curMat); use(shortTerm); stParts.forEach(use);
    }
  }
  if (!ifrs && !parts.length) {
    for (const slot of DEBT_COMPONENT_SLOTS) use(read(slot));
    if (parts.length) out.fromComponents = true;
  }
  out.debtParts = parts.filter((p) => typeof p.value === "number");
  out.debt = out.debtParts.length ? out.debtParts.reduce((a, p) => a + p.value, 0) : null;
  if (out.debt === null && !hasEverBorrowed(facts)) {
    out.debt = 0;
    out.neverBorrowed = true;
  }
  out.hasLongTerm = out.neverBorrowed || out.debtParts.some((p) => !/Current|Shortterm|ShortTerm|CommercialPaper/.test(p.tag)
    || /IncludingCurrentMaturities/.test(p.tag));
  return out;
}

//: pickBalanceSheet at the freshest cash date, falling back to an earlier
//  date when that balance sheet tags no debt at all. GM's 10-Q (2026-06-30)
//  carries no debt lines while its 10-K (2025-12-31) tags $131.6bn, so total
//  debt came back missing and equity was overstated by the whole amount.
//  Cash and debt are still read from ONE date (the earlier one), never mixed;
//  only a date with genuine long-term debt qualifies, and no more than a year
//  back, so a stale balance sheet never stands in silently (the handler notes it).
function pickBalanceSheetWithFallback(facts, taxonomy, currency) {
  const bs = pickBalanceSheet(facts, taxonomy, currency);
  if (bs.debt !== null || !bs.anchor) return bs;
  const cashTags = (taxonomy === "ifrs-full" ? IFRS_STOCK_TAGS : STOCK_TAGS).cash_and_equivalents;
  const ends = new Set();
  for (const tag of cashTags) {
    for (const rows of Object.values((facts[tag] && facts[tag].units) || {})) {
      for (const r of rows) {
        if (r.start || !r.end || r.end >= bs.anchor) continue;
        if ((Date.parse(bs.anchor) - Date.parse(r.end)) / 86_400_000 <= 400) ends.add(r.end);
      }
    }
  }
  for (const end of [...ends].sort().reverse()) {
    const alt = pickBalanceSheet(facts, taxonomy, currency, end);
    if (alt.debt !== null && alt.hasLongTerm && !alt.neverBorrowed) {
      alt.fellBackFrom = bs.anchor;
      return alt;
    }
  }
  return bs;
}

/**
 * Annual series for a flow concept, newest last.
 *
 * `fy`/`fp` describe the FILING, not the period — an FY2025 10-K restates
 * FY2024 figures under fy:2025 — so periods are identified by their own
 * start/end span instead: a ~365-day duration is an annual period. Filtering
 * on fy would double-count restatements and silently corrupt the FCF series.
 */
function pickAnnualSeries(facts, tags, maxYears = 6, prefer = null, largestOnTie = false) {
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
      //: Per-share first when both exist: Novo tags dividends under a bare
      //  "DKK" unit (monthly rows, 4.55) as well as "DKK/shares" (11.70).
      unit = [`${prefer}/shares`, prefer].find((u) => f.units[u]);
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
  //: `largestOnTie` is for REVENUE only: two revenue tags for the same year
  //  are a total and a component, never two totals, so the larger is the
  //  total. Spotify tags EUR 17.2bn as Revenue and a EUR 0.67bn component as
  //  RevenueFromContractsWithCustomers; cascade order picked the component,
  //  a 26x understatement with a 330% "operating margin".
  const lastVal = (c) => c.series[c.series.length - 1].val;
  candidates.sort((a, b) => {
    const ya = a.latest.slice(0, 4), yb = b.latest.slice(0, 4);
    if (ya !== yb) return ya < yb ? 1 : -1;
    if (largestOnTie && lastVal(a) !== lastVal(b)) return lastVal(b) - lastVal(a);
    return a.rank - b.rank;
  });
  const best = candidates[0];
  return { series: best.series, tag: best.tag, unit: best.unit };
}

/**
 * Total lease liability from whichever shape a filer actually discloses.
 *
 * Preferring the total tag when it exists avoids double-summing a company
 * that also tags a total alongside the split (some filers do both); falling
 * back to current+noncurrent covers the common case where only the split is
 * tagged. Noncurrent-only is accepted (not refused) because several large
 * filers — AAPL's operating lease among them — never tag a current lease
 * liability separately, folding it into "other current liabilities" instead;
 * refusing the figure entirely would be worse than a caveated understatement.
 */
function pickLeaseLiability(facts, totalTags, currentTags, noncurrentTags, prefer = null) {
  const total = pickInstant(facts, totalTags, prefer);
  if (total) return { value: total.value, end: total.end, source: "total" };

  const cur = pickInstant(facts, currentTags, prefer);
  const nc = pickInstant(facts, noncurrentTags, prefer);
  if (!cur && !nc) return null;
  if (nc && !cur) return { value: nc.value, end: nc.end, source: "noncurrent-only" };
  if (cur && !nc) return { value: cur.value, end: cur.end, source: "current-only" };
  return { value: cur.value + nc.value, end: nc.end, source: "sum" };
}

//: A debt tag whose NAME already says "Lease" (e.g.
//  LongTermDebtAndCapitalLeaseObligations) already folds finance-lease
//  liabilities into total_debt. Reporting finance_lease_liabilities
//  alongside it would double-count the same dollars under two field names —
//  a wrong balance sheet that would look like a complete one. XOM and KO
//  both report debt this way (DEBT_TAGS.noncurrent), so the guard is live.
function debtTagIncludesLeases(tag) {
  return typeof tag === "string" && /lease/i.test(tag);
}

//: us-gaap tags whose value is a NET figure (income minus expense, or vice
//  versa) rather than a pure expense. A positive value here means net
//  interest INCOME, not expense — treating it as expense (the historical
//  bug) turns a company with more interest income than expense into one
//  reporting a large interest cost. Only a NEGATIVE net value (net expense)
//  is a valid interest_expense; a positive one is reported missing rather
//  than flipped, because the sign is the one part of a net figure that
//  cannot be silently reinterpreted as its opposite.
const NET_INTEREST_TAGS = ["InterestIncomeExpenseNet"];

/**
 * Interest expense from one raw XBRL row, applying the net-tag sign rule.
 * `tag` is the SERIES' tag (pickAnnualSeries picks one tag for the whole
 * series), so the rule is evaluated once per series, not per row — but is
 * expressed as a per-row function since it is applied to every row when
 * building interest_expense_series.
 */
function interestExpenseFromRow(val, tag) {
  if (val == null) return null;
  if (NET_INTEREST_TAGS.includes(tag)) return val < 0 ? Math.abs(val) : null;
  return Math.abs(val);
}

/**
 * Project a pickAnnualSeries() result onto an externally-supplied list of
 * period ends (typically free_cash_flows' own ends), so a second series can
 * be reported ONE-TO-ONE with a first — null at any index whose period this
 * series didn't report, never shifted to fill the gap. `transform` lets the
 * caller apply a per-row rule (e.g. interestExpenseFromRow's sign check)
 * while still going through the same end-keyed lookup.
 */
function seriesAlignedTo(flowResult, ends, transform = (v) => v) {
  if (!flowResult) return ends.map(() => null);
  const byEnd = new Map(flowResult.series.map((r) => [r.end, transform(r.val, flowResult.tag)]));
  return ends.map((e) => (byEnd.has(e) ? byEnd.get(e) : null));
}

//: IFRS classification tags are disclosure flags, not monetary facts — some
//  filers tag them with a boolean-ish "pure" unit and no numeric magnitude at
//  all. Presence of ANY row is therefore what "disclosed" means here; there
//  is no amount to filter by duration or restatement the way pickAnnualSeries
//  does for real financial concepts.
function ifrsInterestClassification(facts) {
  const hasDisclosure = (tag) => {
    const f = facts[tag];
    if (!f || !f.units) return false;
    return Object.values(f.units).some((rows) => Array.isArray(rows) && rows.length > 0);
  };
  if (hasDisclosure("InterestPaidClassifiedAsOperatingActivities")) return "operating";
  if (hasDisclosure("InterestPaidClassifiedAsFinancingActivities")) return "financing";
  return null;
}

//: Many ifrs-full filers (Infosys among them) tag neither
//  InterestPaidClassifiedAs* disclosure at all, even though IAS 7 still
//  requires them to classify interest paid consistently — the classification
//  exists as a real policy, it's just not named directly. Lease cash flows
//  give indirect evidence of it: a filer whose PAYMENTS classified as
//  financing activities cover (nearly) all of its total lease cash outflow
//  must be including lease INTEREST inside that financing figure too, since
//  principal alone is only part of a lease payment — covering ~100% of the
//  total this way is the entity's policy showing through, not a coincidence.
//  A financing figure materially BELOW the total means the missing piece
//  (interest) is booked elsewhere — operating activities, by elimination.
//  A ratio in between is too close to call from this evidence alone and
//  stays null rather than guessing.
const LEASE_INTEREST_FINANCING_RATIO = 0.99;
const LEASE_INTEREST_OPERATING_RATIO = 0.90;

/**
 * Fallback for ifrsInterestClassification() when no explicit
 * InterestPaidClassifiedAs* tag is disclosed. Compares the SAME latest
 * period's CashOutflowForLeases (total) against
 * PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities (financing-only)
 * — both required, and required to share a period end, since comparing
 * different years would compare unrelated numbers. Verified live: INFY
 * FY2025-03-31 tags both at 278,000,000 (ratio 1.00) -> "financing".
 */
function inferIfrsLeaseInterestClassification(facts, prefer) {
  const total = pickAnnualSeries(facts, ["CashOutflowForLeases"], 1, prefer);
  const financing = pickAnnualSeries(
    facts, ["PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities"], 1, prefer);
  if (!total || !financing) return null;
  const totalRow = total.series[total.series.length - 1];
  const financingRow = financing.series[financing.series.length - 1];
  if (totalRow.end !== financingRow.end) return null;   // different periods aren't comparable
  const totalVal = Math.abs(totalRow.val);
  if (totalVal <= 0) return null;
  const ratio = Math.abs(financingRow.val) / totalVal;
  if (ratio >= LEASE_INTEREST_FINANCING_RATIO) return "financing";
  if (ratio <= LEASE_INTEREST_OPERATING_RATIO) return "operating";
  return null;
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
//: Yahoo's chart endpoint fails intermittently (observed: the same AAPL
//  request returning no series, then a full 58-month one seconds later).
//  One failure used to silently cost that load its beta, realised
//  volatility, correlation and real Fama-French regression — every model
//  fell back to its fixed guesses. One short retry absorbs the transient
//  case; a persistent failure still degrades to null, as before.
async function fetchMonthlyClosesWithRetry(symbol) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const series = await fetchMonthlyCloses(symbol).catch(() => null);
    if (series && series.length) return series;
    if (attempt === 0) await new Promise((r) => setTimeout(r, 400));
  }
  return null;
}

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
//: Yahoo bars are stamped at the month START but their close is the month-
//  END close (a 2026-08-01T04:00Z bar's close IS August's month-end price,
//  not July's) — so the return from the July bar to the August bar is
//  August's return, and monthKey of the LATER point in a pair is the right
//  label for that pair's return. Verified live: KO's Aug-2026 bar close vs
//  its Jul-2026 bar close reproduces the same return this function labels
//  202608, matching the change in KO's independently-fetched adjusted closes
//  for that month.
const monthKey = (unixSeconds) => {
  const d = new Date(unixSeconds * 1000);
  return d.getUTCFullYear() * 12 + d.getUTCMonth();
};

//: monthKey (year*12 + zero-based month) back to a YYYYMM integer label,
//  e.g. 2026*12+7 -> 202608.
const monthKeyToLabel = (key) => Math.floor(key / 12) * 100 + (key % 12) + 1;

//: The current UTC calendar month. A month equal to this one is still open —
//  its bar (if any) reflects a mid-month price, not a real month-end close —
//  so every caller below excludes it rather than reporting a partial return
//  as if it were a completed one.
const currentMonthKey = () => monthKey(Date.now() / 1000);

//: Yahoo's `interval=1mo` chart response ends with one extra LIVE point
//  stamped inside the still-open current month, appended after that month's
//  regular bar (e.g. a 2026-09-01T04:00Z bar, then a live 2026-09-23T16:26Z
//  quote — both fall in the same calendar month). Left alone, that live
//  point becomes a second observation bucketed under the same monthKey and,
//  joined into a return pair, silently overwrites the real month's return
//  with a near-zero month-to-date figure. Collapse to one close per calendar
//  month FIRST — the last observation seen in it — before any return is
//  computed from the series.
function dedupeMonthlyCloses(series) {
  const byMonth = new Map();
  for (const pt of series) byMonth.set(monthKey(pt.t), pt); // series is ascending; last write wins
  return [...byMonth.values()].sort((a, b) => a.t - b.t);
}

/**
 * Simple returns from a close series, one per calendar month, keyed by the
 * month they END in (the later bar's month — see monthKey above). Restricted
 * to COMPLETED months: the still-open current month is dropped, since its
 * "return" so far is a partial month-to-date figure that would otherwise
 * enter beta/vol/correlation as an artificially small (or occasionally
 * large) observation.
 */
function monthlyReturns(series) {
  const deduped = dedupeMonthlyCloses(series);
  const cur = currentMonthKey();
  const byMonth = new Map();
  for (let i = 1; i < deduped.length; i++) {
    const key = monthKey(deduped[i].t);
    if (key === cur) continue;
    const prev = deduped[i - 1].c, close = deduped[i].c;
    if (prev > 0) byMonth.set(key, close / prev - 1);
  }
  return byMonth;
}

//: [YYYYMM, r] pairs, ascending, r rounded to 6dp — the shape exposed on the
//  response as `monthly_returns`. Built from the same Map monthlyReturns()
//  already produces, so no extra network call or recomputation is involved.
function monthlyReturnPairs(returnsMap) {
  return [...returnsMap.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([key, r]) => [monthKeyToLabel(key), Number(r.toFixed(6))]);
}

//: A beta needs enough observations to mean anything. 24 monthly points is
//  the floor; below that the estimate swings wildly on one outlier month, and
//  returning a confidently-wrong 2.4 is worse than returning nothing and
//  letting the documented sector median apply.
const MIN_BETA_OBSERVATIONS = 24;

//: Sample standard deviation (n-1 denominator) of a list of monthly simple
//  returns, annualised by the usual sqrt(12) scaling of i.i.d. monthly vol.
//  Exported standalone so it can be checked against a hand-computed series.
function annualizedVol(xs) {
  const n = xs.length;
  if (n < 2) return null;
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  const variance = xs.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1);
  return Math.sqrt(variance) * Math.sqrt(12);
}

/**
 * Pure regression core: beta, realised vol (stock and market) and their
 * correlation from aligned (xs = market, ys = stock) monthly-return pairs.
 * Split out from computeBeta() so it can be exercised directly against a
 * hand-computed fixture, with no network call involved.
 *
 * Downstream models (options, Heston, VaR, MPT) were running on fixed
 * guesses (25% stock vol, 18% market vol, 0.6 correlation) even though this
 * same regression already had the real, symbol-specific numbers sitting
 * right there — they were just never returned alongside beta.
 *
 * Sanity guards mirror the beta guard: a vol or correlation outside a
 * plausible range is far more likely a data artifact than a real risk
 * profile, so it comes back null rather than as a number a model would take
 * at face value.
 */
function regressionStats(xs, ys) {
  if (xs.length < MIN_BETA_OBSERVATIONS || xs.length !== ys.length) return null;

  const n = xs.length;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let cov = 0, varm = 0, vars = 0;
  for (let i = 0; i < n; i++) {
    cov += (xs[i] - mx) * (ys[i] - my);
    varm += (xs[i] - mx) ** 2;
    vars += (ys[i] - my) ** 2;
  }
  if (varm <= 0) return null;                 // a flat market has no beta
  const beta = cov / varm;
  //: A beta outside this range is almost always a data artifact (a bad
  //  split adjustment, a near-dead ticker), not a real risk profile. Reject
  //  rather than pass a number the DCF would take at face value.
  if (!Number.isFinite(beta) || beta < -3 || beta > 5) return null;

  const stockVol = annualizedVol(ys);
  const marketVol = annualizedVol(xs);
  //: Pearson correlation from the same sums the regression already computed
  //  — cov/(sd_x * sd_y), using population sd (consistent with cov/varm above)
  //  rather than the sample sd used for the reported vols.
  const sdxPop = Math.sqrt(varm / n), sdyPop = Math.sqrt(vars / n);
  const correlation = sdxPop > 0 && sdyPop > 0 ? cov / n / (sdxPop * sdyPop) : null;

  //: (0, 3] admits even a near-meme-stock vol (300%/yr) while rejecting a
  //  zero or non-finite result as a data artifact, not a real profile.
  const volOk = (v) => Number.isFinite(v) && v > 0 && v <= 3;
  const corrOk = Number.isFinite(correlation) && correlation >= -1 && correlation <= 1;

  return {
    beta, observations: n,
    stockVol: volOk(stockVol) ? stockVol : null,
    marketVol: volOk(marketVol) ? marketVol : null,
    correlation: corrOk ? correlation : null,
  };
}

async function computeBeta(symbol) {
  const bench = benchmarkFor(symbol);
  const [stock, market] = await Promise.all([
    fetchMonthlyClosesWithRetry(symbol),
    fetchMonthlyClosesWithRetry(bench.symbol),
  ]);
  if (!stock || !market) return null;

  const rs = monthlyReturns(stock), rm = monthlyReturns(market);
  const xs = [], ys = [];
  for (const [m, sr] of rs) {
    const mr = rm.get(m);
    if (typeof mr === "number") { ys.push(sr); xs.push(mr); }
  }
  const stats = regressionStats(xs, ys);
  if (!stats) return null;

  //: The stock's own completed-month returns (not intersected with the
  //  benchmark's calendar) — exposed on the response as `monthly_returns`.
  //  Reuses `rs`, already computed above from the same fetch; no second call.
  return { ...stats, benchmark: bench.name, benchmarkSymbol: bench.symbol,
           monthlyReturns: monthlyReturnPairs(rs) };
}

/* ------------------------------- ADR ratios ------------------------------- *
 * A depositary receipt is not one ordinary share. HDFC Bank's ADS represents
 * three equity shares, ICICI's two; Infosys, Wipro and Dr Reddy's are 1:1.
 * EDGAR reports the company's TOTAL ORDINARY shares while the quote fetched
 * beside it is per ADS, so multiplying the two overstates market cap — and
 * every per-share figure derived from it — by exactly that ratio. For HDB
 * that is a 3x error on the headline number, with nothing in the output to
 * suggest anything is wrong.
 *
 * The ratio is NOT hardcoded from memory. It is DERIVED at request time from
 * the two live prices:
 *
 *     ratio = (adr_price_usd x usd_inr) / local_price_inr
 *
 * then snapped to the nearest conventional ratio only if it lands close to
 * one. Measured against real quotes this comes out at 1.028 (INFY), 0.999
 * (WIT), 3.043 (HDB), 2.063 (IBN) and 0.989 (RDY) — within ~3%, the rest
 * being non-simultaneous closes and FX timing.
 *
 * That arithmetic is what makes the mapping below safe to maintain: if a
 * company changes its ratio, or an entry here is simply wrong, the implied
 * figure stops landing near a clean value and the whole conversion is
 * refused rather than applied on a stale assumption.                        */

//: ADR ticker -> its home listing. Only entries whose ratio has actually been
//  reconciled against live quotes belong here; a guess adds no coverage,
//  because an unreconcilable entry is refused at request time anyway.
//
//  Everything past the Indian five was added after the 2026-09 audit found
//  TSM's ordinary share count published beside its per-ADS price (market cap
//  ~$11.6T, 5x). Each was reconciled live on 2026-09-24; the implied ratio is
//  in the trailing comment. A Form 20-F filer NOT listed here (or in
//  ADR_PINNED_RATIOS) has its share count withheld — see the handler.
const ADR_LOCAL_LISTINGS = {
  INFY: "INFY.NS",        // Infosys
  WIT: "WIPRO.NS",        // Wipro
  HDB: "HDFCBANK.NS",     // HDFC Bank
  IBN: "ICICIBANK.NS",    // ICICI Bank
  RDY: "DRREDDY.NS",      // Dr Reddy's Laboratories
  BABA: "9988.HK",  BIDU: "9888.HK",  JD: "9618.HK",   NTES: "9999.HK",   // 7.89, 8.14, 2.02, 4.93
  TCOM: "9961.HK",  LI: "2015.HK",    NIO: "9866.HK",  XPEV: "9868.HK",   // 1.02, 1.97, 1.02, 2.01
  TM: "7203.T",     SONY: "6758.T",   MUFG: "8306.T",  HMC: "7267.T",     // 10.14, 1.00, 1.01, 2.93
  SAP: "SAP.DE",    ASML: "ASML.AS",  ING: "INGA.AS",  PHG: "PHIA.AS",    // 0.99, 1.01, 0.99, 0.99
  NVO: "NOVO-B.CO", NVS: "NOVN.SW",   LOGI: "LOGN.SW", ABBNY: "ABBN.SW",  // 1.00, 1.00, 0.98, 1.00
  SHEL: "SHEL.L",   AZN: "AZN.L",     BP: "BP.L",      UL: "ULVR.L",      // 2.01, 0.99, 6.02, 1.00
  HSBC: "HSBA.L",   GSK: "GSK.L",     DEO: "DGE.L",    RIO: "RIO.L",      // 4.99, 1.99, 4.01, 1.00
  BTI: "BATS.L",    RELX: "REL.L",    VOD: "VOD.L",    BCS: "BARC.L",     // 1.00, 1.00, 9.93, 3.98
  LYG: "LLOY.L",    NGG: "NG.L",      BHP: "BHP.AX",   TTE: "TTE.PA",     // 3.96, 5.01, 1.98, 1.01
  SNY: "SAN.PA",    SAN: "SAN.MC",    BBVA: "BBVA.MC", E: "ENI.MI",       // 0.50, 0.99, 0.99, 2.00
  EQNR: "EQNR.OL",  STLA: "STLAM.MI", ERIC: "ERIC-B.ST", NOK: "NOKIA.HE", // 1.00, 0.98, 1.00, 1.02
  ARGX: "ARGX.BR",  KB: "105560.KS",  SHG: "055550.KS", PKX: "005490.KS", // 0.99, 0.99, 1.00, 0.25
  CHT: "2412.TW",   UMC: "2303.TW",   ASX: "3711.TW",                     // 10.13, 5.09, 2.00
};

//: Depositary ratios that are stated rather than derived, for two cases the
//  live derivation cannot handle:
//   - TSM trades at a persistent premium to its Taipei shares (implied 5.74
//     against the contractual 5 on 2026-09-24), which the nearest-candidate
//     snap would round to 6. The contractual ratio is used, and the live
//     figure is still checked against it with a band wide enough for the
//     premium but not for an increased ratio: a move to 6 at the same
//     premium implies ~6.9, 38% off. (A cut to 4 would imply ~4.6 and pass;
//     price alone cannot separate that from a smaller premium.)
//   - Listings with no home market to price against. These are ordinary
//     shares or 1:1 ADSs except PDD (1 ADS = 4 Class A ordinary shares), and
//     are reported as stated, not verified.
const ADR_PINNED_RATIOS = {
  TSM: { ratio: 5, local: "2330.TW" },
  SPOT: { ratio: 1, local: null },
  ARM: { ratio: 1, local: null },
  SE: { ratio: 1, local: null },
  NU: { ratio: 1, local: null },
  PDD: { ratio: 4, local: null },
};
const ADR_PINNED_BAND = 0.30;

//: Conventional depositary ratios. A derived figure that matches none of
//  these within tolerance is treated as unverified, not rounded to the
//  closest anyway. 0.25 (POSCO) and 8 (Alibaba, Baidu) were added with the
//  wider ADR map; the tightest gap between neighbours is still 20% (8 -> 10).
const ADR_RATIO_CANDIDATES = [0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10];

//: Real-quote error was 0.1-3.1%; 8% leaves room for a volatile day and a
//  stale FX print while still rejecting a ratio that is genuinely wrong (the
//  gap between adjacent candidates is never smaller than 25%).
const ADR_RATIO_TOLERANCE = 0.08;

let fxCache = null, fxCacheAt = 0;
const FX_TTL = 3600 * 1000;

async function usdTo(currency) {
  if (currency === "USD") return 1;
  if (fxCache && Date.now() - fxCacheAt < FX_TTL && fxCache[currency]) return fxCache[currency];
  try {
    const r = await fetch("https://open.er-api.com/v6/latest/USD",
      { signal: AbortSignal.timeout(8000) });
    if (!r.ok) return null;
    const j = await r.json();
    if (!j || !j.rates) return null;
    fxCache = j.rates; fxCacheAt = Date.now();
    return fxCache[currency] || null;
  } catch { return null; }
}

/**
 * Ordinary shares represented by one ADS, or null when it cannot be verified.
 *
 * Returning null is a real outcome, not a failure to try: the caller drops
 * the share count entirely rather than publishing one that cannot be
 * reconciled with the price beside it.
 */
async function deriveAdrRatio(adrSymbol, adrPrice) {
  const sym = adrSymbol.toUpperCase();
  const pinned = ADR_PINNED_RATIOS[sym];
  if (pinned && !pinned.local) {
    return { ratio: pinned.ratio, implied: null, error: null, localSymbol: null, stated: true };
  }
  const local = pinned ? pinned.local : ADR_LOCAL_LISTINGS[sym];
  if (!local || !adrPrice || adrPrice.currency !== "USD") return null;

  const localQuote = await fetchPrice(local);
  if (!localQuote || !localQuote.price) return null;
  const fx = await usdTo(localQuote.currency);
  if (!fx) return null;

  const implied = (adrPrice.price * fx) / localQuote.price;
  if (!Number.isFinite(implied) || implied <= 0) return null;

  if (pinned) {
    const error = Math.abs(implied - pinned.ratio) / pinned.ratio;
    if (error > ADR_PINNED_BAND) return null;
    return { ratio: pinned.ratio, implied, error, localSymbol: local, stated: true,
             localPrice: localQuote.price, localCurrency: localQuote.currency, fx };
  }

  const nearest = ADR_RATIO_CANDIDATES
    .reduce((a, b) => (Math.abs(b - implied) < Math.abs(a - implied) ? b : a));
  const error = Math.abs(nearest - implied) / nearest;
  if (error > ADR_RATIO_TOLERANCE) return null;   // no conventional ratio fits

  return { ratio: nearest, implied, error, localSymbol: local,
           localPrice: localQuote.price, localCurrency: localQuote.currency, fx };
}

/* ----------------------------- share freshness ----------------------------- *
 * A filed share count is an instant that can predate the live price by a year
 * or more, and the dominant thing that goes wrong in that gap is a corporate
 * action: a split or bonus issue mechanically multiplies the count, dwarfing
 * gradual drift from buybacks or fresh issuance. HDFC Bank's own 1:1 bonus
 * (Aug 2025) and Wipro's (Dec 2024) each doubled shares outstanding after the
 * filing EDGAR had on hand — one testable case (Wipro) understated its market
 * cap by half, the other (HDFC Bank, only discovered while verifying this fix)
 * was silently wrong the same way and had gone unnoticed.
 *
 * This is fixable rather than just flaggable: Yahoo's keyless chart endpoint
 * (already used for price and beta — no new external dependency) discloses
 * `events.splits`, so a share count can be rolled forward instead of merely
 * captioned as stale.                                                        */

//: A split's "record date" and the date a filing's cover page can truthfully
//  call the new count "outstanding" are not the same day — Wipro's bonus
//  record date was 2024-12-03, one day before EDGAR's own as-of date of
//  2024-12-04, yet the filed count was still the PRE-bonus figure (verified
//  against Wipro's actual ~10.47B post-bonus count). Allotment lags the
//  record date by up to about two weeks, so a split just inside that window
//  is treated as not yet reflected rather than skipped on a same-side date
//  comparison that would otherwise miss exactly the case this exists for.
const SPLIT_ALLOTMENT_LAG_MS = 14 * 86_400_000;

/**
 * Pure half of the split lookup: given Yahoo's raw `events.splits` object and
 * the ISO date a share count was filed as-of, return the cumulative
 * split/bonus multiplier for whatever hadn't yet been allotted at that date.
 * Separated from the fetch below so it can be tested against real fixture
 * data without a network call.
 *
 * `splitsObj === null` means the lookup COULDN'T be done (the fetch failed)
 * and is distinct from `{}`, which means it succeeded and genuinely found
 * nothing — collapsing the two would silently present an unverified share
 * count as though it had been checked, so null propagates rather than being
 * treated as "no splits".
 */
function computeSplitAdjustment(splitsObj, sinceISODate) {
  const sinceMs = Date.parse(sinceISODate);
  if (!Number.isFinite(sinceMs)) return null;
  if (splitsObj == null) return null;                        // fetch failed — unknown, not "none"
  if (typeof splitsObj !== "object") return { ratio: 1, events: [] };

  const events = Object.values(splitsObj)
    .filter(s => typeof s?.date === "number" && s.numerator > 0 && s.denominator > 0
      && s.date * 1000 > sinceMs - SPLIT_ALLOTMENT_LAG_MS)
    .sort((a, b) => a.date - b.date);
  if (!events.length) return { ratio: 1, events: [] };

  const ratio = events.reduce((acc, s) => acc * (s.numerator / s.denominator), 1);
  return { ratio, events };
}

/**
 * `symbol`'s raw split history from Yahoo's keyless chart endpoint, or null
 * on failure. Fetched ONCE and shared: the share count and the disclosed
 * dividend can each predate a different corporate action (a filing's balance
 * sheet and income statement don't always carry the same as-of date), so
 * each needs its own call to computeSplitAdjustment() against its own
 * reference date — refetching per figure would double the network cost for
 * data that doesn't change.
 */
async function fetchRawSplits(symbol) {
  try {
    const r = await fetch(
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}`
        + `?interval=1mo&range=10y&events=split`,
      { headers: { "User-Agent": "Mozilla/5.0 (compatible; FinModelsTerminal/1.0)" },
        signal: AbortSignal.timeout(8000) });
    if (!r.ok) return null;
    const j = await r.json();
    return j?.chart?.result?.[0]?.events?.splits || {};
  } catch { return null; }
}

/** Live share price — the one figure EDGAR structurally cannot provide. */
const MINOR_UNIT_QUOTES = {
  GBp: { ccy: "GBP", div: 100 }, GBX: { ccy: "GBP", div: 100 },
  ZAc: { ccy: "ZAR", div: 100 }, ILA: { ccy: "ILS", div: 100 },
};

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
    //: regularMarketTime is Yahoo's epoch SECONDS for the quote, distinct from
    //  the fetch time — a quote can be stale (last close, after-hours) even
    //  though the request just succeeded, and nothing else in this payload
    //  says when the price is actually as of.
    const asOf = typeof meta?.regularMarketTime === "number"
      ? new Date(meta.regularMarketTime * 1000).toISOString() : null;
    if (!(typeof px === "number" && px > 0)) return null;
    //: London, Johannesburg and Tel Aviv quote in minor units (GBp, ZAc,
    //  ILA). There is no FX rate for "GBp", so every UK home listing failed
    //  the ADR check; normalise to the major unit here, once.
    const minor = MINOR_UNIT_QUOTES[meta.currency];
    return minor ? { price: px / minor.div, currency: minor.ccy, asOf }
                 : { price: px, currency: meta.currency || "USD", asOf };
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
    if (betaInfo && (betaInfo.stockVol !== null || betaInfo.correlation !== null)) {
      notes.push(`Realised volatility, benchmark volatility and their correlation are computed `
        + `from the same ${betaInfo.observations} monthly returns used for beta above, rather than `
        + `the pipeline's fixed 25%/18%/0.6 guesses that the options, Heston, VaR and MPT models `
        + `would otherwise fall back to.`);
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
        realized_volatility: betaInfo && betaInfo.stockVol !== null ? Number(betaInfo.stockVol.toFixed(4)) : null,
        market_volatility: betaInfo && betaInfo.marketVol !== null ? Number(betaInfo.marketVol.toFixed(4)) : null,
        market_correlation: betaInfo && betaInfo.correlation !== null ? Number(betaInfo.correlation.toFixed(4)) : null,
        return_observations: betaInfo ? betaInfo.observations : null,
        //: Same monthly-return series beta was computed from — see computeBeta().
        monthly_returns: betaInfo ? betaInfo.monthlyReturns : [],
        return_benchmark: betaInfo ? betaInfo.benchmarkSymbol : null,
        //: No EDGAR facts on this path at all, so there is no taxonomy to name.
        accounting_standard: null,
        currency: price.currency,
        free_cash_flows: [],
        //: This path has no EDGAR filing behind it at all, so every figure
        //  below that only ever comes from XBRL is null rather than omitted —
        //  same reasoning as the `missing` list below.
        finance_lease_liabilities: null,
        operating_lease_liabilities: null,
        stock_based_compensation: null,
        interest_expense_series: [],
        sbc_series: [],
        fcf_period_ends: [],
        interest_paid_classification: null,
        price_as_of: price.asOf ?? null,
        backends_used: ["market-data"],
      },
      //: Everything a full extraction would have carried is named here, so
      //  the caller reports real gaps instead of inferring them from absence.
      missing: ["company_name", "revenue", "free_cash_flows", "net_income", "total_debt",
                "cash_and_equivalents", "shares_outstanding", "dividend_per_share",
                "revenue_growth", "operating_margin", "tax_rate",
                "depreciation_amortization", "rd_expense", "capital_expenditures",
                "finance_lease_liabilities", "operating_lease_liabilities",
                "stock_based_compensation", "interest_expense_series", "sbc_series",
                "fcf_period_ends", "interest_paid_classification", "accounting_standard"],
      notes,
      beta: betaInfo || null,
      sources: [
        { name: "Share price", url: "Yahoo Finance chart API" },
        ...(betaInfo ? [{ name: `Beta vs ${betaInfo.benchmark} (${betaInfo.observations} monthly returns)`,
                          url: "Computed by OLS from price history" }] : []),
        ...(betaInfo && (betaInfo.stockVol !== null || betaInfo.correlation !== null)
          ? [{ name: "Realised volatility & correlation (same monthly return series as beta)",
               url: "Computed from price history" }] : []),
      ],
    };
  }
  return null;
}

const { clientIp, withinLimitLayered } = require("./_lib/net");

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

  // Every ticker here is uncached-per-caller upstream work (unlike quotes.js's
  // fixed tape basket, which is one shared CDN-cached fetch for everyone) —
  // and the heaviest of any endpoint in this app: a cold hit fans out to a
  // multi-MB SEC companyfacts JSON, SEC's ~1MB ticker directory (until warm),
  // a Yahoo price lookup, and sometimes an er-api FX/dividend lookup. That
  // makes it the cheapest-to-trigger, most-expensive-to-serve request in the
  // product, so it gets the tightest per-IP budget of any endpoint (versus
  // quotes.js's 30/60s and rates.js's 20/60s) — a human analyst pulling up
  // companies one at a time reads each result for a while before typing the
  // next ticker; a handful a minute, not a dozen a second. Layered with a
  // global backstop since clientIp() is only as trustworthy as whatever's in
  // front of this function — see the comment in _lib/net.js. The backstop's
  // window is kept short (5s) so a burst that exhausts the shared budget
  // self-clears in a few seconds rather than locking out every OTHER visitor
  // for up to a minute; its cap is sized down from quotes/rates' (50, 35) to
  // reflect that each fundamentals hit costs several times as much upstream
  // work as a quote or a rate lookup.
  //
  // Placed before loadTickerMap()/resolveTicker() below — the first upstream
  // fetch this handler can trigger — so a 429 short-circuits before any SEC,
  // Yahoo, or er-api call is made.
  if (!(await withinLimitLayered(`fundamentals:rl:${clientIp(req)}`, 10, 60, "fundamentals:rl:global", 20, 5))) {
    return res.status(429).json({ ok: false, error: "rate limited" });
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
    if (market) {
      if (market.fields.current_price == null || market.fields.beta == null) {
        res.setHeader("Cache-Control", CACHE_SHORT);
      }
      return res.status(200).json(market);
    }
    return res.status(404).json({
      ok: false, notFound: true,
      error: `"${ticker}" was not found as an SEC registrant or as a listed security. `
           + `Check the symbol, or upload the filing as a PDF.`,
    });
  }

  let facts, entityName, taxonomy = "us-gaap", deiFacts = null, predecessorNote = null;
  //: Started alongside companyfacts so it adds no latency.
  const filerInfoPromise = fetchFilerInfo(entry.cik);
  try {
    const t0 = Date.now();
    let r = await secFetch(FACTS_URL(entry.cik)).catch((e) => e);
    //: One retry, but only for a FAST failure (a 429/5xx or a dropped
    //  connection): a timed-out attempt has already spent most of the
    //  function's time budget, and the no-store below makes the visitor's
    //  own next request the retry instead.
    const failedFast = (r instanceof Error || !r.ok) && Date.now() - t0 < 4000
      && (r instanceof Error ? isTransient(r) : r.status === 429 || r.status >= 500);
    if (failedFast) {
      await new Promise((ok) => setTimeout(ok, 300));
      r = await secFetch(FACTS_URL(entry.cik)).catch((e) => e);
    }
    if (r instanceof Error) throw r;
    if (!r.ok) throw new Error(`companyfacts ${r.status}`);
    const j = await r.json();
    entityName = j.entityName || entry.title;
    //: A holding-company reorganisation gives the ticker a NEW registrant
    //  with no annual history (ExxonMobil Holdings, CIK 2115436, July 2026:
    //  10-Qs only, so XOM loaded no revenue or FCF at all). The predecessor's
    //  facts are merged underneath; the successor's rows win on any shared
    //  period because pickAnnualSeries/pickInstant keep the latest-filed row.
    const predecessor = PREDECESSOR_CIKS[entry.cik];
    if (predecessor) {
      const pr = await secFetch(FACTS_URL(predecessor)).catch(() => null);
      const pj = pr && pr.ok ? await pr.json().catch(() => null) : null;
      if (pj && pj.facts) {
        j.facts = mergeFacts(pj.facts, j.facts || {});
        predecessorNote = `Annual history before the ${entityName} reorganisation comes from `
          + `the predecessor registrant (CIK ${predecessor}); later filings from the current one.`;
      }
    }
    deiFacts = (j.facts && j.facts.dei) || null;
    //: A filer that moved from US GAAP to IFRS keeps both taxonomies in
    //  companyfacts, and the stale one must not win by default: Toyota and
    //  Sony switched in FY2021, so reading us-gaap first served their FY2020
    //  and FY2021 figures as current. The taxonomy whose annual revenue
    //  reaches the latest fiscal year is used; us-gaap wins a tie.
    const gaap = (j.facts && j.facts["us-gaap"]) || null;
    const ifrsFacts = (j.facts && j.facts["ifrs-full"]) || null;
    facts = gaap;
    if (ifrsFacts && (!gaap || latestAnnualEnd(ifrsFacts, IFRS_FLOW_TAGS.revenue)
                              > latestAnnualEnd(gaap, FLOW_TAGS.revenue))) {
      facts = ifrsFacts;
      taxonomy = "ifrs-full";
    }
    if (!facts) throw new Error("no us-gaap or ifrs-full facts");
  } catch (err) {
    //: Registered with SEC but no usable XBRL (a 404 on companyfacts, or a
    //  taxonomy we don't read). Market data is still real and still useful,
    //  so degrade to it rather than dead-ending the user.
    const transient = isTransient(err);
    if (transient) res.setHeader("Cache-Control", CACHE_NONE);
    const market = await marketOnly(ticker);
    if (market) {
      market.notes.unshift(transient
        ? `SEC filing data for ${ticker} didn't load this time `
          + `(${String(err.message).slice(0, 60)}), so only market data is shown. `
          + `This is usually temporary — load the ticker again.`
        : `No machine-readable XBRL filing data for ${ticker} `
          + `(${String(err.message).slice(0, 60)}), so only market data is shown.`);
      if (!transient && (market.fields.current_price == null || market.fields.beta == null)) {
        res.setHeader("Cache-Control", CACHE_SHORT);
      }
      return res.status(200).json(market);
    }
    return res.status(502).json({
      ok: false,
      error: `SEC filing data unavailable for ${ticker} (${String(err.message).slice(0, 80)}) — upload the filing as a PDF instead.`,
    });
  }

  const notes = [];
  if (predecessorNote) notes.push(predecessorNote);
  const flows = {}, stocks = {};
  const flowTags = taxonomy === "ifrs-full" ? IFRS_FLOW_TAGS : FLOW_TAGS;
  const stockTags = taxonomy === "ifrs-full" ? IFRS_STOCK_TAGS : STOCK_TAGS;

  //: Establish the filing's reporting currency BEFORE reading any figure, and
  //  hold every concept to it: the currency reaching the newest fiscal year,
  //  USD on a tie (see detectReportingCurrency). A USD price beside non-USD
  //  financials is converted below, never silently mixed in.
  const reportingCurrency = detectReportingCurrency(facts, flowTags.revenue);
  for (const [k, tags] of Object.entries(flowTags)) {
    flows[k] = pickAnnualSeries(facts, tags, 6, reportingCurrency, k === "revenue");
  }
  //: Every income and cash-flow concept must describe the same fiscal year
  //  as revenue. A revenue tag the filer abandoned years ago (Morgan Stanley's
  //  `Revenues` ended FY2014, Wells Fargo's FY2019) is still the freshest
  //  candidate when nothing newer is in the cascade, and it paired 2014
  //  revenue with 2025 net income into a 49% "margin". Revenue more than a
  //  year behind the company's own net income or operating cash flow is
  //  stale, not current: drop it (it is reported missing) rather than show it.
  let staleRevenueDropped = false;
  {
    const lastEnd = (k) => flows[k]?.series?.length ? flows[k].series[flows[k].series.length - 1].end : null;
    const freshest = [lastEnd("net_income"), lastEnd("operating_cash_flow")].filter(Boolean).sort().pop();
    const revEnd = lastEnd("revenue");
    if (freshest && revEnd && (Date.parse(freshest) - Date.parse(revEnd)) / 86_400_000 > 400) {
      notes.push(`Revenue is not shown: the only revenue tag this filer still carries ends at ${revEnd}, `
        + `while its net income runs to ${freshest}, so showing it would mix fiscal years.`);
      flows.revenue = null;
      staleRevenueDropped = true;
    }
  }
  for (const [k, tags] of Object.entries(stockTags)) {
    //: Share counts are unit-'shares' so the currency pin doesn't apply, and
    //  they get the fragment guard — see pickInstant's minRelative.
    stocks[k] = k === "shares_outstanding"
      ? pickInstant(facts, tags, null, 0.01, false)
      : pickInstant(facts, tags, reportingCurrency);
  }
  //: The cover-page count (dei:EntityCommonStockSharesOutstanding) is the
  //  filer's own statement of shares outstanding at the filing date. Used when
  //  the statements tag no count (SAP tags only shares ISSUED) or when it is
  //  a year or more fresher (TSMC's 2025 20-F carries the cover count but no
  //  statement facts). Skipped for multi-class filers — two values on one
  //  date (Berkshire A and B) cannot be told apart without dimensions.
  //: Only a CURRENT cover count: Berkshire stopped tagging a plain one in
  //  2011 (its classes are now dimensioned), and that 941,481 figure beside a
  //  2026 price read as a $0.5bn company. It must be no older than half a
  //  year before the latest fiscal year end.
  //: When the stale-revenue guard above dropped the series, net income's
  //  year stands in so the cover-count and weighted-average share fallbacks
  //  still have a date. A filer that never tagged revenue keeps today's
  //  behaviour (no date, so no fallback).
  const latestRevEnd = flows.revenue?.series?.length
    ? flows.revenue.series[flows.revenue.series.length - 1].end
    : (staleRevenueDropped && flows.net_income?.series?.length
        ? flows.net_income.series[flows.net_income.series.length - 1].end : null);
  const coverCandidate = pickCoverShares(deiFacts);
  const deiShares = coverCandidate && latestRevEnd
    && (Date.parse(latestRevEnd) - Date.parse(coverCandidate.end)) / 86_400_000 <= 180
    ? coverCandidate : null;
  const statementShares = stocks.shares_outstanding;
  if (deiShares && (!statementShares
      || (Date.parse(deiShares.end) - Date.parse(statementShares.end)) / 86_400_000 > 180)) {
    stocks.shares_outstanding = deiShares;
    notes.push(`Share count is the ${deiShares.value.toLocaleString("en-US")} shares outstanding `
      + `stated on the filing's cover page as of ${deiShares.end}`
      + (statementShares ? `, fresher than the ${statementShares.end} balance-sheet count.` : "."));
  }
  //: Last resort: the EPS denominator. META tags no point-in-time count at
  //  all (two classes, cover page refused) and AstraZeneca tags neither a
  //  count nor a cover page — both left market cap blank. Basic weighted-
  //  average shares for the latest period is the filer's own figure for the
  //  shares one EPS is spread across, across every class; it lags a point-
  //  in-time count only by the period's buybacks or issuance.
  const wavg = pickWeightedAverageShares(facts, taxonomy);
  const current = stocks.shares_outstanding;
  if (wavg && latestRevEnd
      && (Date.parse(latestRevEnd) - Date.parse(wavg.end)) / 86_400_000 <= 180
      && (!current || (Date.parse(wavg.end) - Date.parse(current.end)) / 86_400_000 > 365)) {
    stocks.shares_outstanding = wavg;
    notes.push(`Share count is the ${Math.round(wavg.value).toLocaleString("en-US")} basic `
      + `weighted-average shares for the period ${wavg.start} to ${wavg.end} (the EPS denominator), `
      + `because the filing tags no current point-in-time count. It differs from today's count `
      + `only by buybacks or issuance since then.`);
  }
  if (taxonomy === "ifrs-full") {
    notes.push(`Figures come from a Form 20-F filed under IFRS, denominated in ${reportingCurrency}.`);
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
  //: The period `end` for each surviving row, in the SAME order — this is
  //  what interest_expense_series/sbc_series are projected onto below, so a
  //  year that free_cash_flows dropped (no matching capex) is dropped from
  //  those series too, and element i of every series always refers to the
  //  same fiscal year as free_cash_flows[i].
  let fcfPeriodEnds = [];
  if (flows.operating_cash_flow) {
    const capexByEnd = new Map(
      (flows.capital_expenditures?.series || []).map((r) => [r.end, r.val]));
    const fcfRows = flows.operating_cash_flow.series.map((r) => {
      const capex = capexByEnd.get(r.end);
      // Capex is reported as a positive outflow in the cash-flow statement.
      return capex == null ? null : { end: r.end, val: r.val - Math.abs(capex) };
    }).filter((v) => v !== null);
    freeCashFlows = fcfRows.map((r) => r.val);
    fcfPeriodEnds = fcfRows.map((r) => r.end);
    if (flows.capital_expenditures
        && freeCashFlows.length < flows.operating_cash_flow.series.length) {
      notes.push("Some years had operating cash flow but no matching capital-expenditure disclosure; those years are omitted from the FCF series rather than assumed zero.");
    }
    if (!flows.capital_expenditures) {
      freeCashFlows = [];
      fcfPeriodEnds = [];
      notes.push("No capital-expenditure tag found, so free cash flow could not be derived from operating cash flow.");
    }
  }

  //: When capex is not tagged for enough years to build the 3-year base
  //  (Toyota, Infosys, PDD tag no PP&E purchase line), use depreciation &
  //  amortisation as the capex stand-in: OCF − D&A is the standard
  //  maintenance-capex approximation, built from the filer's own cash flows,
  //  and far closer than the revenue × margin stand-in the pipeline would
  //  otherwise use. Flagged as `fcf_basis` so the DCF reports PARTIAL.
  let fcfBasis = freeCashFlows.length ? "reported" : null;
  //: Not for deposit-taking banks: their operating cash flow is loan and
  //  deposit flows (JPM's was −$148bn), so OCF − D&A means nothing there.
  const isBank = ["Deposits", "DepositsFromCustomers", "InterestBearingDepositLiabilities",
                  "DepositsFromBanks", "BalancesOnDemandDepositsFromCustomers",
                  "InterestIncomeOnLoansAndAdvancesToCustomers"].some((t) => facts[t]);
  if (!isBank && freeCashFlows.length < 3 && flows.operating_cash_flow && flows.depreciation_amortization) {
    const daByEnd = new Map(flows.depreciation_amortization.series.map((r) => [r.end, r.val]));
    const proxyRows = flows.operating_cash_flow.series
      .filter((r) => daByEnd.has(r.end))
      .map((r) => ({ end: r.end, val: r.val - Math.abs(daByEnd.get(r.end)) }));
    if (proxyRows.length >= 3) {
      freeCashFlows = proxyRows.map((r) => r.val);
      fcfPeriodEnds = proxyRows.map((r) => r.end);
      fcfBasis = "ocf_minus_da";
      const idx = notes.findIndex((n) => n.startsWith("No capital-expenditure tag found"));
      if (idx >= 0) notes.splice(idx, 1);
      notes.push(`Free cash flow is operating cash flow minus depreciation & amortisation for `
        + `${proxyRows.length} years: this filer tags no capital-expenditure line for enough years, `
        + `so D&A stands in for maintenance capex. Actual capex may differ; the DCF is marked PARTIAL.`);
    }
  }

  // --- interest expense / SBC series, aligned to fcfPeriodEnds ------------
  const interestExpenseSeries = seriesAlignedTo(flows.interest_expense, fcfPeriodEnds,
    interestExpenseFromRow);
  const sbcSeries = seriesAlignedTo(flows.stock_based_compensation, fcfPeriodEnds,
    (v) => (v == null ? null : Math.abs(v)));

  // --- lease liabilities ---------------------------------------------------
  // Kept out of STOCK_TAGS's simple cascade because the right figure depends
  // on WHICH shape a filer discloses (total tag vs. current+noncurrent split)
  // — see pickLeaseLiability(). IFRS 16 has no finance/operating split at
  // all, so IFRS filers get a single LeaseLiabilities lookup instead.
  //: Cash, investments and debt all from ONE balance sheet — see
  //  pickBalanceSheet and the DEBT_TAGS note for the four ways the old
  //  independent picks went wrong.
  const bs = pickBalanceSheetWithFallback(facts, taxonomy, reportingCurrency);
  let financeLease, operatingLease;
  if (taxonomy === "ifrs-full") {
    financeLease = pickInstant(facts, ["LeaseLiabilities"], reportingCurrency);
    //: TSMC gives its lease liability only in TWD; translate it at the
    //  filer's own rate on the balance-sheet date, as pickBalanceSheet does.
    if (!financeLease && bs.anchor) {
      const rates = translationRates(facts, IFRS_STOCK_TAGS.cash_and_equivalents, bs.anchor, reportingCurrency);
      const v = valueAt(facts, ["LeaseLiabilities"], bs.anchor, reportingCurrency, rates);
      if (v) {
        financeLease = { value: v.value, end: bs.anchor };
        if (v.translatedFrom) bs.translated.push(`LeaseLiabilities (${v.translatedFrom})`);
      }
    }
    operatingLease = null;
    if (financeLease) {
      notes.push("This filer reports under IFRS 16, which has no operating/finance lease "
        + "split; the single LeaseLiabilities balance is reported as finance_lease_liabilities "
        + "and operating_lease_liabilities is left null.");
    }
  } else {
    financeLease = pickLeaseLiability(facts, LEASE_TAGS.financeTotal, LEASE_TAGS.financeCurrent,
      LEASE_TAGS.financeNoncurrent, reportingCurrency);
    operatingLease = pickLeaseLiability(facts, LEASE_TAGS.operatingTotal, LEASE_TAGS.operatingCurrent,
      LEASE_TAGS.operatingNoncurrent, reportingCurrency);
  }
  if (operatingLease && operatingLease.source === "noncurrent-only") {
    notes.push("operating_lease_liabilities reflects only the noncurrent portion; this filer "
      + "has no separate current operating-lease-liability tag.");
  }
  if (financeLease && financeLease.source === "noncurrent-only") {
    notes.push("finance_lease_liabilities reflects only the noncurrent portion; this filer "
      + "has no separate current finance-lease-liability tag.");
  }
  //: Guard against double-counting: if the tag already feeding total_debt has
  //  "Lease" in its own name, finance leases are already inside total_debt
  //  and reporting them again as a separate field would double-count them.
  let financeLeaseValue = financeLease ? financeLease.value : null;
  //: A lease figure is only disclosed annually by many filers (AAPL tags it
  //  in the 10-K alone), so it may trail the quarterly anchor — but not by
  //  more than a reporting cycle, or it describes a different company.
  const staleLease = (l) => l && bs.anchor
    && (Date.parse(bs.anchor) - Date.parse(l.end)) / 86_400_000 > 400;
  if (staleLease(financeLease)) {
    notes.push(`finance_lease_liabilities is reported null: the latest figure is as of `
      + `${financeLease.end}, more than a year before the ${bs.anchor} balance sheet.`);
    financeLeaseValue = null;
  }
  let operatingLeaseValue = operatingLease ? operatingLease.value : null;
  if (staleLease(operatingLease)) {
    notes.push(`operating_lease_liabilities is reported null: the latest figure is as of `
      + `${operatingLease.end}, more than a year before the ${bs.anchor} balance sheet.`);
    operatingLeaseValue = null;
  }
  if (financeLeaseValue !== null && bs.debtParts.some((p) => debtTagIncludesLeases(p.tag))) {
    notes.push("finance_lease_liabilities is reported null: the debt tag already used for "
      + "total_debt appears to include finance-lease liabilities, and reporting both would "
      + "double-count the same dollars.");
    financeLeaseValue = null;
  }

  // --- debt and cash ------------------------------------------------------
  const totalDebt = bs.debt;
  const cash = bs.cash;
  const sti = bs.sti;
  if (bs.anchor) {
    const lines = bs.debtParts.map((p) => p.tag).join(" + ");
    notes.push(`Cash and total debt are from the balance sheet dated ${bs.anchor}`
      + (lines ? `; total debt = ${lines}.` : "."));
    if (bs.fellBackFrom) {
      notes.push(`The newer balance sheet dated ${bs.fellBackFrom} tags no borrowings, so cash and `
        + `total debt are both taken from the ${bs.anchor} balance sheet instead.`);
    }
  }
  if (bs.neverBorrowed) {
    notes.push("Total debt is 0: this company has never tagged a borrowing of any kind in its "
      + "XBRL filings (no debt, notes, loans or maturity schedule), so it is treated as debt-free.");
  } else if (bs.fromComponents) {
    notes.push("Total debt is summed from the separate borrowing lines this filer uses (bank "
      + "loans, senior notes, convertibles) because it tags no total-debt line.");
  }
  if (totalDebt === null) {
    notes.push(`No debt line is tagged on the ${bs.anchor || "latest"} balance sheet in a form `
      + `this reader recognises, so total debt is reported missing rather than as zero. `
      + `Enter it manually if the company has borrowings.`);
  } else if (!bs.hasLongTerm || bs.ambiguousCurrent) {
    notes.push("Total debt may understate borrowings: no long-term debt line, or no separable "
      + "short-term line, was tagged on this balance sheet date. Check it against the filing.");
  }
  if (bs.translated.length) {
    notes.push(`Some balance-sheet lines were tagged only in the filer's home currency and were `
      + `converted at the filer's own translation rate for ${bs.anchor} (implied by the cash `
      + `balance it reports in both currencies): ${bs.translated.join(", ")}.`);
  }

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

  //: Operating (EBIT) margin from the tagged operating income for the SAME
  //  fiscal year as revenue — the definition the pipeline's sector baselines
  //  use. Only when a filer tags no operating income for that year (banks,
  //  most insurers) is net income used instead, and the note says so. The
  //  old code always used net income: MSFT read 40.3% against a true 46.8%.
  let operatingMargin = null;
  const opSeries = flows.operating_income?.series || [];
  const opLatest = opSeries.length ? opSeries[opSeries.length - 1] : null;
  const revLatestEnd = revSeries.length ? revSeries[revSeries.length - 1].end : null;
  if (revenue && revenue > 0 && opLatest && opLatest.end === revLatestEnd) {
    operatingMargin = opLatest.val / revenue;
  } else if (revenue && netIncome != null && revenue > 0) {
    operatingMargin = netIncome / revenue;
    notes.push("Operating margin shown is a net-income margin: this filer tags no operating income for its latest fiscal year.");
  }

  const symUpper = entry.symbol.toUpperCase();
  const pinnedAdr = ADR_PINNED_RATIOS[symUpper] || null;
  const isKnownAdr = !!ADR_LOCAL_LISTINGS[symUpper] || !!pinnedAdr;
  //: A Form 20-F filer is a foreign private issuer; its US line is almost
  //  always a depositary receipt whose ratio EDGAR does not record.
  //: Read off revenue when there is one, else off the latest filing of any
  //  cover or cash fact — a bank such as SMFG tags no revenue, and its
  //  ordinary share count went out beside its ADS price unchecked.
  const latestRevForm = revSeries.length ? String(revSeries[revSeries.length - 1].form || "")
                                         : latestFilingForm([deiFacts, facts]);
  const filesForm20F = /^20-F/.test(latestRevForm);
  const sharesAsOf = stocks.shares_outstanding?.end || null;
  //: EDGAR reports the underlying company's own share count, so a corporate
  //  action on that count happens on ITS listing, not the ADR ticker traded
  //  in New York — checking splits on the ADR itself would miss every one.
  const splitCheckSymbol = (pinnedAdr && pinnedAdr.local) || ADR_LOCAL_LISTINGS[symUpper]
                           || entry.symbol;

  //: entry.symbol, not the raw input — Yahoo also spells share classes
  //  with a hyphen, so a user's "BRK.B" must become "BRK-B" here too.
  //: Price, beta and the split check are all independent market lookups,
  //  fetched together — this endpoint is already the slowest thing in the
  //  intake, and none of the three depends on another.
  const [priceInfo, betaInfo, rawSplits] = await Promise.all([
    fetchPrice(entry.symbol),
    computeBeta(entry.symbol).catch(() => null),
    fetchRawSplits(splitCheckSymbol).catch(() => null),
  ]);
  const splitAdj = computeSplitAdjustment(rawSplits, sharesAsOf);
  //: Needs the ADR price, so it runs after that resolves rather than beside it.
  const adr = await deriveAdrRatio(entry.symbol, priceInfo).catch(() => null);
  if (!priceInfo) {
    notes.push("Live share price unavailable; enter it manually or the models will use their documented fallback.");
  }
  //: The price must be on the filing's currency basis. A USD quote beside
  //  EUR financials (SAP) or CAD ones (a TSX cross-listing) is converted at
  //  the live rate, which preserves meaning — a share's value in EUR is its
  //  USD value times EUR per USD. Any other mismatch is dropped, not mixed.
  let currentPrice = null;
  if (priceInfo) {
    if (!reportingCurrency || priceInfo.currency === reportingCurrency) {
      currentPrice = priceInfo.price;
    } else if (priceInfo.currency === "USD") {
      const fx = await usdTo(reportingCurrency);
      if (fx) {
        currentPrice = priceInfo.price * fx;
        notes.push(`Share price converted to ${reportingCurrency}, the currency this company `
          + `reports in: USD ${priceInfo.price.toFixed(2)} x ${fx.toFixed(4)} ${reportingCurrency}/USD `
          + `(live rate) = ${reportingCurrency} ${currentPrice.toFixed(2)}.`);
      }
    }
  }
  if (priceInfo && currentPrice === null) {
    notes.push(`Share price omitted: the quote is in ${priceInfo.currency} but this company `
      + `reports in ${reportingCurrency}, and no exchange rate was available to convert it. `
      + `Enter the price manually on the filing's own basis.`);
  }
  if (betaInfo) {
    notes.push(`Beta ${betaInfo.beta.toFixed(2)} is computed here by regressing `
      + `${betaInfo.observations} monthly returns against the ${betaInfo.benchmark}, `
      + `not taken from a filing — beta is a market statistic and is not disclosed in XBRL.`);
    if (betaInfo.stockVol !== null || betaInfo.correlation !== null) {
      notes.push(`Realised volatility, benchmark volatility and their correlation are computed `
        + `from the same ${betaInfo.observations} monthly returns used for beta above, rather than `
        + `the pipeline's fixed 25%/18%/0.6 guesses that the options, Heston, VaR and MPT models `
        + `would otherwise fall back to.`);
    }
  } else {
    notes.push("Beta could not be computed from price history (too few observations or no price series); the sector-median fallback will apply instead.");
  }

  //: A filed share count is an instant. Roll it forward through any split or
  //  bonus issue EDGAR's snapshot predates, BEFORE the ADR ratio is applied —
  //  the ratio is a fixed multiple of the ordinary count, so getting the
  //  ordinary count right first is what makes the ADS figure right too.
  //  Confirmed live: Wipro's Dec-2024 filing (5.23B) understated its real
  //  ~10.47B post-bonus count by exactly its 1:1 bonus; HDFC Bank's Mar-2025
  //  filing (7.65B ordinary) understated its real ~15.35B post-bonus count
  //  the same way — the second only surfaced while verifying this fix.
  let ordinaryShares = stocks.shares_outstanding?.value ?? null;
  const splitEvents = splitAdj?.events || [];
  if (ordinaryShares !== null && splitEvents.length) {
    const before = ordinaryShares;
    ordinaryShares = ordinaryShares * splitAdj.ratio;
    const desc = splitEvents.map(s => {
      const d = new Date(s.date * 1000).toISOString().slice(0, 10);
      return `${s.numerator}:${s.denominator} on ${d}`;
    }).join(", ");
    notes.push(`Share count adjusted for a corporate action not yet reflected in the filing `
      + `(${desc}, from ${splitCheckSymbol}'s public trading history): `
      + `${Math.round(before).toLocaleString("en-US")} becomes `
      + `${Math.round(ordinaryShares).toLocaleString("en-US")} ordinary shares.`);
  }

  /* ---- reconcile the share count with the price it will be multiplied by --
   * EDGAR reports total ORDINARY shares. The quote is per ADS. Publishing the
   * two together without converting overstates market cap by the depositary
   * ratio — 3x for HDFC Bank — and every per-share figure with it.          */
  let adsShares = ordinaryShares;
  if (isKnownAdr && ordinaryShares !== null) {
    if (adr && adr.stated) {
      adsShares = ordinaryShares / adr.ratio;
      notes.push(adr.implied === null
        ? `Share count on the listing's basis: 1 ADS/listed share = ${adr.ratio} ordinary `
          + `share${adr.ratio === 1 ? "" : "s"}. This listing has no home market to price against, `
          + `so the ratio is the depositary's stated one rather than a live-verified figure.`
        : `Share count converted to an ADS basis at the contractual 1 ADS = ${adr.ratio} ordinary `
          + `shares. ${Math.round(ordinaryShares).toLocaleString("en-US")} ordinary shares become `
          + `${Math.round(adsShares).toLocaleString("en-US")} ADS. The live prices imply `
          + `${adr.implied.toFixed(2)} against ${adr.localSymbol}: this ADR trades at a `
          + `${((adr.implied / adr.ratio - 1) * 100).toFixed(0)}% premium to the home shares, `
          + `which is a price difference, not a different ratio.`);
    } else if (adr) {
      adsShares = ordinaryShares / adr.ratio;
      notes.push(adr.ratio === 1
        ? `Depositary ratio verified as 1 ADS = 1 ordinary share (implied `
          + `${adr.implied.toFixed(3)} from the ${adr.localSymbol} price), so the share `
          + `count needs no further adjustment.`
        : `Share count converted to an ADS basis: 1 ADS = ${adr.ratio} ordinary shares, `
          + `derived from this listing's price against ${adr.localSymbol} and verified to `
          + `${(adr.error * 100).toFixed(1)}%. ${Math.round(ordinaryShares).toLocaleString("en-US")} `
          + `ordinary shares become ${Math.round(adsShares).toLocaleString("en-US")} ADS, so `
          + `market cap and every per-share figure line up with the quoted price.`);
    } else {
      //: Known ADR, unverifiable ratio — withhold rather than publish a count
      //  that silently disagrees with the price. A missing input falls back to
      //  a documented default; a wrong one is presented as a finding.
      adsShares = null;
      notes.push("This is a depositary receipt and the ADS-to-ordinary-share ratio could not "
        + "be verified against the home listing right now, so the share count is withheld "
        + "rather than reported on a basis that may not match the quoted price. Enter it "
        + "manually on an ADS basis if you need per-share or market-cap output.");
    }
  } else if (filesForm20F && ordinaryShares !== null) {
    //: A foreign private issuer this reader has no depositary ratio for. Its
    //  EDGAR count is ordinary shares; the quote is almost certainly per ADS.
    //  TSM, before it was mapped, published ~$11.6T of market cap this way.
    adsShares = null;
    notes.push("This company files Form 20-F, so its US listing is most likely a depositary "
      + "receipt, and this reader has no verified ADS-to-ordinary-share ratio for it. The "
      + `filing's ${Math.round(ordinaryShares).toLocaleString("en-US")} ordinary shares are `
      + "withheld rather than multiplied by a per-ADS price. Enter the share count manually on "
      + "the listing's basis for per-share and market-cap output.");
  }

  //: The split check only catches a corporate action — it cannot see a
  //  buyback or fresh issuance, which change the count gradually with no
  //  discrete event to look up. So a materially old filing still gets
  //  flagged even after adjustment, just with an accurate caveat about what
  //  was and wasn't corrected for.
  if (sharesAsOf && adsShares !== null) {
    const monthsOld = (Date.now() - Date.parse(sharesAsOf)) / (30.44 * 86_400_000);
    if (monthsOld > 15) {
      const caveat = splitEvents.length
        ? "Known splits and bonus issues since then have been applied above, but a buyback "
          + "or fresh share issuance would not be reflected."
        : "No split or bonus issue was found in the meantime, but a buyback or fresh share "
          + "issuance would not be reflected.";
      notes.push(`Share count is as of ${sharesAsOf}, ${Math.round(monthsOld)} months old — `
        + `older than one annual reporting cycle. ${caveat} Verify it before relying on `
        + `market cap or per-share figures.`);
    }
  } else if (splitAdj === null && sharesAsOf) {
    //: The split lookup itself failed (network/parse error, not "no splits
    //  found") — say so rather than silently presenting an unadjusted count
    //  as if it had already been checked.
    notes.push("Could not check for a stock split or bonus issue since the filing date; "
      + "the share count above has not been adjusted for one.");
  }

  //: A per-share dividend has the OPPOSITE exposure to a split from the share
  //  count: more shares now split the same payout, so a dividend disclosed
  //  BEFORE a bonus issue is DIVIDED by the ratio, not multiplied, to state
  //  it on the same per-share basis as the (already-adjusted) current price.
  //  Left uncorrected this doesn't fail loudly — it feeds straight into the
  //  Gordon Growth Model (assumptions.py) as `dividend`, which scales
  //  linearly with it, so a stale pre-bonus dividend would silently double
  //  that model's fair-value output for exactly the tickers this fix targets.
  //  Checked against the dividend's OWN period end, not the share count's —
  //  a filing's income statement and balance sheet don't always carry the
  //  same as-of date, so the two can need different corrections.
  let dividendPerShare = latestFlow("dividends_per_share");
  const dividendPeriodEnd = flows.dividends_per_share?.series?.length
    ? flows.dividends_per_share.series[flows.dividends_per_share.series.length - 1].end
    : null;
  if (dividendPerShare !== null && dividendPeriodEnd) {
    const dividendSplitAdj = computeSplitAdjustment(rawSplits, dividendPeriodEnd);
    const dividendSplitEvents = dividendSplitAdj?.events || [];
    if (dividendSplitEvents.length) {
      const before = dividendPerShare;
      dividendPerShare = dividendPerShare / dividendSplitAdj.ratio;
      const desc = dividendSplitEvents.map(s => {
        const d = new Date(s.date * 1000).toISOString().slice(0, 10);
        return `${s.numerator}:${s.denominator} on ${d}`;
      }).join(", ");
      notes.push(`Dividend per share adjusted for a corporate action since it was declared `
        + `(${desc}): ${before.toFixed(4)} becomes ${dividendPerShare.toFixed(4)} per `
        + `current share, so it is comparable to today's price rather than overstating yield.`);
    } else if (dividendSplitAdj === null) {
      notes.push("Could not check for a stock split or bonus issue since the dividend was "
        + "declared; the dividend per share above has not been adjusted for one.");
    }
  }

  //: A filed per-share dividend is per ORDINARY share; the quote beside it is
  //  per ADS. Put it on the listing's basis with the same ratio as the share
  //  count, or withhold it when the count was withheld — Gordon Growth prices
  //  the dividend directly, so HDB's (ratio 3) would otherwise read 3x low.
  if (dividendPerShare !== null && (isKnownAdr || filesForm20F)) {
    if (adsShares === null) {
      dividendPerShare = null;
      notes.push("Dividend per share withheld for the same reason as the share count: it is "
        + "filed per ordinary share and the depositary ratio needed to restate it per ADS is "
        + "unverified.");
    } else if (adr && adr.ratio !== 1) {
      const before = dividendPerShare;
      dividendPerShare = dividendPerShare * adr.ratio;
      notes.push(`Dividend per share restated per ADS: ${before.toFixed(4)} per ordinary share `
        + `x ${adr.ratio} = ${dividendPerShare.toFixed(4)}, the same basis as the quoted price.`);
    }
  }

  const latestEnd = revSeries.length ? revSeries[revSeries.length - 1].end
                                     : (staleRevenueDropped && flows.net_income?.series?.length
                                         ? flows.net_income.series[flows.net_income.series.length - 1].end
                                         : (stocks.cash_and_equivalents?.end || null));

  //: EDGAR's structured data can trail a filing by months: Toyota, Sony,
  //  Infosys, HDFC Bank and TSMC had 2026 20-Fs on file while companyfacts
  //  still held only the year before. Nothing in the figures shows that, so
  //  say it whenever the newest annual report ends ten months or more past
  //  the year the data covers.
  const filerInfo = await filerInfoPromise;
  const newest = filerInfo.latestAnnual;
  const dataEnd = flows.net_income?.series?.length
    ? flows.net_income.series[flows.net_income.series.length - 1].end : latestEnd;
  if (newest && dataEnd && (Date.parse(newest.reportDate) - Date.parse(dataEnd)) / 86_400_000 > 300) {
    notes.push(`A newer annual report is on file with the SEC (${newest.form} for the year ending `
      + `${newest.reportDate}, filed ${newest.filed || "recently"}), but SEC's structured data does not `
      + `include its statements yet, so the financials here are for the year ending ${dataEnd}. `
      + `Upload the newer annual report as a PDF for the latest figures.`);
  }

  //: Interest expense from InterestIncomeExpenseNet, applied through the
  //  sign rule in interestExpenseFromRow(): a positive net value is net
  //  interest INCOME, not expense (Badger Meter FY2025, CIK 9092: +$5.124M
  //  net, no InterestExpense tag at all — the unguarded Math.abs() reported
  //  that income as a $5.124M interest cost). Only a negative net value
  //  (net expense) becomes a value here; a positive one is reported missing.
  const interestExpenseTag = flows.interest_expense?.tag;
  const interestExpenseValue = interestExpenseTag
    ? interestExpenseFromRow(latestFlow("interest_expense"), interestExpenseTag) : null;
  const interestSeries = flows.interest_expense?.series || [];
  const interestEnd = interestSeries.length ? interestSeries[interestSeries.length - 1].end : null;
  if (interestExpenseValue !== null && interestEnd && revLatestEnd && interestEnd < revLatestEnd) {
    notes.push(`Interest expense was last tagged for the year ended ${interestEnd}, before the `
      + `latest fiscal year (${revLatestEnd}); the free-cash-flow interest add-back carries that `
      + `figure forward for the later years.`);
  }
  if (interestExpenseValue === null && NET_INTEREST_TAGS.includes(interestExpenseTag)
      && latestFlow("interest_expense") != null) {
    notes.push("interest_expense is reported missing: the only available tag "
      + `(${interestExpenseTag}) reported net interest INCOME for the latest year, `
      + "not expense, and a net-income figure is not reinterpreted as an expense.");
  }

  //: us-gaap filers report under ASC 230, which requires interest paid to be
  //  classified within operating activities — that is a rule, not a
  //  per-filer disclosure, so it needs no XBRL lookup. IFRS (IAS 7) leaves
  //  the choice to the filer, so ifrs-full readers look for whichever
  //  classification tag was actually disclosed.
  let interestPaidClassification = taxonomy === "ifrs-full"
    ? ifrsInterestClassification(facts) : "operating";
  if (taxonomy === "ifrs-full" && interestPaidClassification === null) {
    const inferred = inferIfrsLeaseInterestClassification(facts, reportingCurrency);
    if (inferred) {
      interestPaidClassification = inferred;
      notes.push(`interest_paid_classification "${inferred}" is inferred from lease cash-flow `
        + `classification (CashOutflowForLeases vs PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities), `
        + `since no explicit InterestPaidClassifiedAs* disclosure was tagged.`);
    }
  }

  //: Field names mirror ExtractedFinancials exactly (see
  //  src/pipeline/pdf_extractor.py) so web_bridge.load_fundamentals() can
  //  rehydrate them without a second translation layer.
  const fields = {
    company_name: entityName,
    ticker: entry.symbol,
    fiscal_year: latestEnd ? Number(latestEnd.slice(0, 4)) : null,
    shares_as_of: sharesAsOf,
    revenue,
    free_cash_flows: freeCashFlows,
    net_income: netIncome,
    total_debt: totalDebt,
    cash_and_equivalents: cash === null && sti === null ? null : (cash || 0) + (sti || 0),
    //: On the SAME basis as the price beside it — see adsShares below.
    shares_outstanding: adsShares,
    //: On the filing's currency basis — converted from USD at the live rate
    //  when the two differ, missing when no rate was available.
    current_price: currentPrice,
    depreciation_amortization: latestFlow("depreciation_amortization"),
    rd_expense: latestFlow("rd_expense"),
    capital_expenditures: (() => {
      const v = latestFlow("capital_expenditures");
      return v == null ? null : Math.abs(v);
    })(),
    interest_expense: interestExpenseValue,
    interest_expense_series: interestExpenseSeries,
    sbc_series: sbcSeries,
    fcf_period_ends: fcfPeriodEnds,
    fcf_basis: fcfBasis,
    interest_paid_classification: interestPaidClassification,
    stock_based_compensation: (() => {
      const v = latestFlow("stock_based_compensation");
      return v == null ? null : Math.abs(v);
    })(),
    finance_lease_liabilities: financeLeaseValue,
    operating_lease_liabilities: operatingLeaseValue,
    //: The quote's OWN as-of time (Yahoo's regularMarketTime), not the
    //  request time — a quote can be a prior close, and nothing else here
    //  says so. Reported whenever a price was fetched, independent of the
    //  currency gate on current_price below, since it describes the quote
    //  itself rather than whether it was usable alongside these financials.
    price_as_of: priceInfo ? priceInfo.asOf : null,
    tax_rate: taxRate,
    revenue_growth: revenueGrowth,
    operating_margin: operatingMargin,
    //: Annual figure only — see the dividends_per_share comment in FLOW_TAGS
    //  for why taking the most recent row instead would understate it 4x.
    //  Split-adjusted above when a corporate action postdates the disclosure.
    dividend_per_share: dividendPerShare,
    //: Always true for anything this endpoint returns: pickAnnualSeries only
    //  admits ~365-day periods, so a quarterly declaration can't leak through
    //  and be re-annualised a second time downstream.
    dividend_is_annual: true,
    beta: betaInfo ? Number(betaInfo.beta.toFixed(3)) : null,
    //: Same aligned monthly-return pairs and 24-observation floor as beta
    //  above — see computeBeta(). These replace the fixed 25%/18%/0.6 guesses
    //  the options, Heston, VaR and MPT models otherwise fall back to.
    realized_volatility: betaInfo && betaInfo.stockVol !== null ? Number(betaInfo.stockVol.toFixed(4)) : null,
    market_volatility: betaInfo && betaInfo.marketVol !== null ? Number(betaInfo.marketVol.toFixed(4)) : null,
    market_correlation: betaInfo && betaInfo.correlation !== null ? Number(betaInfo.correlation.toFixed(4)) : null,
    return_observations: betaInfo ? betaInfo.observations : null,
    //: Same monthly-return series beta was computed from — see computeBeta().
    monthly_returns: betaInfo ? betaInfo.monthlyReturns : [],
    return_benchmark: betaInfo ? betaInfo.benchmarkSymbol : null,
    //: The taxonomy the facts above were actually read from (chosen earlier,
    //  us-gaap tried first, ifrs-full on a 20-F filer) — see the comment
    //  above `facts = (j.facts && j.facts["us-gaap"])`.
    accounting_standard: taxonomy,
    sic_code: filerInfo.sic,
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
    //: sic_code is classification metadata, not a financial figure the
    //  extraction-confidence count should penalise.
    .filter(([k, v]) => k !== "sic_code" && (v === null || (Array.isArray(v) && v.length === 0)))
    .map(([k]) => k);

  if (fields.current_price == null || fields.beta == null) {
    res.setHeader("Cache-Control", CACHE_SHORT);
  }
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
    //: `implied` is null for a stated ratio with no home market (SPOT, ARM).
    adr: adr ? { ratio: adr.ratio,
                 implied: typeof adr.implied === "number" ? Number(adr.implied.toFixed(4)) : null,
                 localSymbol: adr.localSymbol, stated: !!adr.stated } : null,
    sources: [
      { name: "SEC EDGAR XBRL companyfacts", url: `https://data.sec.gov/api/xbrl/companyfacts/CIK${entry.cik}.json` },
      ...(priceInfo ? [{ name: "Share price", url: "Yahoo Finance chart API" }] : []),
      ...(betaInfo ? [{ name: `Beta vs ${betaInfo.benchmark} (${betaInfo.observations} monthly returns)`,
                       url: "Computed by OLS from price history" }] : []),
      ...(betaInfo && (betaInfo.stockVol !== null || betaInfo.correlation !== null)
        ? [{ name: "Realised volatility & correlation (same monthly return series as beta)",
             url: "Computed from price history" }] : []),
    ],
  });
};

//: Pure helpers exported for scripts/test_fundamentals.js. They carry the
//  parts that are easy to get subtly wrong (share-class spelling, restatement
//  dedup, period alignment) and impossible to check by eyeballing a live
//  response, so they are tested directly rather than only through the handler.
module.exports._internals = { isTransient, resolveTicker, detectReportingCurrency, deriveAdrRatio,
                              pickBalanceSheet, valueAt, translationRates, pickCoverShares, mergeFacts, latestAnnualEnd, latestFilingForm, hasEverBorrowed, DEBT_COMPONENT_SLOTS, pickWeightedAverageShares,
                              PREDECESSOR_CIKS, DEBT_TAGS, IFRS_DEBT_TAGS, ADR_PINNED_RATIOS, ADR_PINNED_BAND,
                              MINOR_UNIT_QUOTES,
                              ADR_LOCAL_LISTINGS, ADR_RATIO_CANDIDATES, ADR_RATIO_TOLERANCE, pickInstant, pickAnnualSeries, FLOW_TAGS, STOCK_TAGS,
                              IFRS_FLOW_TAGS, IFRS_STOCK_TAGS, LEASE_TAGS,
                              benchmarkFor, monthlyReturns, monthKey, BENCHMARKS, MIN_BETA_OBSERVATIONS,
                              computeSplitAdjustment, SPLIT_ALLOTMENT_LAG_MS, annualizedVol, regressionStats,
                              pickLeaseLiability, debtTagIncludesLeases, NET_INTEREST_TAGS,
                              interestExpenseFromRow, seriesAlignedTo, ifrsInterestClassification,
                              dedupeMonthlyCloses, monthKeyToLabel, currentMonthKey, monthlyReturnPairs,
                              inferIfrsLeaseInterestClassification, LEASE_INTEREST_FINANCING_RATIO,
                              LEASE_INTEREST_OPERATING_RATIO };
