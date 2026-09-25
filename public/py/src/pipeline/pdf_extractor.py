"""PDF text extraction and financial-figure scraping.

Extracts text and tables from a financial PDF using a cascade of extractors,
then applies regex heuristics to pick out the numbers each valuation model
needs. Every backend is optional at import time — the extractor works with
whichever libraries are installed and reports which ones ran.

Cascade order (most feature-rich first):
    1. **PyMuPDF (fitz)** — fastest text + layout, best for well-formed reports
    2. **pdfplumber** — best table extraction
    3. **pypdf** — pure-Python fallback
    4. **pdfminer.six** — last-resort text extraction from stubborn PDFs

The scraper recognises common financial-statement formats: dollars in
millions/billions, parentheses for negatives, comma separators, "in $M" /
"($ millions)" headers, and multi-year columnar tables. Numbers are
normalised to a canonical dollar amount (not "millions").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..base_model import ModelError, ValidationError

# Optional backends — imported lazily so a missing package doesn't break the file.
_BACKENDS_AVAILABLE: dict[str, bool] = {}


def _try_import(name: str) -> Any:
    """Best-effort import of an optional PDF backend."""
    try:
        module = __import__(name)
        _BACKENDS_AVAILABLE[name] = True
        return module
    except Exception:  # pragma: no cover - environment-dependent
        _BACKENDS_AVAILABLE[name] = False
        return None


# --------------------------------------------------------------------------- #
# Data container
# --------------------------------------------------------------------------- #
@dataclass
class ExtractedFinancials:
    """Structured financial figures scraped from a PDF.

    All monetary amounts are in raw dollars (already scaled up from "millions"
    or "billions"). ``None`` means the figure was not confidently identified in
    the document.
    """

    company_name: str | None = None
    ticker: str | None = None
    fiscal_year: int | None = None
    revenue: float | None = None
    #: REPORTED free cash flow history, not a forecast. The DCF projects
    #: forward from the most recent entry (AutoAssumer.build); feeding this
    #: list to it directly as FCF_1..FCF_N discounted past years as if they
    #: were future ones and valued the terminal on a stale year.
    free_cash_flows: list[float] = field(default_factory=list)
    #: Which end of ``free_cash_flows`` is the most recent year.
    #: ``"newest_first"`` is how filings lay out their columns (a 10-K's
    #: "2025 2024 2023", a SEBI results table's current period first), so it
    #: is the default for PDF extraction; the SEC ticker path returns years in
    #: ascending order and marks itself ``"oldest_first"``.
    fcf_history_order: str = "newest_first"
    net_income: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    shares_outstanding: float | None = None
    current_price: float | None = None
    dividend_per_share: float | None = None
    beta: float | None = None
    revenue_growth: float | None = None
    operating_margin: float | None = None
    tax_rate: float | None = None
    #: Depreciation & amortisation (cash-flow-statement add-back) — used by
    #: the Ind AS 116 hidden-debt normalizer's owner-earnings recomputation.
    depreciation_amortization: float | None = None
    #: Research & development expense (income statement) — used as the cash
    #: R&D spend in the same owner-earnings recomputation.
    rd_expense: float | None = None
    #: Capital expenditure ("purchases of property and equipment" /
    #: "capex", cash-flow statement) — used as a maintenance-capex proxy.
    capital_expenditures: float | None = None
    #: Finance costs / interest expense (income statement) — used to derive
    #: a real cost of debt (this ÷ total_debt) for WACC instead of a flat
    #: risk-free+150bp spread, when both this and total_debt are known.
    interest_expense: float | None = None
    #: "Expected [share price] volatility" — a real, company-specific
    #: figure most 10-Ks disclose in the stock-based-compensation footnote
    #: (an ASC 718 Black-Scholes input for valuing employee option grants,
    #: not necessarily identical to a market-implied volatility for an
    #: arbitrary option, but real and period-specific — a genuine input to
    #: prefer over a flat 25% default for the option-pricing models). The
    #: `current_price` pattern above explicitly excludes this exact phrase
    #: to avoid a false price match; this field is what actually captures it.
    disclosed_volatility: float | None = None
    #: Market statistics computed from five years of monthly returns on the
    #: ticker path (api/fundamentals.js, the same regression that yields
    #: beta): the stock's realised annualised volatility, its benchmark
    #: index's, and their correlation. Measured, not disclosed — ``None``
    #: on the PDF path, and whenever too few returns were available.
    realized_volatility: float | None = None
    market_volatility: float | None = None
    market_correlation: float | None = None
    return_observations: int | None = None
    #: Lease liabilities (current + noncurrent) NOT already inside
    #: ``total_debt`` — the ticker path leaves a field None when the debt tag
    #: it used already includes it, so adding these never double-counts.
    #: IFRS 16 / Ind AS 116 filers report one lease liability, mapped to the
    #: finance field (every IFRS 16 lease is on-balance-sheet financing).
    finance_lease_liabilities: float | None = None
    operating_lease_liabilities: float | None = None
    #: Stock-based compensation expense for the latest fiscal year (a
    #: non-cash add-back in operating cash flow, treated as a real cost when
    #: normalising FCF — see AutoAssumer._normalised_base).
    stock_based_compensation: float | None = None
    #: Per-year series aligned one-to-one with ``free_cash_flows`` (same
    #: order, ``None`` for a year the filing didn't tag), plus the period
    #: ends they refer to. Ticker path only.
    interest_expense_series: list[float | None] = field(default_factory=list)
    sbc_series: list[float | None] = field(default_factory=list)
    fcf_period_ends: list[str] = field(default_factory=list)
    #: How ``free_cash_flows`` was built on the ticker path: ``"reported"``
    #: (operating cash flow − capex) or ``"ocf_minus_da"`` (no capex line
    #: tagged, so D&A stands in for maintenance capex). ``None`` for PDFs.
    fcf_basis: str | None = None
    #: Where the cash-flow statement puts interest paid: ``"operating"``
    #: (always under US GAAP), ``"financing"`` (allowed under IFRS / Ind AS),
    #: or ``None`` when unknown. Decides whether FCF gets interest added back.
    interest_paid_classification: str | None = None
    #: When the market price was struck (ISO 8601 UTC), when known.
    price_as_of: str | None = None
    #: The stock's monthly TOTAL returns (dividend-adjusted closes) for
    #: completed calendar months, ascending, as ``[YYYYMM, return]`` pairs —
    #: the same series beta is regressed on, exposed so Fama-French can run
    #: on the company's real returns. Ticker path only.
    monthly_returns: list[list[float]] = field(default_factory=list)
    #: Symbol of the index those returns were measured against (``^GSPC``
    #: for US listings). Fama-French's factors are US-market factors, so
    #: this decides whether a real regression is meaningful at all.
    return_benchmark: str | None = None
    #: ``"us-gaap"`` or ``"ifrs-full"`` — the XBRL taxonomy the filing
    #: reports in (ticker path). ``None`` for a PDF.
    accounting_standard: str | None = None
    #: ISO 4217 code the filing's own figures are denominated in (detected
    #: from currency symbols/codes in the document text — see
    #: :meth:`PDFExtractor._detect_currency`). Every monetary field above is
    #: in raw units of THIS currency, not necessarily USD — a DCF/RDCF
    #: headline built from an undetected non-USD filing and labelled "$"
    #: would misrepresent the actual scale by whatever the real FX rate is.
    #: Defaults to "USD" (this pipeline's original, implicit assumption)
    #: when the document gives no more specific signal.
    currency: str = "USD"
    #: True when ``dividend_per_share`` is a whole-year declaration rather
    #: than one period's payment — an Indian board resolution recommends a
    #: "Final Dividend of Rs.4/- per equity share for the financial year
    #: ended March 31, 2026", which is the ANNUAL dividend, stated inside a
    #: quarterly results filing. The quarterly-to-annual scaling would
    #: otherwise multiply it by four and report a ₹16 dividend the company
    #: never declared — a wrong number that looks more credible than the old
    #: placeholder precisely because it is built from a real extracted one.
    dividend_is_annual: bool = False
    #: Which reporting basis every *figure* above was read from:
    #: ``"consolidated"``, ``"standalone"``, or ``"unsegmented"`` when the
    #: document carries no statement-section headers to choose between (a
    #: US 10-K, a press release on its own). A filing that publishes both
    #: bases states each figure twice, with genuinely different values —
    #: recording which one was used is what makes the numbers checkable
    #: against the source instead of merely plausible.
    statement_basis: str = "unsegmented"
    #: Trailing-twelve-month values for the income-statement flow rows a
    #: quarterly filing reports, keyed by the field name they correspond to
    #: above. Populated only when the filing's own column headers *prove*
    #: the arithmetic is valid — see :meth:`PDFExtractor.scrape_ttm_flows`.
    #: Empty for an annual filing, and for any quarterly one where the proof
    #: fails. Consumers use it in place of a x4 run-rate; see
    #: ``web_bridge._annualise_quarterly``.
    ttm_flows: dict[str, float] = field(default_factory=dict)
    #: One of the ~31 industry categories :meth:`PDFExtractor._classify_sector`
    #: recognises (matching Damodaran's own NYU Stern taxonomy — see
    #: :data:`src.pipeline.assumptions.SECTOR_BASELINES`), or ``None`` when
    #: the filing's own text does not clear the confidence bar that method
    #: requires. Used only to pick a better FALLBACK than a flat generic
    #: default (beta, margin, growth) when the filing itself discloses
    #: nothing more specific — never overrides a real extracted or
    #: filing-stated value.
    sector: str | None = None
    #: The registrant's SEC Standard Industrial Classification code (EDGAR
    #: submissions ``sic``), supplied by the ticker path only. Used to spot
    #: commodity producers — see ``_COMMODITY_SIC_RANGES`` in assumptions.py.
    sic_code: int | None = None
    #: Revenue for each year of ``free_cash_flows``, element for element
    #: (ticker path only). Lets the FCF base be a normalised margin applied to
    #: the latest revenue — see ``AutoAssumer._normalised_base``.
    revenue_series: list[float | None] = field(default_factory=list)
    #: The part of ``total_debt`` owed by a captive finance arm (GM Financial,
    #: Ford Credit), from the balance sheet's segment columns. ``None`` when
    #: the company has no finance arm or the split is not disclosed.
    finance_arm_debt: float | None = None
    #: Free cash flow to EQUITY per year, aligned with ``free_cash_flows``
    #: (captive-finance manufacturers only; see api/fundamentals.js
    #: captiveEquityCashFlows). When present the DCF values equity directly.
    fcfe_series: list[float | None] = field(default_factory=list)
    #: A telecom's average annual spectrum/licence purchases (ticker path),
    #: charged against free cash flow like capex.
    spectrum_charge: float | None = None
    #: The quote's instrument type (EQUITY, ETF, CRYPTOCURRENCY...), ticker
    #: path only. The company models refuse anything but an EQUITY.
    instrument_type: str | None = None
    #: Which backends actually produced text (for debugging in the UI).
    backends_used: list[str] = field(default_factory=list)
    #: Raw text (first ~50k chars) kept for downstream inspection.
    raw_text: str = ""

    @property
    def net_debt(self) -> float | None:
        """Net debt = total debt − cash & equivalents (when both are present)."""
        if self.total_debt is None or self.cash_and_equivalents is None:
            return self.total_debt
        return self.total_debt - self.cash_and_equivalents

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of every scraped field."""
        return {
            "company_name": self.company_name, "ticker": self.ticker,
            "fiscal_year": self.fiscal_year, "revenue": self.revenue,
            "free_cash_flows": self.free_cash_flows,
            "fcf_history_order": self.fcf_history_order,
            "net_income": self.net_income, "total_debt": self.total_debt,
            "cash_and_equivalents": self.cash_and_equivalents,
            "net_debt": self.net_debt,
            "shares_outstanding": self.shares_outstanding,
            "current_price": self.current_price,
            "dividend_per_share": self.dividend_per_share, "beta": self.beta,
            "revenue_growth": self.revenue_growth,
            "operating_margin": self.operating_margin, "tax_rate": self.tax_rate,
            "depreciation_amortization": self.depreciation_amortization,
            "rd_expense": self.rd_expense,
            "capital_expenditures": self.capital_expenditures,
            "interest_expense": self.interest_expense,
            "disclosed_volatility": self.disclosed_volatility,
            "realized_volatility": self.realized_volatility,
            "market_volatility": self.market_volatility,
            "market_correlation": self.market_correlation,
            "return_observations": self.return_observations,
            "finance_lease_liabilities": self.finance_lease_liabilities,
            "operating_lease_liabilities": self.operating_lease_liabilities,
            "stock_based_compensation": self.stock_based_compensation,
            "interest_expense_series": self.interest_expense_series,
            "sbc_series": self.sbc_series,
            "fcf_period_ends": self.fcf_period_ends,
            "fcf_basis": self.fcf_basis,
            "interest_paid_classification": self.interest_paid_classification,
            "price_as_of": self.price_as_of,
            "monthly_returns": self.monthly_returns,
            "return_benchmark": self.return_benchmark,
            "accounting_standard": self.accounting_standard,
            "currency": self.currency,
            "dividend_is_annual": self.dividend_is_annual,
            "statement_basis": self.statement_basis,
            "ttm_flows": self.ttm_flows,
            "sector": self.sector,
            "sic_code": self.sic_code,
            "revenue_series": self.revenue_series,
            "finance_arm_debt": self.finance_arm_debt,
            "fcfe_series": self.fcfe_series,
            "spectrum_charge": self.spectrum_charge,
            "instrument_type": self.instrument_type,
            "backends_used": self.backends_used,
        }


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #
class PDFExtractor:
    """Extract financial figures from a company financial PDF.

    Example:
        >>> extractor = PDFExtractor()
        >>> data = extractor.extract("path/to/10-K.pdf")   # doctest: +SKIP
        >>> data.revenue                                    # doctest: +SKIP
    """

    #: Multipliers for "in millions" / "in thousands" / "in billions" headers,
    #: plus the Indian numbering system (1 lakh = 1e5, 1 crore = 1e7) used by
    #: every BSE/NSE-listed company's filings — absent here, a real Indian
    #: filing's "Amount in (Rs.) in lakhs" figures were silently treated as
    #: already being in whole rupees, understating every value 100,000x.
    SCALE_HINTS = {
        "billion": 1e9, "bn": 1e9, "bil": 1e9,
        "crore": 1e7, "crores": 1e7, "cr": 1e7,
        "million": 1e6, "mn": 1e6, "mil": 1e6, "mm": 1e6,
        "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
        "thousand": 1e3, "k": 1e3,
    }

    #: Display symbol per ISO 4217 code — used wherever a headline number
    #: gets a currency prefix instead of an unconditional "$" (see
    #: :class:`src.pipeline.runner.AnalysisReport._headline`).
    CURRENCY_SYMBOLS = {
        "USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥",
        "CNY": "¥", "CAD": "C$", "AUD": "A$", "HKD": "HK$", "SGD": "S$",
        "CHF": "CHF ", "KRW": "₩",
    }

    #: A bare currency symbol/code anywhere in running text is not a
    #: reliable signal on its own — a real BLS International filing
    #: contains "BLS £-Services Limited" (a subsidiary's name, not a GBP
    #: figure) and separately mentions a UK-based acquisition entirely
    #: unrelated to what currency the CONSOLIDATED statement itself is
    #: denominated in. Every pattern here requires the symbol/code to sit
    #: immediately next to an actual number, which a company name or an
    #: incidental prose mention never does.
    _CURRENCY_SIGNALS: tuple[tuple[str, str], ...] = (
        (r"₹\s?\d", "INR"),
        (r"€\s?\d", "EUR"),
        (r"£\s?\d", "GBP"),
        (r"¥\s?\d", "JPY"),
        (r"\bINR\s?\d|\d\s?INR\b", "INR"),
        (r"\bEUR\s?\d|\d\s?EUR\b", "EUR"),
        (r"\bGBP\s?\d|\d\s?GBP\b", "GBP"),
        (r"\bRs\.?\s*\d", "INR"),
        (r"\$\s?\d", "USD"),
    )

    @classmethod
    def _detect_currency(cls, text: str) -> str:
        """Best-effort ISO 4217 code for the currency this filing reports in.

        "lakh"/"crore" (:attr:`SCALE_HINTS`) checked first — that numbering
        system is used exclusively for Indian Rupee reporting, so its mere
        presence is a stronger, already-battle-tested signal than trying to
        separately re-detect INR from a currency symbol. Falls through to
        symbol/code detection (each require digit-adjacency — see
        :attr:`_CURRENCY_SIGNALS`), then defaults to "USD" (this pipeline's
        original, implicit assumption) when nothing more specific is found,
        so a filing that gives no signal at all behaves exactly as before
        this method existed.
        """
        if re.search(r"\blakh|\blacs?\b|\bcrores?\b|\bcr\.\b", text, re.IGNORECASE):
            return "INR"
        for pattern, code in cls._CURRENCY_SIGNALS:
            if re.search(pattern, text):
                return code
        return "USD"

    def __init__(self, max_pages: int | None = None) -> None:
        """Configure extractor.

        Args:
            max_pages: Cap on pages to read (``None`` = no cap).
        """
        self.max_pages = max_pages

    # ------------------------------------------------------------------ #
    # Text/table cascade
    # ------------------------------------------------------------------ #
    def extract_text(self, source: str | Path | bytes) -> tuple[str, list[str]]:
        """Extract raw text using the first backend that succeeds.

        Args:
            source: Path to the PDF file, or its bytes.

        Returns:
            Tuple ``(text, backends_used)`` — a single concatenated text blob
            and the list of backends whose output was included.

        Raises:
            ModelError: If every backend fails to produce any text.
        """
        text_parts: list[str] = []
        used: list[str] = []
        # Backends in cascade order — try each; keep the richest output.
        for backend in ("fitz", "pdfplumber", "pypdf", "pdfminer"):
            try:
                fragment = self._run_backend(backend, source)
            except Exception:  # pragma: no cover - backend-specific failures
                continue
            if fragment and len(fragment.strip()) > 100:
                text_parts.append(fragment)
                used.append(backend)
                # Once we have a rich extraction, no need to try weaker backends.
                if backend in ("fitz", "pdfplumber"):
                    break
        if not text_parts:
            raise ModelError(
                "No PDF backend could extract text. Install at least one of: "
                "pymupdf, pdfplumber, pypdf, pdfminer.six."
            )
        return "\n".join(text_parts), used

    def _run_backend(self, name: str, source: str | Path | bytes) -> str:
        """Dispatch to a specific extraction backend by name."""
        if name == "fitz":
            fitz = _try_import("fitz")
            if fitz is None:
                return ""
            doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) \
                else fitz.open(source)
            pages = list(doc)[: self.max_pages] if self.max_pages else list(doc)
            return "\n".join(p.get_text() for p in pages)
        if name == "pdfplumber":
            pdfplumber = _try_import("pdfplumber")
            if pdfplumber is None:
                return ""
            import io
            src = io.BytesIO(source) if isinstance(source, bytes) else source
            with pdfplumber.open(src) as pdf:
                pages = pdf.pages[: self.max_pages] if self.max_pages else pdf.pages
                return "\n".join((p.extract_text() or "") for p in pages)
        if name == "pypdf":
            pypdf = _try_import("pypdf")
            if pypdf is None:
                return ""
            import io
            reader = pypdf.PdfReader(io.BytesIO(source) if isinstance(source, bytes) else source)
            pages = reader.pages[: self.max_pages] if self.max_pages else reader.pages
            return "\n".join(p.extract_text() or "" for p in pages)
        if name == "pdfminer":
            pdfminer = _try_import("pdfminer.high_level")
            if pdfminer is None:
                return ""
            import io
            src = io.BytesIO(source) if isinstance(source, bytes) else Path(source).read_bytes()
            src = io.BytesIO(src) if isinstance(src, bytes) else src
            return pdfminer.extract_text(src) or ""
        return ""

    # ------------------------------------------------------------------ #
    # Number scraping
    # ------------------------------------------------------------------ #
    _NUMBER_RE = re.compile(
        r"""
        (?<![\w.])
        \$?\s*                              # optional $
        \(?                                 # optional opening ( for negatives
        # Integer part, three groupings in priority order:
        #   1. Western  "1,234,567"   — 3-digit groups throughout.
        #   2. Indian   "2,99,821"    — the last group is 3 digits, every
        #      group before it is 2 (1,00,000 is one lakh). EVERY BSE/NSE
        #      filing writes its large figures this way, and without this
        #      branch the western pattern fails to match past the first
        #      group and the bare \d+ fallback then reads "2,99,821.51" as
        #      the number 2 — a real 150,000x error found on a live BLS
        #      full-year revenue column, silent because 2.0 is a perfectly
        #      parseable number.
        #   3. Bare     "1234567"
        (\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)
        (?:\.(\d+))?                        # optional decimal
        \)?                                 # optional closing )
        \s*(million|billion|thousand|bn|mn|mm|bil|mil|k)?
        (?!\w|\.\d)
        """,
        # The trailing guard excludes a following word character (so "1990s"
        # or "3rd" don't get read as bare numbers) and a period that's
        # itself followed by a digit (a truncated decimal point). It must
        # NOT exclude a bare trailing period with no digit after it — a
        # sentence simply ending right after the number (e.g. "...752.") is
        # extremely common, and excluding it forces the engine to backtrack
        # off the number's last comma-group to find a position where the
        # lookahead is satisfied, silently truncating large real figures
        # (a real bug found via a 3,210,875,752-style share count regressing
        # to 3,210,875 when it happened to end a sentence).
        re.IGNORECASE | re.VERBOSE,
    )

    #: A number immediately followed by ", <year>" (as in "...on June 30,
    #: 2025)...") is a date fragment, not a financial figure — narrative text
    #: around a keyword match routinely contains a nearby date, and without
    #: this guard it gets picked up as if it were the actual figure.
    _DATE_TAIL_RE = re.compile(r"^\s*,\s*(?:19|20)\d{2}\b")

    #: A short "(1+II)" / "(V-VI)" span is a line-formula cross-reference —
    #: Indian filings routinely label a subtotal row "Total income (I+II)"
    #: or "Profit before tax (V-VI)" right before the real figure, and OCR
    #: sometimes renders the roman numerals as bare digits ("(1+11)"). Either
    #: number inside that parenthetical could otherwise outscore the actual
    #: multi-digit amount a few characters later, since it's the first (or
    #: second) numeric-looking token the window regex finds. Matched and
    #: blanked out (same length, so surrounding offsets are unaffected)
    #: before candidate numbers are scanned — see :meth:`_parse_number`.
    _ARITH_REF_RE = re.compile(r"\(\s*[IVXivx\d]{1,4}\s*[+\-]\s*[IVXivx\d]{1,4}\s*\)")

    #: OCR sometimes drops the thousands-separator comma and leaves a bare
    #: space in its place — a real scan of "20,162.22" came back "20 162.22"
    #: (all four figures on that specific row lost their commas the same
    #: way). Left alone, the base number regex reads "20" as a complete,
    #: plausible-looking value and stops there, silently landing on a number
    #: 1,000x too small once scale ("in lakhs") is applied. Normalise a
    #: single-space-separated "<1-3 digits> <exactly 3 digits>" run back into
    #: a comma before scanning for candidates, so the existing comma-grouped
    #: branch of :attr:`_NUMBER_RE` picks it up correctly. Deliberately a
    #: literal space only (not ``\s``, which also matches newlines) and
    #: scoped to the narrow keyword-adjacent window :meth:`_parse_number` is
    #: always called with — applying this document-wide would risk merging
    #: two genuinely unrelated numbers that just happen to sit a single
    #: space apart in running prose.
    _BROKEN_THOUSANDS_SEP_RE = re.compile(r"(?<=\d) (\d{3})(?!\d)")

    @classmethod
    def _parse_number(cls, match_text: str) -> float | None:
        """:meth:`_parse_number_scaled` without the inline-unit flag."""
        value, _ = cls._parse_number_scaled(match_text)
        return value

    @classmethod
    def _parse_number_scaled(cls, match_text: str) -> tuple[float | None, bool]:
        """Parse the first *plausible* numeric token, honouring $, (), commas and scale suffix.

        Returns ``(value, inline_unit_applied)``. The second element says
        whether the token carried its own scale suffix ("$3.2 billion") that
        has already been multiplied in, and it is the *only* correct signal
        for whether a surrounding "(In millions)" table header should be
        applied on top — applying both double-scales, applying neither
        under-scales by the header's full factor.

        That question used to be answered by a magnitude test (``abs(value)
        < 1e5`` ⇒ assume no unit was applied). The proxy holds for small
        figures and fails completely for large ones: an Apple-scale
        ``"Total revenue 391,035"`` under ``(In millions)`` sailed past the
        test and was reported as 391 *thousand* dollars rather than $391
        billion — a 1,000,000x understatement — while the same table's
        ``97,690`` scaled correctly, so the bug was invisible in aggregate
        and appeared only on the largest filers.

        The "wrapped in parentheses means negative" accounting convention is
        checked against the matched token itself (``m.group(0)``, which the
        regex's own ``\\(?...\\)?`` already anchors tightly around the digits)
        — never against the wider ``match_text`` window it was found in. A
        window can legitimately contain unrelated parenthesised text before
        or after the number (a footnote reference, an adjacent unrelated
        line in a comparison table, a qualifying phrase like "(before
        consolidation adjustment)") that has nothing to do with the sign of
        *this* number; checking the whole window there flips figures that
        were never actually negative.

        Every candidate token in the window is considered in order (not just
        the first): a token immediately followed by ", <year>" is a date
        fragment (e.g. the "30" in "reported ... on June 30, 2025") and is
        skipped in favour of the next number in the window, if any. A
        "(1+II)"-style line-formula cross-reference is blanked out entirely
        before candidates are scanned — see :attr:`_ARITH_REF_RE`. A broken
        "20 162.22" thousands separator is normalised back to "20,162.22" —
        see :attr:`_BROKEN_THOUSANDS_SEP_RE`.
        """
        match_text = cls._ARITH_REF_RE.sub(lambda m: " " * len(m.group(0)), match_text)
        match_text = cls._BROKEN_THOUSANDS_SEP_RE.sub(r",\1", match_text)
        for m in cls._NUMBER_RE.finditer(match_text):
            if cls._DATE_TAIL_RE.match(match_text[m.end():]):
                continue
            raw, dec, unit = m.groups()
            try:
                value = float(raw.replace(",", "") + ("." + dec if dec else ""))
            except ValueError:
                continue
            token = m.group(0)
            if "(" in token and ")" in token:
                value = -value
            if unit:
                value *= cls.SCALE_HINTS.get(unit.lower(), 1.0)
            return value, bool(unit)
        return None, False

    #: Boilerplate SEBI/ICAI auditor-report language that scopes a nearby
    #: figure to a *subset* of the company (unreviewed subsidiaries, a
    #: before-consolidation cut) rather than its consolidated total — e.g.
    #: a real BLS International filing's "Other Matters" section reads
    #: "...whose interim financial information reflects total revenues
    #: (before consolidation adjustment) of Rs. 38,753.24 lakhs...", which a
    #: bare "total revenue" keyword match previously picked up in place of
    #: the real consolidated figure sitting earlier in the document under
    #: "Income from operations" — both numbers are plausible-looking, so
    #: this was silently wrong, not obviously broken. Reusable across any
    #: field where a footnote-scoped figure could plausibly precede the
    #: real consolidated one in document order.
    _FOOTNOTE_SCOPE_DISQUALIFIERS = (
        r"before\s+consolidation\s+adjustment",
        r"whose\s+(?:interim\s+)?financial\s+(?:information|statements|results)",
        r"\bsubsidiar",
    )

    @classmethod
    def _first_after(
        cls, text: str, patterns: list[str], window: int = 120,
        apply_scale: bool = False,
        scale_below: float | None = None,
        plausible: Callable[[float], bool] | None = None,
        disqualify: tuple[str, ...] | None = None,
    ) -> float | None:
        """Return the first *plausible* number appearing after any of ``patterns``.

        Args:
            text: Full document text to search.
            patterns: Regexes tried in order; every match of every pattern is
                considered (not just the first pattern's first match) before
                giving up — a keyword can legitimately appear many times in a
                long filing (footnotes, narrative prose, comparison tables)
                before its primary-statement occurrence.
            window: How many characters after the keyword to search for a number.
            apply_scale: When set, multiply the value by the scale ("in
                millions"/"in lakhs"/etc.) detected near *this* match (see
                :meth:`_local_scale`) rather than leaving it in raw document
                units — unless the token already carried its own inline unit
                suffix, which :meth:`_parse_number_scaled` reports and which
                would otherwise be counted twice. Set it for monetary fields
                only: a scale header declares a *currency* unit ("₹ in
                lakhs"), so it says nothing about a share count.
            scale_below: Additionally require the raw value to be under this
                threshold before scaling it. Only meaningful for a
                non-currency field where a real domain floor exists — see
                :meth:`_scrape_share_count`, the sole caller. Leave unset
                for money: there is no lower bound on what a company can
                legitimately report, so a magnitude test there silently
                drops the multiplier off the largest figures in the filing.
            plausible: Optional predicate a candidate value must satisfy to be
                accepted; an implausible candidate (e.g. a percentage that's
                actually a bare calendar year, a share count in the tens) is
                skipped in favour of the next occurrence instead of being
                returned as-is.
            disqualify: Optional regexes; a match whose own trailing window
                (the same text searched for the number) contains any of these
                is skipped in favour of the next occurrence — a keyword
                "hit" that's actually scoped to a footnote/subset rather than
                the real consolidated figure. See
                :attr:`_FOOTNOTE_SCOPE_DISQUALIFIERS` for the common case.
        """
        for pat in patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                trailing = text[match.end() : match.end() + window]
                if disqualify and any(re.search(dq, trailing, re.IGNORECASE) for dq in disqualify):
                    continue
                value, inline_unit = cls._parse_number_scaled(trailing)
                if value is None:
                    continue
                if (apply_scale and not inline_unit
                        and (scale_below is None or abs(value) < scale_below)):
                    value *= cls._local_scale(text, match.start())
                if plausible is not None and not plausible(value):
                    continue
                return value
        return None

    @classmethod
    def _first_after_widening(
        cls, section: str, full: str, patterns: list[str], **kwargs: Any,
    ) -> float | None:
        """:meth:`_first_after` on the statement section, then the whole document.

        Used for *ratios* only — growth, margin, effective tax rate — and
        deliberately not for absolute figures. The difference is that the two
        reporting bases state genuinely different absolute numbers (Caplin
        Point's quarterly revenue is ₹202.95 Cr standalone and ₹643.9 Cr
        consolidated), so reading one field from each is a real error; a
        margin or a growth rate is the same order of thing either way.

        Ratios also routinely live *outside* any statement section: a filing
        narrates "Revenue growth 12%" in its press release or covering
        letter, pages before the statement it belongs to. Confining them to
        the section would discard a figure the filing states outright in
        favour of one derived, or worse, of a generic default.
        """
        found = cls._first_after(section, patterns, **kwargs)
        if found is not None or section is full:
            return found
        return cls._first_after(full, patterns, **kwargs)

    @classmethod
    def _guess_scale_for_text(cls, snippet: str) -> float:
        """Detect an "in $ millions" / "in lakhs" / etc. header within ``snippet``."""
        header = snippet.lower()
        if re.search(r"in\s+\$?\s*billion", header) or "in $bn" in header:
            return 1e9
        if re.search(r"\bcrores?\b", header):
            return 1e7
        if re.search(r"in\s+\$?\s*million", header) or "in $mm" in header or "in $m" in header:
            return 1e6
        if re.search(r"\blakh|\blacs?\b", header):
            return 1e5
        if re.search(r"in\s+\$?\s*thousand", header):
            return 1e3
        return 1.0

    @classmethod
    def _guess_scale_for_statement(cls, text: str) -> float:
        """Document-level scale guess from its opening lines (fallback only).

        Kept for backward compatibility with any caller wanting a single
        whole-document guess; :meth:`_local_scale` is what :meth:`_first_after`
        actually uses now, since a real filing's financial-statement tables
        (and the scale header for the specific figure being read) routinely
        sit far past the first few thousand characters — see :meth:`_local_scale`.
        """
        return cls._guess_scale_for_text(text[:6000])

    @classmethod
    def _local_scale(cls, text: str, pos: int, lookback: int = 3000) -> float:
        """Scale multiplier for a figure found at ``pos`` in ``text``.

        Prefers a scale header that appears close to (before) this specific
        match over one guessed once from the document's opening lines. A
        single document-wide guess anchored to the first ~6000 characters
        misses the real header entirely on a long filing — a full SEC 10-K
        routinely runs 150+ pages of legal boilerplate (forward-looking
        statements, risk factors, business description) before Item 7's
        "(Dollars in millions)" tables appear, so the actual balance-sheet
        figures were being read as if no scale applied at all. Falls back to
        the document-level guess when no local header is found (preserves
        behaviour for short documents where the header is near the top).
        """
        window = text[max(0, pos - lookback):pos]
        local = cls._guess_scale_for_text(window)
        if local != 1.0:
            return local
        return cls._guess_scale_for_text(text[:6000])

    #: The header every SEBI-format results statement opens with, captured
    #: per basis — "STATEMENT OF UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR
    #: THE QUARTER ENDED ...". Deliberately requires the "statement of" +
    #: "financial results" frame rather than the bare word "consolidated":
    #: the word alone appears dozens of times in narrative notes, auditor
    #: language and segment tables, none of which begin a statement.
    _STATEMENT_SECTION_RE = re.compile(
        r"statement\s+of\s+(?:the\s+)?(?:un[\s-]?audited|audited|reviewed)?\s*"
        r"(consolidated|standalone|stand[\s-]alone)\s+financial\s+results",
        re.IGNORECASE,
    )

    @classmethod
    def _select_statement_section(cls, text: str) -> tuple[str, str]:
        """Narrow extraction to a single reporting basis, preferring consolidated.

        This is the fix for the failure mode that made a real Caplin Point
        filing produce an internally contradictory report: every field was
        resolved by scanning the *whole* document for that field's own
        keyword, so which basis a figure came from was decided by nothing
        more principled than where its label happened to fall in the file.

        That is not a stable rule, because document order is not stable. Two
        real filings order the two statements oppositely:

        * BLS International publishes CONSOLIDATED first, STANDALONE second —
          so first-match-wins happened to read consolidated throughout, and
          looked correct.
        * Caplin Point publishes STANDALONE first, CONSOLIDATED second — so
          the same logic read revenue from the consolidated press-release
          table (₹643.9 Cr) but net income (₹120.38 Cr) and D&A (₹6.56 Cr)
          from the standalone statement, blending two different companies'
          worth of figures into one statement. Every ratio computed from a
          mixed pair (margin, D&A/revenue, cost of debt) is then meaningless
          while still looking entirely reasonable.

        Consolidated is preferred because it is the basis that describes the
        whole economic entity, which is what a valuation is of; standalone
        excludes subsidiaries, and for a group like Caplin Point (whose
        LatAm operating subsidiaries are most of the business) the two differ
        by roughly 3x.

        Returns:
            ``(section_text, basis)`` — the slice figures should be read
            from, and which basis it is. A document with no statement
            headers at all (a US 10-K, a standalone press release) returns
            the full text unchanged and ``"unsegmented"``, so nothing about
            those formats changes.
        """
        marks = [(m.start(), m.group(1).lower().replace(" ", "").replace("-", ""))
                 for m in cls._STATEMENT_SECTION_RE.finditer(text)]
        if not marks:
            return text, "unsegmented"

        preferred = next((pos for pos, basis in marks if basis == "consolidated"), None)
        basis = "consolidated"
        if preferred is None:
            preferred = marks[0][0]
            basis = "standalone"

        # The section runs until the next statement header of the *other*
        # basis — an auditor's review report on the other statement counts,
        # since it quotes that statement's figures and would reintroduce
        # exactly the cross-basis bleed this is preventing.
        end = next((pos for pos, other in marks
                    if pos > preferred and other != basis), len(text))
        return text[preferred:end], basis

    #: Free cash flow as a results announcement states it in prose — "Free
    #: Cash Flow is ₹40 Crores (after Capex investment of ₹55 Crores)". Bound
    #: tightly to the keyword's own clause: it must be the number directly
    #: after "is"/"of"/"at"/"stood at", on the same line, and the pattern
    #: stops before the parenthetical so the capex figure inside it can never
    #: be mistaken for the cash flow.
    #: The currency prefix is optional AND spelled several ways in the same
    #: corpus — "₹40 Crores" in one section of a filing and "Rs 40 Crores" or
    #: "Rs.40" in another. Accepting only the ₹ glyph made this pattern miss
    #: the "Rs" spelling entirely and fall through to the table-row scan,
    #: which then read the FCF and the capex in the following parenthetical
    #: as two consecutive years of cash flow.
    #: Handles the three ways a filing writes a NEGATIVE free cash flow — a
    #: leading minus, the accounting parenthesis, and the word "negative"
    #: before the keyword. A cash-burning quarter is real, disclosed data:
    #: dropping it (as this pattern originally did, by requiring a leading
    #: digit) meant the assumer synthesised a POSITIVE free cash flow in its
    #: place, inventing a number that contradicts what the filing states.
    _FCF_PROSE_RE = re.compile(
        r"(negative\s+)?free\s+cash\s+flow[s]?\s*"
        r"(?:is|of|at|was|stood\s+at|stands\s+at)\s*"
        r"(?:[₹$€£]|\bRs\.?|\bINR)?\s*"
        r"(\(|-|–|−)?\s*(?:[₹$€£]|\bRs\.?|\bINR)?\s*"
        r"(\d[\d,]*(?:\.\d+)?)\)?\s*"
        r"(crores?|cr\b|lakhs?|million|mn|billion|bn)?",
        re.IGNORECASE,
    )

    #: A parenthetical that explains what was deducted to reach the figure —
    #: "(after Capex investment of ₹55 Crores)". Its number qualifies the
    #: cash flow; it is not another period's cash flow, and the table-row
    #: scan below has no other way to tell the difference.
    _CAPEX_ASIDE_RE = re.compile(
        r"\([^)]*\b(?:capex|capital\s+expenditure)[^)]*\)", re.IGNORECASE)

    @classmethod
    def _scrape_fcf_series(cls, text: str) -> list[float]:
        """Find free cash flow — the prose statement first, then a table row.

        The prose form is tried first because it is unambiguous: it names one
        figure, for one period, in the same clause as the keyword. The
        table-row scan below cannot make that guarantee, and on a real Caplin
        Point filing it demonstrated exactly how badly that fails — its
        ``[^\\n]*`` skipped over the real "₹40 Crores" sitting on the keyword's
        own line, then harvested the *following* line, returning
        ``[65, 78, 22]`` crores. Only the 65 was even a currency figure (a
        capex number); 78 and 22 were the two halves of a geographic revenue
        split, "in the range of 78% and 22% respectively". Those three values
        were then passed to the DCF as its free-cash-flow trajectory.

        Returns a single-element list when the document states one figure for
        one period — callers must treat that as a *base* to project from, not
        as a complete multi-year series (see AutoAssumer._synth_fcfs).
        """
        prose = cls._FCF_PROSE_RE.search(text)
        if prose:
            negated, sign, digits, unit = prose.groups()
            value = float(digits.replace(",", ""))
            if negated or sign:
                value = -value
            unit = (unit or "").lower().rstrip(".")
            if unit:
                value *= cls.SCALE_HINTS.get(unit, 1.0)
            else:
                # No magnitude test: the if/elif already guarantees the
                # header scale is applied only when the prose stated no
                # unit of its own — see _parse_number_scaled.
                value *= cls._local_scale(text, prose.start())
            if value != 0:
                return [value]

        # Table-row form. The window now starts at the END OF THE KEYWORD,
        # not at the end of its line: a table row states its values on the
        # label's own line ("Free Cash Flow  40  53  65"), and the previous
        # `[^\n]*` skipped exactly those before reading anything. Kept at 200
        # characters, which still spans the label-on-one-line /
        # values-on-the-next layout that PDF text extraction often produces.
        rows = re.finditer(
            r"free\s+cash\s+flow[s]?|(?<![a-z])fcf(?![a-z])"
            r"|cash\s+flow\s+from\s+operations\s*-\s*capex",
            text, re.IGNORECASE,
        )
        for row in rows:
            tail = text[row.end(): row.end() + 200]
            # Blank out capex asides (same length, so no offset shifts) before
            # scanning — see _CAPEX_ASIDE_RE.
            tail = cls._CAPEX_ASIDE_RE.sub(lambda m: " " * len(m.group(0)), tail)
            nums: list[float] = []
            for m in cls._NUMBER_RE.finditer(tail):
                # A percentage is never a cash flow. This is the guard that
                # was missing when a "78% and 22%" revenue split was read as
                # two years of free cash flow.
                if tail[m.end(): m.end() + 1] == "%":
                    continue
                if cls._DATE_TAIL_RE.match(tail[m.end():]):
                    continue
                value, inline_unit = cls._parse_number_scaled(m.group(0))
                if value is not None and abs(value) > 0.01:
                    nums.append((value, inline_unit))
            if 2 <= len(nums) <= 8:
                # Scale detected near this specific row, not a single
                # document-wide guess (see _local_scale). Applied per value
                # to whichever ones didn't state their own unit — see
                # _parse_number_scaled for why this is not a magnitude test.
                local_scale = cls._local_scale(text, row.start())
                scaled = [n if inline_unit else n * local_scale
                          for n, inline_unit in nums]
                return scaled[:6]
        return []

    #: Confirms a document uses the SEBI (India) LODR Regulation 33 standard
    #: quarterly-results table layout before any column position is trusted
    #: — a real, regulation-mandated header ("... Quarter ended ... Year
    #: Ended ...") every BSE/NSE-listed company's quarterly filing carries,
    #: not a guessed convention. Its data columns are always ordered
    #: [current quarter, immediately preceding quarter, same quarter one
    #: year earlier, full year] — see :meth:`_derive_yoy_metrics`.
    _SEBI_QUARTERLY_HEADER_RE = re.compile(r"quarter\s+ended[\s\S]{0,80}year\s+ended", re.IGNORECASE)

    @classmethod
    def _scrape_row_columns(
        cls, text: str, patterns: list[str], max_cols: int = 4, window: int = 200,
    ) -> list[float] | None:
        """Like :meth:`_scrape_fcf_series` but keyed off arbitrary patterns
        and capped at ``max_cols`` — used to read a specific row's full set
        of reporting-period columns (this quarter, last quarter, same
        quarter last year, full year), not just the first number after it.
        """
        for pat in patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                tail = text[match.end():match.end() + window]
                tail = cls._ARITH_REF_RE.sub(lambda m: " " * len(m.group(0)), tail)
                tail = cls._BROKEN_THOUSANDS_SEP_RE.sub(r",\1", tail)
                nums: list[float] = []
                for m in cls._NUMBER_RE.finditer(tail):
                    if cls._DATE_TAIL_RE.match(tail[m.end():]):
                        continue
                    raw, dec, unit = m.groups()
                    try:
                        value = float(raw.replace(",", "") + ("." + dec if dec else ""))
                    except ValueError:
                        continue
                    token = m.group(0)
                    if "(" in token and ")" in token:
                        value = -value
                    if unit:
                        value *= cls.SCALE_HINTS.get(unit.lower(), 1.0)
                    nums.append((value, bool(unit)))
                    if len(nums) >= max_cols:
                        break
                if len(nums) >= 3:   # need at least [current, ..., yoy] to be useful
                    local_scale = cls._local_scale(text, match.start())
                    return [n if inline_unit else n * local_scale
                            for n, inline_unit in nums]
        return None

    #: The column-header date row of a SEBI-format results statement, which
    #: labels every data column with the period it covers. Reading these is
    #: what turns the column ordering from an assumption into something the
    #: filing itself states — see :meth:`_ttm_column_dates`.
    #:
    #: Both real spellings are matched, because two real filings of the same
    #: regulated format disagree: Caplin Point heads its columns
    #: "30.06.2026 | 31.03.2026 | 30.06.2025 | 31.03.2026" and BLS
    #: International "June 30, 2026 | March 31, 2026 | June 30, 2025 |
    #: March 31, 2026". Handling only the numeric form silently withheld the
    #: TTM from every filing that writes its months out.
    _COLUMN_DATE_RE = re.compile(
        r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b"
        r"|\b(January|February|March|April|May|June|July|August|September"
        r"|October|November|December)\s+\d{1,2},?\s+(\d{4})\b",
        re.IGNORECASE,
    )

    _MONTH_NUMBERS = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }

    @classmethod
    def _column_month_index(cls, groups: tuple[str, ...]) -> int | None:
        """A column-header date as a months-since-year-zero ordinal.

        The day is deliberately dropped: these columns are always period
        ends, so the day is whatever that month's last day happens to be and
        carries no information the comparison needs.
        """
        _day, month, year, month_name, name_year = groups
        if month_name:
            return int(name_year) * 12 + cls._MONTH_NUMBERS[month_name.lower()]
        if year:
            return int(year) * 12 + int(month)
        return None

    #: Statement rows a TTM can be built from. Deliberately much narrower
    #: than the equivalent lists in :meth:`scrape_figures`: those include
    #: prose fallbacks ("Free Cash reserves are at ...") which state one
    #: figure for one period and so can never supply the four period columns
    #: this arithmetic needs. A TTM row has to be an actual statement row.
    #: Ordered STATUTORY LABEL FIRST, which is the opposite of
    #: :meth:`scrape_figures` and deliberately so. That method may legitimately
    #: take a figure from a press-release headline; a TTM may not, because
    #: only the statutory table carries the four dated period columns this
    #: arithmetic reads. Two real filings disagree about which label the
    #: statutory top line even uses — BLS calls it "Income from operations"
    #: and Caplin Point "Total income", reserving "Total Revenue" for its
    #: press release and investor deck — so both are tried before the
    #: narrative wordings. :meth:`scrape_ttm_flows` then verifies the row it
    #: found reconciles with the extracted quarterly figure.
    #: How far past the dated header the statutory table is taken to run.
    #: Generous enough for a full SEBI results statement including its
    #: segment breakdown, and far short of the press release and investor
    #: deck that real filings staple on afterwards.
    _TTM_TABLE_SPAN = 12_000

    _TTM_ROW_PATTERNS: dict[str, list[str]] = {
        "revenue": [r"income\s+from\s+operations", r"total\s+income",
                    r"total\s+revenue", r"net\s+revenue"],
        "net_income": [r"net\s+profit\s+for\s+the\s+\S+",
                       r"profit\s+for\s+the\s+(?:period|quarter|year)"],
        "interest_expense": [r"finance\s+costs?", r"interest\s+expense"],
        # Both conjunctions, as scrape_figures already handles: a real
        # Caplin Point statement writes "Depreciation & Amortisation
        # Expense" and a real Tesla 10-K "Depreciation and amortization".
        "depreciation_amortization": [
            r"depreciation\s*(?:&|and)\s*amorti[sz]ation(?:\s+expenses?)?"],
    }

    @classmethod
    def _ttm_column_dates(cls, text: str) -> int | None:
        """Does this filing's own header prove a TTM is derivable from it?

        ``TTM = full_year + current_quarter - same_quarter_last_year`` is
        the standard roll-forward, and it is **only** valid for a FIRST-
        quarter filing. At Q1 the stated full year is the year that ended
        immediately before this quarter, so adding this quarter and removing
        the one it replaces lands exactly on the last twelve months. At Q2
        the same three columns leave Q1 of the current year unaccounted for
        entirely, and the result is not a twelve-month figure at all — it
        merely looks like one, which is the more dangerous outcome.

        Rather than assume which quarter a filing covers, this reads the
        four dates SEBI requires in the column header and checks the
        relationship holds: the quarter-end column must fall one to four
        months after the year-end column, and the comparative quarter must
        sit twelve months before the current one. A filing whose header does
        not say so is left on the run-rate path — an unprovable TTM is not a
        TTM.

        Returns the offset just past the header — where the table it governs
        begins — so callers can confine themselves to that table, or ``None``.
        """
        header = cls._SEBI_QUARTERLY_HEADER_RE.search(text)
        if not header:
            return None
        # From the END of the "Quarter ended ... Year Ended" header, never
        # its start: the statement's own title ("...FOR THE QUARTER ENDED
        # JUNE 30, 2026") sits immediately before it and is itself a date.
        # Counting it shifts every column one place left, so the check then
        # compares the title against the current quarter and the real
        # full-year column is never looked at at all.
        window = text[header.end(): header.end() + 400]
        months = [i for i in (cls._column_month_index(g)
                              for g in cls._COLUMN_DATE_RE.findall(window))
                  if i is not None]
        if len(months) < 4:
            return None
        current_q, _prev_q, year_ago_q, full_year = months[:4]
        if 1 <= current_q - full_year <= 4 and current_q - year_ago_q == 12:
            return header.end()
        return None

    @classmethod
    def scrape_ttm_flows(
        cls, text: str, current: dict[str, float | None],
    ) -> dict[str, float]:
        """Trailing-twelve-month values for the flow rows this filing states.

        A quarterly filing's flows are otherwise put on an annual footing by
        multiplying by four, which assumes the other three quarters look
        like this one. For a real Caplin Point Q1 that assumption is worth
        ₹162 crore of revenue: the x4 run-rate is ₹2,575.64 Cr against a
        true TTM of ₹2,413.28 Cr (₹2,302.73 Cr full year + ₹643.91 Cr this
        quarter - ₹533.36 Cr the same quarter last year).

        Note this is *not* the same as reading the filing's "Year Ended"
        column directly. That column is the PRIOR COMPLETED financial year,
        which ended before the quarter being reported — using it would throw
        away the most recent quarter and report data up to a year stale.
        Three different numbers, and only the TTM is the last twelve months.

        ``current`` is the already-extracted quarterly value per field. Each
        row's own current-quarter column must agree with it, or the TTM is
        discarded: agreement is what proves both numbers came off the same
        physical statement row. Without that check the two resolve
        independently, and on a real BLS filing they landed on different
        lines of the same statement — "Income from operations" against
        "Total income", which differ by other income — so the annualised
        figure silently changed what "revenue" meant.

        Returns ``{}`` — meaning "use the run-rate" — unless
        :meth:`_ttm_column_dates` proves the arithmetic applies.
        """
        table_start = cls._ttm_column_dates(text)
        if table_start is None:
            return {}
        # Confined to the table the dated header actually governs. The dates
        # prove a column layout for THAT table and nothing else, and a real
        # Caplin Point filing shows why that matters: its press release
        # states "Total revenue at ₹644 Crores; an increase of 20.7% YoY"
        # and its PAT on the next line, so an unconfined row scan read
        # [644, 20.7, 179, 18.8] as four period columns and returned a
        # "TTM revenue" of ₹483.8 Cr — built from two percentages and a
        # profit figure, and close enough to a real number to pass every
        # sanity check downstream.
        table = text[table_start: table_start + cls._TTM_TABLE_SPAN]
        out: dict[str, float] = {}
        for field_name, patterns in cls._TTM_ROW_PATTERNS.items():
            quarter = current.get(field_name)
            if quarter is None or quarter == 0:
                continue
            cols = cls._scrape_row_columns(table, patterns)
            # Four columns exactly: fewer means a row this technique cannot
            # read, and _scrape_row_columns caps at four so more is impossible.
            if not cols or len(cols) != 4:
                continue
            # abs() both sides: an expense row is stored as a positive
            # magnitude by scrape_figures but may be parenthesised, hence
            # negative, in the table itself.
            if abs(abs(cols[0]) - abs(quarter)) > 0.005 * abs(quarter):
                continue
            ttm = cols[3] + cols[0] - cols[2]
            if abs(ttm) > 0:
                out[field_name] = abs(ttm) if quarter > 0 else ttm
        return out

    @classmethod
    def _derive_yoy_metrics(cls, text: str) -> dict[str, float]:
        """Real same-document YoY revenue growth / PBT margin / effective
        tax rate from a prior-year comparative column, instead of the
        generic 5% / 15% / 25% constants :class:`AutoAssumer` otherwise
        falls back to when a filing has no explicit "revenue growth" /
        "operating margin" / "tax rate" statement of its own — true for
        most quarterly results announcements, which report the *numbers*
        but rarely narrate the ratios.

        Only attempted when :attr:`_SEBI_QUARTERLY_HEADER_RE` confirms the
        specific, regulation-mandated column layout this relies on; returns
        ``{}`` (a no-op) for any other filing structure — a filing that
        doesn't match isn't a document this technique can safely read,
        not a case to guess through.
        """
        if not cls._SEBI_QUARTERLY_HEADER_RE.search(text[:6000]):
            return {}
        out: dict[str, float] = {}
        revenue_cols = cls._scrape_row_columns(
            text, [r"income\s+from\s+operations", r"total\s+income"])
        if revenue_cols and revenue_cols[2] > 0:
            out["revenue_growth"] = (revenue_cols[0] - revenue_cols[2]) / revenue_cols[2]
        # "Profit before tax" rather than a segment-table EBIT line — the
        # main statement's PBT row is unambiguous and already finance-cost-
        # adjusted; a genuinely lower (but real) margin proxy is safer here
        # than risking a mismatched segment-table row under a similar label.
        pbt_cols = cls._scrape_row_columns(
            text, [r"profit\s+before\s+(?:exceptional\s+items\s*&?\s*)?tax"])
        if revenue_cols and pbt_cols and revenue_cols[0] > 0:
            out["operating_margin"] = pbt_cols[0] / revenue_cols[0]
        tax_cols = cls._scrape_row_columns(text, [r"total\s+tax\s+expenses?"])
        if tax_cols and pbt_cols and pbt_cols[0] != 0:
            rate = tax_cols[0] / pbt_cols[0]
            if 0 <= rate < 1:   # plausibility guard — a real effective rate
                out["tax_rate"] = rate
        return out

    #: A bare 4-digit value that's also a plausible calendar year is almost
    #: certainly a mis-scrape (a nearby "fiscal 2025"/"as of 2026" caught
    #: instead of an actual percentage) — no real growth rate, margin or tax
    #: rate in a filing is ever going to land on e.g. exactly 2025%.
    @staticmethod
    def _not_year_like(value: float) -> bool:
        return not (1900 <= value <= 2099 and value == int(value))

    #: No real public company has under a thousand shares outstanding —
    #: reject a match that's actually an unrelated small number (a footnote
    #: reference, a vesting period) caught by too-broad a keyword pattern.
    @staticmethod
    def _plausible_share_count(value: float) -> bool:
        return value >= 1_000

    #: Below this, a number cannot be a literal share count for a listed
    #: company and must therefore be stated in a table header's unit (a US
    #: filing's "(in thousands, except per share data)"). Above it, the
    #: number already is a count and no header applies — see
    #: :meth:`_scrape_share_count`. Deliberately well under the smallest
    #: real free float rather than tuned to any one filing.
    _MIN_LISTED_SHARE_COUNT = 1e5

    #: The paid-up-capital row of a SEBI-format results statement, with the
    #: face value per share stated inline in the label itself. Two real
    #: spellings, both handled: Caplin Point's "Paid up Equity Share Capital
    #: (Face value of shares of Rs 2/- each)" and BLS's "Paid-up equity share
    #: capital ( Face Value Per Share Re. 1/-)" — note "Rs"/"Re", the
    #: optional hyphen, the stray inner space, and the "/-" suffix.
    _PAID_UP_CAPITAL_RE = re.compile(
        r"paid[\s-]?up\s+equity\s+share\s+capital[^\n]*?"
        r"(?:face\s+value[^\n]*?)?\bRs?e?\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*(?:each|per\s+share)?",
        re.IGNORECASE,
    )

    #: A board-recommended dividend as Indian filings actually word it:
    #: "Recommended a Final Dividend of Rs.4/- (200%) per equity share of
    #: Rs.2/- each". The amount and the face value are both "Rs.N/-" tokens in
    #: one sentence, so the pattern has to bind to the FIRST (the dividend)
    #: and stop before the second (the face value) — reading the wrong one
    #: silently reports a ₹2 dividend that was never declared.
    _DIVIDEND_PROSE_RE = re.compile(
        r"dividend\s+of\s+Rs?e?\.?\s*(\d+(?:\.\d+)?)\s*/?-?"
        r"(?:\s*\([^)]*\))?\s*per\s+(?:equity\s+)?share",
        re.IGNORECASE,
    )

    #: Wording that marks a declared dividend as covering a whole financial
    #: year rather than one reporting period — checked in the sentence around
    #: the match, not document-wide.
    _ANNUAL_DIVIDEND_CUES = (
        r"final\s+dividend",
        r"for\s+the\s+(?:financial\s+)?year\s+ended",
        r"per\s+annum",
    )

    @classmethod
    def _scrape_dividend_per_share(cls, text: str) -> tuple[float | None, bool]:
        """Dividend per share, and whether it is an annual declaration.

        The table-row wording ("Dividend per share  4.00") is tried first and
        unchanged. The prose form is what a results announcement actually
        carries, in the covering letter rather than any statement — and it is
        the reason a real Caplin Point filing reported the assumer's ₹2
        placeholder while the document itself declared ₹4 two pages earlier.

        Returns:
            ``(dividend_per_share, is_annual)``. ``is_annual`` is what stops
            a whole-year "Final Dividend" from being quadrupled into an
            annual rate it already is.
        """
        table_form = cls._first_after(
            text, [r"dividend\s+per\s+share", r"dps\b",
                   r"declared\s+dividends\s+per\s+share"])
        if table_form is not None:
            return table_form, False
        match = cls._DIVIDEND_PROSE_RE.search(text)
        if not match:
            return None, False
        context = text[max(0, match.start() - 120): match.end() + 160]
        is_annual = any(re.search(cue, context, re.IGNORECASE)
                        for cue in cls._ANNUAL_DIVIDEND_CUES)
        return float(match.group(1)), is_annual

    @classmethod
    def _scrape_share_count(cls, text: str) -> float | None:
        """Shares outstanding — read directly, or derived from paid-up capital.

        An Indian quarterly results statement never states a share count. It
        states paid-up equity share capital and the face value per share, and
        the count is exactly their quotient — Caplin Point's ₹15.20 Cr of
        paid-up capital at ₹2 face value is 76,000,000 shares; BLS's ₹4,117.41
        lakhs at Re.1 is 411,741,000. Without this derivation every Indian
        filing falls back to the assumer's placeholder share count, which is
        then divided into a real enterprise value to produce a per-share
        number that looks precise and means nothing.

        The direct US-style patterns are tried first and unchanged, so a 10-K
        behaves exactly as before.
        """
        # scale_below, uniquely, because this is the one field that is not
        # currency. A scale header declares a *currency* unit ("₹ in
        # lakhs", "$ in millions"): a US filing's "(in thousands, except
        # per share data)" does cover its share-count row, but an Indian
        # "(₹ in lakhs)" emphatically does not. A magnitude test is the
        # only signal that separates them, and here — unlike on the
        # monetary path it was just removed from — it rests on a real
        # domain floor rather than a guess: no listed company has fewer
        # than 100,000 shares outstanding, so a smaller value cannot be a
        # literal count and must be in the header's unit, while a larger
        # one already is a count whatever the header says.
        direct = cls._first_after(
            text, [r"shares\s+outstanding",
                   r"weighted[-\s]average\s+shares\s+outstanding",
                   r"diluted\s+shares"],
            apply_scale=True, scale_below=cls._MIN_LISTED_SHARE_COUNT,
            plausible=cls._plausible_share_count)
        if direct is not None:
            return direct

        for match in cls._PAID_UP_CAPITAL_RE.finditer(text):
            face_value = float(match.group(1))
            if face_value <= 0:
                continue
            capital, inline_unit = cls._parse_number_scaled(
                text[match.end(): match.end() + 120])
            if capital is None or capital <= 0:
                continue
            # Paid-up capital is a currency amount, so the table's scale
            # header applies to it (unlike the share count derived from it).
            if not inline_unit:
                capital *= cls._local_scale(text, match.start())
            shares = capital / face_value
            # Same floor the direct read uses — a derivation that lands
            # somewhere implausible is a mis-parse, not a small company.
            if cls._plausible_share_count(shares):
                return shares
        return None

    #: Confirms a "Current ... Long-Term" column-header pair precedes a
    #: debt row — e.g. a real Tesla 10-K debt-schedule table's header
    #: ("Net Carrying Value" split into "Current"/"Long-Term" sub-columns,
    #: OCR-run together as "...Maturity DateCurrent Long-Term"). Only
    #: trusted when confirmed nearby, so a filing with a plain single-
    #: figure "Total debt $X" line (the common case) is read exactly as
    #: before — see :meth:`_scrape_total_debt`.
    _CURRENT_LONGTERM_HEADER_RE = re.compile(
        r"current[^\n]{0,80}long[-\s]?term", re.IGNORECASE)

    @classmethod
    def _scrape_total_debt(cls, text: str) -> float | None:
        """Real total debt, summing Current + Long-Term columns when a
        confirmed debt-schedule table shows that's what a "Total debt"
        row's first two numbers actually are.

        A real Tesla 10-K's debt-schedule table reads "Total debt 1,569
        6,584 $ 8,177 $ 6,429" — four numeric columns: current-portion net
        carrying value, long-term-portion net carrying value, unpaid
        principal balance, and unused committed amount (definitely NOT
        debt). A plain first-number read grabs just the current portion
        (1,569), understating real total debt (current + long-term =
        8,153, close to the unpaid-principal column's 8,177) by ~5x. Only
        attempted when the "Current ... Long-Term" header pair is
        positively confirmed nearby; otherwise behaves exactly like a
        plain :meth:`_first_after` read (the common case: a single
        unbroken "Total debt $X" figure).
        """
        patterns = [r"total\s+debt", r"long[-\s]term\s+debt",
                    r"total\s+borrowings", r"\bborrowings\b"]
        for pat in patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                lookback = text[max(0, match.start() - 800):match.start()]
                if cls._CURRENT_LONGTERM_HEADER_RE.search(lookback):
                    tail = text[match.end():match.end() + 150]
                    tail = cls._ARITH_REF_RE.sub(lambda m: " " * len(m.group(0)), tail)
                    tail = cls._BROKEN_THOUSANDS_SEP_RE.sub(r",\1", tail)
                    nums: list[float] = []
                    for m in cls._NUMBER_RE.finditer(tail):
                        if cls._DATE_TAIL_RE.match(tail[m.end():]):
                            continue
                        raw, dec, unit = m.groups()
                        try:
                            v = float(raw.replace(",", "") + ("." + dec if dec else ""))
                        except ValueError:
                            continue
                        token = m.group(0)
                        if "(" in token and ")" in token:
                            v = -v
                        if unit:
                            v *= cls.SCALE_HINTS.get(unit.lower(), 1.0)
                        nums.append((v, bool(unit)))
                        if len(nums) >= 2:
                            break
                    if len(nums) == 2:
                        local_scale = cls._local_scale(text, match.start())
                        return sum(v if inline_unit else v * local_scale
                                   for v, inline_unit in nums)
                # No confirmed Current/Long-Term breakdown for this
                # occurrence — read it the same way any other field is.
                trailing = text[match.end():match.end() + 120]
                value, inline_unit = cls._parse_number_scaled(trailing)
                if value is None:
                    continue
                if not inline_unit:
                    value *= cls._local_scale(text, match.start())
                return value
        return None

    #: Phrase-level signal vocabulary per industry, keyed by the exact
    #: category name Damodaran's NYU Stern dataset uses (so
    #: :data:`src.pipeline.assumptions.SECTOR_BASELINES` can key off the
    #: same string with no translation layer). Every phrase is a multi-word
    #: or otherwise distinctive term — deliberately NOT bare generic nouns
    #: like "bank" or "insurance", which show up as incidental context in
    #: nearly any filing (a real BLS International filing mentions "term
    #: deposit ... with scheduled bank" while parking IPO proceeds; it is
    #: not a bank). :meth:`_classify_sector` additionally requires a real
    #: margin over the runner-up before committing to any of these — a
    #: single stray match is not enough on its own regardless.
    _SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
        "Drugs (Pharmaceutical)": (
            r"pharmaceutical", r"\bformulations?\b", r"active pharmaceutical ingredient",
            r"\bAPI\s+manufactur", r"\bANDA\b", r"\bUSFDA\b", r"generic drugs?",
        ),
        "Healthcare Products": (
            r"medical devices?", r"diagnostic kits?", r"healthcare products?",
            r"surgical instruments?", r"hospital equipment",
        ),
        "Software (System & Application)": (
            r"software product", r"\bSaaS\b", r"enterprise software",
            r"application software", r"software licen[cs]e",
        ),
        "Software (Internet)": (
            r"e-?commerce platform", r"internet-based", r"online marketplace",
            r"digital platform business",
        ),
        "Computer Services": (
            r"\bIT services\b", r"information technology services",
            r"software development services", r"system integration services",
            r"managed (?:IT )?services",
        ),
        "Business & Consumer Services": (
            # No bare "\bconsular\b" — it duplicated every "consular
            # services" match (the word is a strict substring of the
            # phrase), silently doubling this sector's score and defeating
            # the "no single stray match" guarantee the scoring depends on.
            # "consular outsourcing" is a real, independently-occurring
            # phrase in BLS's own filing (distinct from "consular services"),
            # so it's kept as its own non-overlapping pattern.
            r"outsourcing services", r"business process outsourcing", r"\bBPO\b",
            r"consular services", r"consular outsourcing",
            r"visa (?:outsourcing|application) services",
            r"staffing services", r"facility management services",
        ),
        "Bank (Money Center)": (
            r"scheduled commercial bank", r"banking company", r"net interest income",
            r"non-?performing assets?", r"\bCASA ratio\b", r"\bNBFC\b",
        ),
        "Insurance (General)": (
            r"insurance company", r"\bunderwriting\b", r"premium income",
            r"policyholders?", r"\bIRDAI\b",
        ),
        "Retail (General)": (
            r"retail stores?", r"apparel retail", r"department stores?",
            r"e-?commerce retail",
        ),
        "Retail (Grocery and Food)": (
            r"supermarkets?", r"grocery retail", r"hypermarkets?", r"food retail chain",
        ),
        "Auto & Truck": (
            r"automotive", r"electric vehicles?", r"automobile manufactur",
            r"vehicle production", r"passenger cars?",
        ),
        "Steel": (
            r"steel manufactur", r"steel plant", r"crude steel", r"blast furnace",
        ),
        "Metals & Mining": (
            r"mining operations", r"metals? and mining", r"ore extraction",
            r"mineral resources",
        ),
        "Real Estate (General/Diversified)": (
            r"real estate developer", r"residential projects?", r"commercial real estate",
            r"property development",
        ),
        "Telecom Services": (
            r"telecommunications services", r"telecom operator", r"fixed-?line",
            r"broadband services",
        ),
        "Telecom (Wireless)": (
            r"wireless carrier", r"mobile network operator", r"cellular services",
            r"\b5G network\b",
        ),
        "Power": (
            r"power generation", r"electricity distribution", r"power plants?",
            r"renewable energy generation",
        ),
        "Oil/Gas (Integrated)": (
            r"oil and gas exploration", r"refining and marketing",
            r"integrated oil compan", r"crude oil production",
        ),
        "Oil/Gas Production and Exploration": (
            r"exploration and production", r"upstream oil", r"oilfields?",
        ),
        "Chemical (Specialty)": (
            r"specialty chemicals?", r"chemical manufactur", r"industrial chemicals?",
        ),
        "Food Processing": (
            r"food processing", r"packaged foods?", r"food and beverage manufactur",
        ),
        "Building Materials": (
            r"cement manufactur", r"building materials", r"construction materials",
        ),
        "Engineering/Construction": (
            r"engineering,?\s*procurement\s*and\s*construction", r"\bEPC contracts?\b",
            r"infrastructure construction", r"civil construction",
        ),
        "Machinery": (
            r"industrial machinery", r"capital equipment manufactur", r"machine tools?",
        ),
        "Semiconductor": (
            r"semiconductors?", r"chip manufactur", r"wafer fabrication",
            r"integrated circuits?",
        ),
        "Apparel": (
            r"apparel manufactur", r"garments?", r"textile and apparel", r"clothing brand",
        ),
        "Hotel/Gaming": (
            r"hotel operations", r"hospitality business", r"casinos?", r"resort properties",
        ),
        "Air Transport": (
            # NOT bare "airlines?" — that matches inside any carrier's own
            # name ("Delta Airlines", "American Airlines"), so an aerospace
            # supplier, caterer, or airport-IT vendor that merely names its
            # airline CUSTOMERS would clear the hit floor on customer
            # references alone, with nothing actually contradicting it.
            r"air transport services", r"aviation services", r"airline operations",
            r"scheduled airline", r"passenger airline", r"commercial airline",
        ),
        "Transportation": (
            r"logistics services", r"freight transport", r"shipping and logistics",
            r"transportation services",
        ),
        "Publishing & Newspapers": (
            r"newspaper publishing", r"media publishing", r"print media",
        ),
        "Shipbuilding & Marine": (
            r"shipbuilding", r"shipyards?", r"marine vessel construction",
        ),
    }

    @classmethod
    def _classify_sector(cls, text: str) -> str | None:
        """A real, if approximate, industry classification — or ``None``.

        Scored by total occurrence count of each sector's phrase vocabulary
        (:attr:`_SECTOR_KEYWORDS`), not distinct-pattern count, so a filing
        that genuinely narrates its business ("the Company manufactures
        pharmaceutical formulations...", repeated across the cover page and
        MD&A) scores higher than one with a single incidental mention.

        Deliberately conservative in both directions this can fail: a
        misclassification is a WORSE failure than declining, because a
        wrong sector produces a wrong-but-authoritative-looking beta/margin/
        growth default (see :data:`src.pipeline.assumptions.SECTOR_BASELINES`)
        instead of an obviously-generic one. Two independent gates before
        committing to any classification:

        * The winning sector must accumulate at least 3 total keyword hits.
          A single incidental match (a real BLS International filing
          mentions parking IPO proceeds "with scheduled bank") never clears
          this on its own.
        * The winner must lead the runner-up by at least 2x (or the
          runner-up must have zero hits). A filing that genuinely discusses
          two businesses, or one whose real business isn't in this list at
          all and instead trips a few near-misses across several
          categories, is exactly the case this margin requirement declines
          rather than guesses through.
        """
        scores: dict[str, int] = {}
        for sector, patterns in cls._SECTOR_KEYWORDS.items():
            total = sum(len(re.findall(pat, text, re.IGNORECASE)) for pat in patterns)
            if total:
                scores[sector] = total
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        winner, top = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if top >= 3 and (runner_up == 0 or top >= 2 * runner_up):
            return winner
        return None

    def scrape_figures(self, text: str) -> ExtractedFinancials:
        """Apply regex heuristics to a raw text blob and populate a report.

        Args:
            text: Concatenated extracted text from :meth:`extract_text`.

        Returns:
            An :class:`ExtractedFinancials` with as many fields identified as
            possible. Unresolved fields remain ``None`` and will be filled by
            the assumer stage.
        """
        company = self._extract_company_name(text)
        # NYSE/NASDAQ/LSE cover-page listings ("NYSE: ACME"), plus every
        # wording an Indian filing uses for the same thing. There is no one
        # standard form: a real BLS filing writes "NSE Symbol: BLS", while a
        # real Caplin Point filing writes "NSE: CAPLIPOINT" in its press
        # release and "Scrip Code: CAPLIPOINT" in its covering letter — the
        # last of which the first two patterns miss entirely.
        #
        # The alphabetic-only constraint on the bare "Scrip Code:" form is
        # load-bearing, not cosmetic: the SAME label prefixes the numeric BSE
        # code ("Scrip Code: 524742") directly above it in that same letter,
        # and a numeric BSE code is an exchange identifier, not a ticker
        # symbol anyone can look a quote up with.
        ticker_match = (
            re.search(r"\b(?:NYSE|NASDAQ|LSE)\s*:\s*([A-Z]{1,6})\b", text)
            or re.search(r"\bNSE\s+Symbol\s*:\s*([A-Z]{1,10})\b", text, re.IGNORECASE)
            or re.search(r"\bNSE\s*:\s*([A-Z]{2,15})\b", text)
            or re.search(r"\bScrip\s+Code\s*:\s*([A-Z]{2,15})\b", text)
            # SEC cover page (10-K / 10-Q): a "Trading Symbol(s)" table whose
            # common-stock row reads "Common Stock, $0.25 Par Value KO New
            # York Stock Exchange". Anchored on the common-stock row and the
            # exchange name, so a notes row ("1.875% Notes Due 2026 KO26")
            # never supplies the symbol. Letter case is matched literally
            # for the symbol itself; only the surrounding words ignore case.
            or re.search(
                r"(?i:trading\s+symbol\(?s?\)?)[\s\S]{0,300}?"
                r"(?i:common\s+(?:stock|shares))[^\n]{0,80}?\s"
                r"([A-Z]{1,5}(?:[.-][A-Z])?)\s+"
                r"(?i:(?:the\s+)?nasdaq|new\s+york\s+stock\s+exchange|nyse)",
                text)
        )
        fy_match = re.search(r"(?:fiscal|for the year ended)[^\n]{0,40}(20\d{2})",
                             text, re.IGNORECASE)

        # Identity (company, ticker, fiscal year, currency) stays sourced from
        # the WHOLE document on purpose — those live in the covering letter
        # and cover page, which sit outside any financial-statement section.
        # Only the figures narrow to a single reporting basis; see
        # _select_statement_section for why mixing bases is the bug.
        figures_text, basis = self._select_statement_section(text)
        dividend, dividend_is_annual = self._scrape_dividend_per_share(text)

        data = ExtractedFinancials(
            company_name=company,
            ticker=ticker_match.group(1).upper() if ticker_match else None,
            fiscal_year=int(fy_match.group(1)) if fy_match else None,
            currency=self._detect_currency(text),
            statement_basis=basis,
            revenue=self._first_after(
                figures_text, [r"total\s+revenue", r"net\s+revenue",
                       # US GAAP manufacturers/retailers (Apple, Nike, most
                       # consumer/hardware filers) routinely never use the
                       # word "revenue" anywhere in their own income
                       # statement, labelling the consolidated top line
                       # "Total net sales" instead. Without this, a real
                       # Apple 10-Q fell all the way through to the bare
                       # "revenues?" catch-all below, which matched
                       # "Deferred revenue" on the BALANCE SHEET — a current
                       # liability, not revenue at all, off by >10x. Tried
                       # before the Ind AS patterns below since it's the
                       # same tier of thing: a specific, unambiguous label
                       # for the actual consolidated total.
                       r"total\s+net\s+sales",
                       # Ind AS / BSE-NSE quarterly-results wording: many
                       # Indian filings never use the word "revenue" for the
                       # consolidated top line at all, labelling it "Income
                       # from operations" (and the income-statement subtotal
                       # "Total income") instead. Tried before the bare
                       # "revenues?" catch-all below, which — as a broad,
                       # unqualified keyword — matches a segment-level
                       # sub-table row ("1 Segment revenue") just as readily
                       # as the real consolidated figure.
                       r"income\s+from\s+operations", r"total\s+income",
                       # (?<!segment ) — "Segment revenue" tables are a
                       # near-universal filing structure; without this guard
                       # this catch-all reliably matches a segment's own
                       # revenue line (a real, but wrong, number) rather than
                       # falling through to a pattern that finds the
                       # consolidated total.
                       r"(?<!segment )revenues?\b"],
                apply_scale=True, disqualify=self._FOOTNOTE_SCOPE_DISQUALIFIERS),
            free_cash_flows=self._scrape_fcf_series(figures_text),
            net_income=self._first_after(
                figures_text,
                [# US GAAP filings with a noncontrolling interest (Coca-Cola
                 # and any other company with partly-owned subsidiaries)
                 # report a "Consolidated Net Income" subtotal BEFORE
                 # deducting NCI, then the true bottom line as "Net Income
                 # Attributable to Shareowners/Shareholders" a line or two
                 # below it. Both contain the literal substring "net
                 # income", so the bare pattern below used to stop at the
                 # pre-NCI subtotal — the wrong figure, if a smaller one on
                 # a real KO filing (₹3,803 vs the real ₹3,810 attributable
                 # to shareowners). Tried first so the specific, correct
                 # label wins over the generic one whenever both are present.
                 r"net\s+income\s+attributable\s+to\s+(?:shareowners|"
                 r"shareholders|the\s+company|common\s+(?:share|stock)"
                 r"holders)",
                 r"net\s+income", r"net\s+earnings",
                       r"profit\s+for\s+the\s+(?:period|quarter|year)",
                       # Tolerant of the "period"/"year" itself being OCR-
                       # corrupted (a real scan turned it into "neriod/vear")
                       # — the distinctive, rarely-ambiguous part of this
                       # Ind AS statement label is "Net Profit for the ",
                       # not the exact word after it.
                       r"net\s+profit\s+for\s+the\s+\S+",
                       r"profit\s+after\s+tax", r"\bPAT\b"],
                apply_scale=True, disqualify=self._FOOTNOTE_SCOPE_DISQUALIFIERS),
            total_debt=self._scrape_total_debt(figures_text),
            cash_and_equivalents=self._first_after(
                figures_text,
                [r"cash\s+and\s+(?:cash\s+)?equivalents",
                 # Indian results releases state the cash position in prose
                 # rather than as a balance-sheet line, and never in the
                 # US-GAAP wording above — a real Caplin Point filing says
                 # "Free Cash reserves are at ₹1,502 Crores and Total Liquid
                 # Assets at ₹2,875 Crores". Cash reserves are listed first
                 # because they are the narrower, more cash-like measure;
                 # "liquid assets" includes investments a strict
                 # cash-and-equivalents line would exclude.
                 r"(?:free\s+)?cash\s+reserves?\s+(?:are\s+)?(?:at|of)",
                 r"total\s+liquid\s+assets\s+(?:are\s+)?(?:at|of)?"],
                apply_scale=True),
            shares_outstanding=self._scrape_share_count(figures_text),
            # "volatility" excluded: an option-pricing assumption in a stock-
            # comp footnote ("Expected share price volatility 60%"), not the
            # market price, but shares the "share price" keyword.
            #
            # The four fields below read the FULL document, not the selected
            # statement section: a market price, a declared dividend, a beta
            # and a stock-comp volatility assumption are all properties of
            # the *equity*, identical under either reporting basis, and are
            # stated outside the financial statements — a board-recommended
            # dividend appears in the covering letter, pages before any
            # statement section begins.
            current_price=self._first_after(
                text, [r"share\s+price(?!\s+volatility)",
                       r"stock\s+price(?!\s+volatility)", r"closing\s+price"],
                plausible=lambda v: 0 < v < 1_000_000 and self._not_year_like(v)),
            dividend_per_share=dividend,
            dividend_is_annual=dividend_is_annual,
            beta=self._first_after(text, [r"\bbeta\b"], window=30),
            revenue_growth=self._first_after_widening(
                figures_text, text,
                [r"revenue\s+growth", r"y[/-]?o[/-]?y\s+growth"], window=40,
                plausible=self._not_year_like),
            operating_margin=self._first_after_widening(
                figures_text, text, [r"operating\s+margin"], window=40,
                plausible=self._not_year_like),
            tax_rate=self._first_after_widening(
                figures_text, text,
                [r"effective\s+tax\s+rate", r"tax\s+rate"], window=40,
                plausible=self._not_year_like),
            depreciation_amortization=self._first_after(
                figures_text, [r"depreciation\s+and\s+amortization",
                               r"depreciation\s*(?:&|and)\s*amortisation"],
                apply_scale=True),
            rd_expense=self._first_after(
                figures_text, [r"research\s+and\s+development\s+expenses?",
                               r"research\s*(?:&|and)\s*development"],
                apply_scale=True),
            capital_expenditures=self._first_after(
                figures_text, [r"capital\s+expenditures?",
                               r"purchases?\s+of\s+property(?:,?\s+plant)?"
                               r"(?:\s*(?:&|and)\s*equipment)?",
                               # "after Capex investment of ₹55 Crores" — the
                               # prose form the same press releases use.
                               r"capex\s+investment\s+of"],
                apply_scale=True),
            interest_expense=self._first_after(
                figures_text, [r"interest\s+expense", r"finance\s+costs?"],
                apply_scale=True),
            disclosed_volatility=self._first_after(
                text, [r"expected\s+(?:share\s+price\s+|stock\s+price\s+)?volatility"],
                window=40, plausible=self._not_year_like),
        )

        # A percent scraped as e.g. "12" from "12%" should read as 0.12.
        for attr in ("revenue_growth", "operating_margin", "tax_rate", "disclosed_volatility"):
            v = getattr(data, attr)
            if v is not None and v > 1:
                setattr(data, attr, v / 100.0)

        # These three are always reported as a positive magnitude in the
        # downstream formulas (an add-back, an expense, a spend) even when
        # the source statement shows them as a parenthesised cash outflow.
        for attr in ("depreciation_amortization", "rd_expense", "capital_expenditures",
                     "interest_expense"):
            v = getattr(data, attr)
            if v is not None:
                setattr(data, attr, abs(v))

        # Real same-document YoY growth/margin/tax-rate, only for whichever
        # of the three the filing didn't already state explicitly above —
        # see _derive_yoy_metrics for why this is scoped to a specific,
        # confirmed table layout rather than attempted on every filing.
        if data.revenue_growth is None or data.operating_margin is None or data.tax_rate is None:
            yoy = self._derive_yoy_metrics(figures_text)
            if data.revenue_growth is None and "revenue_growth" in yoy:
                data.revenue_growth = yoy["revenue_growth"]
            if data.operating_margin is None and "operating_margin" in yoy:
                data.operating_margin = yoy["operating_margin"]
            if data.tax_rate is None and "tax_rate" in yoy:
                data.tax_rate = yoy["tax_rate"]

        # Read from the selected statement section for the same reason every
        # absolute figure is: a TTM built by mixing a consolidated quarter
        # into a standalone full year is two different companies' arithmetic.
        data.ttm_flows = self.scrape_ttm_flows(
            figures_text,
            {f: getattr(data, f) for f in self._TTM_ROW_PATTERNS})

        # Not figures_text (a business description is narrative, not a
        # statement-section figure, and routinely sits on the cover page
        # before any statement begins), but bounded to the same 50k-char
        # slice raw_text keeps below — running ~150 IGNORECASE regex passes
        # over a full 300-500KB 10-K would block the Pyodide/WASM main
        # thread for real, user-visible seconds on every upload. A
        # company's own business description is front-loaded (cover page,
        # Item 1) on every real filing this pipeline has been tested
        # against — confirmed unchanged on the Caplin fixture, whose real
        # text exceeds 50k chars (22 pharma-vocabulary hits full text vs 21
        # in the first 50k, same winner by the same decisive margin).
        data.sector = self._classify_sector(text[:50_000])

        data.raw_text = text[:50_000]
        return data

    #: A sign-off capture that names a *body* rather than the filer — "For
    #: and on behalf of the Board" / "...the Board of Directors" is an
    #: equally standard closing to "For and on behalf of, <Company Name>",
    #: and a real Caplin Point filing uses exactly that form (its actual
    #: company-name sign-off, "For Caplin Point Laboratories Limited", omits
    #: the "on behalf of" entirely). Without this guard the extractor
    #: reported the company as literally "the Board" — a wrong-but-
    #: plausible-looking value that then headlined every downstream report.
    _GENERIC_SIGNOFF_RE = re.compile(
        r"^(?:the\s+)?(?:board|directors?|board\s+of\s+directors|company|"
        r"management|above)\b",
        re.IGNORECASE,
    )

    #: An ALL-CAPS statement header naming the filer — "CAPLIN POINT
    #: LABORATORIES LIMITED", "BLS INTERNATIONAL SERVICES LIMITED". Every
    #: SEBI-format results statement repeats this immediately above the
    #: financial table, which makes it a stronger and more universal signal
    #: than any sign-off phrasing. Requires a legal-form suffix so a
    #: shouted section heading ("STATEMENT OF UNAUDITED RESULTS") can't
    #: match. Scanned line-wise rather than free-text so an OCR-mangled
    #: neighbour line can't bleed into the capture.
    _ALLCAPS_FILER_RE = re.compile(
        r"^[A-Z][A-Z&.,'()\- ]{4,70}?\s(?:LIMITED|LTD\.?|INC\.?|CORPORATION|CORP\.?|PLC|LLP)\.?$"
    )

    @classmethod
    def _extract_company_name(cls, text: str) -> str | None:
        """Locate the filer's actual name, preferring explicit signals over guesswork.

        The original approach — take the first short non-numeric-leading
        line — reliably picks up boilerplate instead of the real name: every
        SEC 10-K cover page starts with the literal line ``UNITED STATES``
        (then ``SECURITIES AND EXCHANGE COMMISSION``, ``FORM 10-K``, ...)
        before the actual company name appears; every BSE/NSE regulatory
        filing is formatted as a letter starting with the filing date.
        Format-specific signals, in descending order of reliability:

        1. SEC cover pages state the name immediately before the phrase
           "(Exact name of registrant as specified in its charter)".
        2. Indian board-resolution letters close with "For and on behalf
           of, <Company Name>" — but only when what follows is actually a
           name and not a body (see :attr:`_GENERIC_SIGNOFF_RE`).
        3. The ALL-CAPS filer header every SEBI-format results statement
           carries directly above its financial table (see
           :attr:`_ALLCAPS_FILER_RE`) — the signal that rescues a filing
           whose only "on behalf of" phrasing names the Board.

        The original first-short-line heuristic is kept as a last-resort
        fallback for formats that match none of the three.
        """
        sec_cover = re.search(
            r"([A-Z][^\n(]{2,78}?)\s*\(Exact name of registrant",
            text, re.IGNORECASE,
        )
        if sec_cover:
            return sec_cover.group(1).strip().rstrip(",.")

        # Every occurrence, not just the first: a long filing can carry an
        # auditor's or a director's "on behalf of" block before the filer's
        # own, and only the latter names a company.
        for signoff in re.finditer(
            r"for and on behalf of[,:]?\s+([^\n]{3,80})", text, re.IGNORECASE,
        ):
            candidate = signoff.group(1).strip().rstrip(",.")
            if candidate and not cls._GENERIC_SIGNOFF_RE.match(candidate):
                return candidate

        for line in text.splitlines():
            stripped = line.strip()
            if cls._ALLCAPS_FILER_RE.match(stripped):
                return stripped

        first_lines = [ln.strip() for ln in text.splitlines()[:15] if ln.strip()]
        return next(
            (ln for ln in first_lines
             if 3 <= len(ln) <= 80 and not any(c.isdigit() for c in ln[:6])),
            None,
        )

    # ------------------------------------------------------------------ #
    # Public entry-point
    # ------------------------------------------------------------------ #
    def extract(self, source: str | Path | bytes) -> ExtractedFinancials:
        """End-to-end extraction: text cascade → figure scraping.

        Args:
            source: PDF path or bytes.

        Returns:
            An :class:`ExtractedFinancials` populated as far as the heuristics
            can go.

        Raises:
            ModelError: If no backend can read the PDF.
            ValidationError: If the input is neither a path nor bytes.
        """
        if not isinstance(source, (str, Path, bytes, bytearray)):
            raise ValidationError(
                f"source must be a path or bytes, got {type(source).__name__}."
            )
        if isinstance(source, (str, Path)) and not Path(source).is_file():
            raise ValidationError(f"PDF file not found: {source!r}")
        text, used = self.extract_text(source)
        data = self.scrape_figures(text)
        data.backends_used = used
        return data
