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
    free_cash_flows: list[float] = field(default_factory=list)
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
    #: ISO 4217 code the filing's own figures are denominated in (detected
    #: from currency symbols/codes in the document text — see
    #: :meth:`PDFExtractor._detect_currency`). Every monetary field above is
    #: in raw units of THIS currency, not necessarily USD — a DCF/RDCF
    #: headline built from an undetected non-USD filing and labelled "$"
    #: would misrepresent the actual scale by whatever the real FX rate is.
    #: Defaults to "USD" (this pipeline's original, implicit assumption)
    #: when the document gives no more specific signal.
    currency: str = "USD"
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
            "currency": self.currency,
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
        (\d{1,3}(?:,\d{3})+|\d+)            # integer part w/ optional thousands sep
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
        """Parse the first *plausible* numeric token, honouring $, (), commas and scale suffix.

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
            return value
        return None

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
                :meth:`_local_scale`) rather than leaving it in raw document units.
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
                value = cls._parse_number(trailing)
                if value is None:
                    continue
                if apply_scale and abs(value) < 1e5:
                    value *= cls._local_scale(text, match.start())
                if plausible is not None and not plausible(value):
                    continue
                return value
        return None

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

    @classmethod
    def _scrape_fcf_series(cls, text: str) -> list[float]:
        """Find a Free Cash Flow row and return its multi-year values (in $)."""
        rows = re.finditer(
            r"(free\s+cash\s+flow|fcf|cash\s+flow\s+from\s+operations\s*-\s*capex)"
            r"[^\n]*\n?([\s\S]{0,200})",
            text, re.IGNORECASE,
        )
        for row in rows:
            tail = row.group(2)
            # Extract every numeric token; keep 3-7 realistic values.
            nums = [cls._parse_number(m.group(0)) for m in cls._NUMBER_RE.finditer(tail)]
            nums = [n for n in nums if n is not None and abs(n) > 0.01]
            if 2 <= len(nums) <= 8:
                # Scale detected near this specific row, not a single
                # document-wide guess (see _local_scale).
                local_scale = cls._local_scale(text, row.start())
                scaled = [n * local_scale if abs(n) < 1e5 else n for n in nums]
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
                    nums.append(value)
                    if len(nums) >= max_cols:
                        break
                if len(nums) >= 3:   # need at least [current, ..., yoy] to be useful
                    local_scale = cls._local_scale(text, match.start())
                    return [n * local_scale if abs(n) < 1e5 else n for n in nums]
        return None

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
                        nums.append(v)
                        if len(nums) >= 2:
                            break
                    if len(nums) == 2:
                        total = nums[0] + nums[1]
                        if abs(total) < 1e5:
                            total *= cls._local_scale(text, match.start())
                        return total
                # No confirmed Current/Long-Term breakdown for this
                # occurrence — read it the same way any other field is.
                trailing = text[match.end():match.end() + 120]
                value = cls._parse_number(trailing)
                if value is None:
                    continue
                if abs(value) < 1e5:
                    value *= cls._local_scale(text, match.start())
                return value
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
        # NYSE/NASDAQ/LSE cover-page listings ("NYSE: ACME"), plus the
        # "NSE Symbol: X" / "BSE Scrip Code: N" wording Indian filings use
        # instead (scrip codes are numeric, so only the NSE symbol form
        # yields a ticker here).
        ticker_match = (
            re.search(r"\b(?:NYSE|NASDAQ|LSE)\s*:\s*([A-Z]{1,6})\b", text)
            or re.search(r"\bNSE\s+Symbol\s*:\s*([A-Z]{1,10})\b", text, re.IGNORECASE)
        )
        fy_match = re.search(r"(?:fiscal|for the year ended)[^\n]{0,40}(20\d{2})",
                             text, re.IGNORECASE)

        data = ExtractedFinancials(
            company_name=company,
            ticker=ticker_match.group(1).upper() if ticker_match else None,
            fiscal_year=int(fy_match.group(1)) if fy_match else None,
            currency=self._detect_currency(text),
            revenue=self._first_after(
                text, [r"total\s+revenue", r"net\s+revenue",
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
            free_cash_flows=self._scrape_fcf_series(text),
            net_income=self._first_after(
                text, [r"net\s+income", r"net\s+earnings",
                       r"profit\s+for\s+the\s+(?:period|quarter|year)",
                       # Tolerant of the "period"/"year" itself being OCR-
                       # corrupted (a real scan turned it into "neriod/vear")
                       # — the distinctive, rarely-ambiguous part of this
                       # Ind AS statement label is "Net Profit for the ",
                       # not the exact word after it.
                       r"net\s+profit\s+for\s+the\s+\S+",
                       r"profit\s+after\s+tax", r"\bPAT\b"],
                apply_scale=True, disqualify=self._FOOTNOTE_SCOPE_DISQUALIFIERS),
            total_debt=self._scrape_total_debt(text),
            cash_and_equivalents=self._first_after(
                text, [r"cash\s+and\s+(?:cash\s+)?equivalents"], apply_scale=True),
            shares_outstanding=self._first_after(
                text, [r"shares\s+outstanding",
                       r"weighted[-\s]average\s+shares\s+outstanding",
                       r"diluted\s+shares"],
                apply_scale=True, plausible=self._plausible_share_count),
            # "volatility" excluded: an option-pricing assumption in a stock-
            # comp footnote ("Expected share price volatility 60%"), not the
            # market price, but shares the "share price" keyword.
            current_price=self._first_after(
                text, [r"share\s+price(?!\s+volatility)",
                       r"stock\s+price(?!\s+volatility)", r"closing\s+price"],
                plausible=lambda v: 0 < v < 1_000_000 and self._not_year_like(v)),
            dividend_per_share=self._first_after(
                text, [r"dividend\s+per\s+share", r"dps\b",
                       r"declared\s+dividends\s+per\s+share"]),
            beta=self._first_after(text, [r"\bbeta\b"], window=30),
            revenue_growth=self._first_after(
                text, [r"revenue\s+growth", r"y[/-]?o[/-]?y\s+growth"], window=40,
                plausible=self._not_year_like),
            operating_margin=self._first_after(
                text, [r"operating\s+margin"], window=40,
                plausible=self._not_year_like),
            tax_rate=self._first_after(
                text, [r"effective\s+tax\s+rate", r"tax\s+rate"], window=40,
                plausible=self._not_year_like),
            depreciation_amortization=self._first_after(
                text, [r"depreciation\s+and\s+amortization",
                       r"depreciation\s*(?:&|and)\s*amortisation"],
                apply_scale=True),
            rd_expense=self._first_after(
                text, [r"research\s+and\s+development\s+expenses?",
                       r"research\s*(?:&|and)\s*development"],
                apply_scale=True),
            capital_expenditures=self._first_after(
                text, [r"capital\s+expenditures?",
                       r"purchases?\s+of\s+property(?:,?\s+plant)?"
                       r"(?:\s*(?:&|and)\s*equipment)?"],
                apply_scale=True),
            interest_expense=self._first_after(
                text, [r"interest\s+expense", r"finance\s+costs?"],
                apply_scale=True),
        )

        # A percent scraped as e.g. "12" from "12%" should read as 0.12.
        for attr in ("revenue_growth", "operating_margin", "tax_rate"):
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
            yoy = self._derive_yoy_metrics(text)
            if data.revenue_growth is None and "revenue_growth" in yoy:
                data.revenue_growth = yoy["revenue_growth"]
            if data.operating_margin is None and "operating_margin" in yoy:
                data.operating_margin = yoy["operating_margin"]
            if data.tax_rate is None and "tax_rate" in yoy:
                data.tax_rate = yoy["tax_rate"]

        data.raw_text = text[:50_000]
        return data

    @staticmethod
    def _extract_company_name(text: str) -> str | None:
        """Locate the filer's actual name, preferring explicit signals over guesswork.

        The previous approach — take the first short non-numeric-leading
        line — reliably picks up boilerplate instead of the real name: every
        SEC 10-K cover page starts with the literal line ``UNITED STATES``
        (then ``SECURITIES AND EXCHANGE COMMISSION``, ``FORM 10-K``, ...)
        before the actual company name appears; every BSE/NSE regulatory
        filing is formatted as a letter starting with the filing date. Two
        much more reliable, format-specific signals exist instead:

        1. SEC cover pages state the name immediately before the phrase
           "(Exact name of registrant as specified in its charter)".
        2. Indian board-resolution letters close with "For and on behalf
           of, <Company Name>".

        The original first-short-line heuristic is kept as a last-resort
        fallback for formats that match neither.
        """
        sec_cover = re.search(
            r"([A-Z][^\n(]{2,78}?)\s*\(Exact name of registrant",
            text, re.IGNORECASE,
        )
        if sec_cover:
            return sec_cover.group(1).strip().rstrip(",.")

        signoff = re.search(
            r"for and on behalf of[,:]?\s+([^\n]{3,80})",
            text, re.IGNORECASE,
        )
        if signoff:
            return signoff.group(1).strip()

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
