"""PDF-to-model analysis pipeline.

Turns an uploaded company financial PDF into a live valuation across the ten
models. The pipeline is split into four testable stages:

1. :mod:`~src.pipeline.pdf_extractor` — cascade text/table extraction using
   PyMuPDF → pdfplumber → pypdf → pdfminer.six, then regex-based scraping of
   financial figures (revenue, FCF, net debt, shares, beta, growth, …).
2. :mod:`~src.pipeline.assumptions` — ``AutoAssumer`` (IB-style heuristics that
   fill missing inputs) and ``ManualAssumer`` (read-through of user-provided
   overrides from the notebook widgets).
3. :mod:`~src.pipeline.runner` — orchestrates model construction, runs the
   selected subset and returns an :class:`AnalysisReport`.
4. :mod:`~src.pipeline.exporters` — writes the report as a PDF (reportlab),
   Excel workbook (openpyxl) or Google Doc (googleapiclient).
"""

from __future__ import annotations

from .assumptions import (
    AssumptionSet,
    AutoAssumer,
    ManualAssumer,
    ManualOverrides,
)
from .exporters import export_google_doc, export_pdf, export_xlsx
from .pdf_extractor import ExtractedFinancials, PDFExtractor
from .runner import AnalysisReport, AnalysisRunner, AVAILABLE_MODELS

__all__ = [
    "AssumptionSet", "AutoAssumer", "ManualAssumer", "ManualOverrides",
    "ExtractedFinancials", "PDFExtractor",
    "AnalysisReport", "AnalysisRunner", "AVAILABLE_MODELS",
    "export_pdf", "export_xlsx", "export_google_doc",
]
