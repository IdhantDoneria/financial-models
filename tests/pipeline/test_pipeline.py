"""End-to-end tests for the PDF-analyser pipeline.

Validates every stage:
    1. **Extractor** — builds a synthetic 10-K PDF, extracts, verifies scraped
       fields.
    2. **AutoAssumer** — every model receives well-formed kwargs; missing
       inputs get sensible defaults.
    3. **ManualAssumer** — every override propagates.
    4. **Runner** — runs all ten models on synthetic data without exceptions.
    5. **Exporters** — PDF and XLSX outputs are created and non-empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from src.pipeline import (
    AVAILABLE_MODELS,
    AnalysisRunner,
    AutoAssumer,
    ManualAssumer,
    ManualOverrides,
    PDFExtractor,
    export_pdf,
    export_xlsx,
)


# --------------------------------------------------------------------------- #
# Fixture: synthetic company PDF
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def synthetic_pdf(tmp_path_factory) -> Path:
    """Build a small synthetic 10-K-style PDF for the extractor tests."""
    path = tmp_path_factory.mktemp("pdf") / "synth.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    doc.build([
        Paragraph("Acme Corporation", styles["Title"]),
        Paragraph("NYSE: ACME", styles["Normal"]),
        Paragraph("Consolidated Financial Statements (in $ millions)", styles["Normal"]),
        Paragraph("For the fiscal year ended December 31, 2025", styles["Normal"]),
        Spacer(1, 20),
        Paragraph("Total revenue     $ 12,450", styles["Normal"]),
        Paragraph("Net income        $ 1,830", styles["Normal"]),
        Paragraph("Total debt        $ 3,500", styles["Normal"]),
        Paragraph("Cash and cash equivalents $ 850", styles["Normal"]),
        Paragraph("Weighted-average shares outstanding 425 million", styles["Normal"]),
        Paragraph("Share price $ 87.50", styles["Normal"]),
        Paragraph("Beta 1.15", styles["Normal"]),
        Paragraph("Revenue growth 12%", styles["Normal"]),
        Paragraph("Operating margin 18%", styles["Normal"]),
        Paragraph("Effective tax rate 22%", styles["Normal"]),
    ])
    return path


# --------------------------------------------------------------------------- #
# 1. Extraction
# --------------------------------------------------------------------------- #
def test_extract_recovers_company_and_headline_figures(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assert data.company_name and "Acme" in data.company_name
    assert data.ticker == "ACME"
    assert data.fiscal_year == 2025
    assert data.revenue == pytest.approx(12_450 * 1e6, rel=0.01)
    assert data.total_debt == pytest.approx(3_500 * 1e6, rel=0.01)
    assert data.cash_and_equivalents == pytest.approx(850 * 1e6, rel=0.01)
    assert data.net_debt == pytest.approx(2_650 * 1e6, rel=0.01)
    assert data.current_price == pytest.approx(87.5, rel=0.01)
    assert data.beta == pytest.approx(1.15, rel=0.01)
    assert 0 < (data.revenue_growth or 0) < 1     # decimalised from percent
    assert data.backends_used                     # at least one backend fired


def test_extractor_rejects_missing_file():
    from src.base_model import ValidationError
    with pytest.raises(ValidationError):
        PDFExtractor().extract("/no/such/file.pdf")


# --------------------------------------------------------------------------- #
# 2. Auto assumer — every model gets kwargs
# --------------------------------------------------------------------------- #
def test_auto_assumer_covers_every_model(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    for name in AVAILABLE_MODELS:
        assert name in assumptions.kwargs_by_model, f"missing kwargs for {name}"
    ctx = assumptions.market_context
    assert 0 < ctx["risk_free_rate"] < 0.10
    assert ctx["terminal_growth"] <= ctx["risk_free_rate"]   # Gordon constraint


# --------------------------------------------------------------------------- #
# 3. Manual overrides propagate
# --------------------------------------------------------------------------- #
def test_manual_overrides_reach_kwargs(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    ov = ManualOverrides(risk_free_rate=0.06, beta=2.0, discount_rate=0.14,
                         terminal_growth=0.035, volatility=0.5)
    a = ManualAssumer().build(data, ov)
    assert a.market_context["risk_free_rate"] == pytest.approx(0.06)
    assert a.market_context["wacc"] == pytest.approx(0.14)
    assert a.market_context["beta"] == pytest.approx(2.0)
    assert a.market_context["volatility"] == pytest.approx(0.5)
    assert a.kwargs_by_model["Black-Scholes-Merton"]["sigma"] == pytest.approx(0.5)
    assert a.kwargs_by_model["Discounted Cash Flow"]["discount_rate"] == pytest.approx(0.14)


# --------------------------------------------------------------------------- #
# 4. Runner executes every model without exceptions
# --------------------------------------------------------------------------- #
def test_runner_all_models_produce_results(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    assumptions = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(assumptions, list(AVAILABLE_MODELS), mode="auto")
    assert not report.errors, f"models failed: {report.errors}"
    assert set(report.results) == set(AVAILABLE_MODELS)
    for name, res in report.results.items():
        assert res and isinstance(res, dict)


def test_runner_respects_selection_subset(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    subset = ["Discounted Cash Flow", "CAPM"]  # note: 'CAPM' is not the registered key
    subset = ["Discounted Cash Flow", "Capital Asset Pricing Model"]
    a = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(a, subset, mode="auto")
    assert set(report.results) == set(subset)


# --------------------------------------------------------------------------- #
# 5. Exporters produce non-empty files
# --------------------------------------------------------------------------- #
def test_export_pdf_and_xlsx(synthetic_pdf, tmp_path):
    data = PDFExtractor().extract(synthetic_pdf)
    report = AnalysisRunner(data).run(
        AutoAssumer().build(data), list(AVAILABLE_MODELS), mode="auto"
    )
    pdf = export_pdf(report, tmp_path / "report.pdf")
    xlsx = export_xlsx(report, tmp_path / "report.xlsx")
    assert pdf.exists() and pdf.stat().st_size > 2000
    assert xlsx.exists() and xlsx.stat().st_size > 3000
    # PDF header sanity
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_summary_frame_has_row_per_selected_model(synthetic_pdf):
    data = PDFExtractor().extract(synthetic_pdf)
    a = AutoAssumer().build(data)
    report = AnalysisRunner(data).run(a, list(AVAILABLE_MODELS), mode="auto")
    df = report.summary_frame()
    assert len(df) == len(AVAILABLE_MODELS)
    assert set(df.columns) == {"Model", "Headline result", "Status"}
