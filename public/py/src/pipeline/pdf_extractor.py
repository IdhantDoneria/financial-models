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
from typing import Any

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

    #: Multipliers for "in millions" / "in thousands" / "in billions" headers.
    SCALE_HINTS = {
        "billion": 1e9, "bn": 1e9, "bil": 1e9,
        "million": 1e6, "mn": 1e6, "mil": 1e6, "mm": 1e6,
        "thousand": 1e3, "k": 1e3,
    }

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
        (?![\w.])
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    @classmethod
    def _parse_number(cls, match_text: str) -> float | None:
        """Parse a single numeric token, honouring $, (), commas and scale suffix."""
        m = cls._NUMBER_RE.search(match_text)
        if not m:
            return None
        raw, dec, unit = m.groups()
        try:
            value = float(raw.replace(",", "") + ("." + dec if dec else ""))
        except ValueError:
            return None
        if "(" in match_text and ")" in match_text:
            value = -value
        if unit:
            value *= cls.SCALE_HINTS.get(unit.lower(), 1.0)
        return value

    @classmethod
    def _first_after(cls, text: str, patterns: list[str], window: int = 120) -> float | None:
        """Return the first number appearing after any of ``patterns`` (case-insensitive)."""
        for pat in patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                trailing = text[match.end() : match.end() + window]
                value = cls._parse_number(trailing)
                if value is not None:
                    return value
        return None

    @classmethod
    def _guess_scale_for_statement(cls, text: str) -> float:
        """Detect "in $ millions" / "in $ billions" headers and return the multiplier."""
        header = text[:6000].lower()
        if re.search(r"in\s+\$?\s*billion", header) or "in $bn" in header:
            return 1e9
        if re.search(r"in\s+\$?\s*million", header) or "in $mm" in header or "in $m" in header:
            return 1e6
        if re.search(r"in\s+\$?\s*thousand", header):
            return 1e3
        return 1.0

    @classmethod
    def _scrape_fcf_series(cls, text: str, scale: float) -> list[float]:
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
                scaled = [n * scale if abs(n) < 1e5 else n for n in nums]
                return scaled[:6]
        return []

    def scrape_figures(self, text: str) -> ExtractedFinancials:
        """Apply regex heuristics to a raw text blob and populate a report.

        Args:
            text: Concatenated extracted text from :meth:`extract_text`.

        Returns:
            An :class:`ExtractedFinancials` with as many fields identified as
            possible. Unresolved fields remain ``None`` and will be filled by
            the assumer stage.
        """
        scale = self._guess_scale_for_statement(text)
        raw_or_scaled = lambda v: v * scale if (v is not None and abs(v) < 1e5) else v

        # Company name — take the first non-empty line if it looks like a title.
        first_lines = [ln.strip() for ln in text.splitlines()[:15] if ln.strip()]
        company = next(
            (ln for ln in first_lines
             if 3 <= len(ln) <= 80 and not any(c.isdigit() for c in ln[:6])),
            None,
        )
        ticker_match = re.search(r"\b(?:NYSE|NASDAQ|LSE)\s*:\s*([A-Z]{1,6})\b", text)
        fy_match = re.search(r"(?:fiscal|for the year ended)[^\n]{0,40}(20\d{2})",
                             text, re.IGNORECASE)

        data = ExtractedFinancials(
            company_name=company,
            ticker=ticker_match.group(1) if ticker_match else None,
            fiscal_year=int(fy_match.group(1)) if fy_match else None,
            revenue=raw_or_scaled(self._first_after(
                text, [r"total\s+revenue", r"net\s+revenue", r"revenues?\b"])),
            free_cash_flows=self._scrape_fcf_series(text, scale),
            net_income=raw_or_scaled(self._first_after(
                text, [r"net\s+income", r"net\s+earnings"])),
            total_debt=raw_or_scaled(self._first_after(
                text, [r"total\s+debt", r"long[-\s]term\s+debt"])),
            cash_and_equivalents=raw_or_scaled(self._first_after(
                text, [r"cash\s+and\s+(?:cash\s+)?equivalents"])),
            shares_outstanding=raw_or_scaled(self._first_after(
                text, [r"shares\s+outstanding",
                       r"weighted[-\s]average\s+shares\s+outstanding",
                       r"diluted\s+shares"])),
            current_price=self._first_after(
                text, [r"share\s+price", r"stock\s+price", r"closing\s+price"]),
            dividend_per_share=self._first_after(
                text, [r"dividend\s+per\s+share", r"dps\b",
                       r"declared\s+dividends\s+per\s+share"]),
            beta=self._first_after(text, [r"\bbeta\b"], window=30),
            revenue_growth=self._first_after(
                text, [r"revenue\s+growth", r"y[/-]?o[/-]?y\s+growth"], window=40),
            operating_margin=self._first_after(
                text, [r"operating\s+margin"], window=40),
            tax_rate=self._first_after(
                text, [r"effective\s+tax\s+rate", r"tax\s+rate"], window=40),
        )

        # A percent scraped as e.g. "12" from "12%" should read as 0.12.
        for attr in ("revenue_growth", "operating_margin", "tax_rate"):
            v = getattr(data, attr)
            if v is not None and v > 1:
                setattr(data, attr, v / 100.0)

        data.raw_text = text[:50_000]
        return data

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
