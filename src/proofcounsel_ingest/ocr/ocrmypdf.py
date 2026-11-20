"""OCRmyPDF normalization plus Tesseract confidence probing."""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from proofcounsel_ingest.extractors.base import ExtractionContext
from proofcounsel_ingest.ocr.base import OcrResult
from proofcounsel_ingest.security.sandbox import run_sandboxed


class OcrMyPdfAdapter:
    def available(self, context: ExtractionContext) -> bool:
        return bool(
            context.settings.resolved_binary("ocrmypdf_binary", "ocrmypdf")
            or (
                context.settings.resolved_binary("pdftoppm_binary", "pdftoppm")
                and context.settings.resolved_binary("tesseract_binary", "tesseract")
            )
        )

    def process_pdf(
        self,
        source: Path,
        pages: list[int],
        output_pdf: Path,
        context: ExtractionContext,
    ) -> OcrResult:
        if not pages:
            raise ValueError("OCR requires at least one page")
        ocrmypdf = context.settings.resolved_binary("ocrmypdf_binary", "ocrmypdf")
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        if not ocrmypdf:
            return self._process_with_tesseract(source, pages, output_pdf, context)
        page_spec = ",".join(str(page) for page in sorted(set(pages)))
        command = [
            ocrmypdf,
            "--pages",
            page_spec,
            "--force-ocr",
            "--rotate-pages",
            "--deskew",
            "--output-type",
            "pdf",
            "--jobs",
            "1",
            "--tesseract-timeout",
            str(max(30, int(context.sandbox_profile.timeout_seconds / max(len(pages), 1)))),
            "--language",
            context.settings.ocr_languages,
            str(source.resolve()),
            str(output_pdf.resolve()),
        ]
        run_sandboxed(command, cwd=output_pdf.parent, profile=context.sandbox_profile)
        confidence = _probe_page_confidence(output_pdf, pages, context)
        low_confidence_pages = [page for page, score in confidence.items() if score < 0.5]
        return OcrResult(
            output_pdf=output_pdf,
            processed_pages=sorted(set(pages)),
            page_confidence=confidence,
            warnings=[f"OCR confidence below 0.50 on pages: {low_confidence_pages}"]
            if low_confidence_pages
            else [],
        )

    def _process_with_tesseract(
        self,
        source: Path,
        pages: list[int],
        output_pdf: Path,
        context: ExtractionContext,
    ) -> OcrResult:
        pdftoppm = context.settings.resolved_binary("pdftoppm_binary", "pdftoppm")
        tesseract = context.settings.resolved_binary("tesseract_binary", "tesseract")
        if not pdftoppm or not tesseract:
            raise RuntimeError("selective PDF OCR requires OCRmyPDF or both pdftoppm and Tesseract")

        selected_pages = sorted(set(pages))
        with tempfile.TemporaryDirectory(prefix="proofcounsel-tesseract-pdf-") as temporary:
            temporary_path = Path(temporary)
            ocr_pages: dict[int, Path] = {}
            for page in selected_pages:
                image_base = temporary_path / f"page-{page}"
                run_sandboxed(
                    [
                        pdftoppm,
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-r",
                        "200",
                        "-png",
                        "-singlefile",
                        str(source.resolve()),
                        str(image_base),
                    ],
                    cwd=temporary_path,
                    profile=context.sandbox_profile,
                )
                image_path = image_base.with_suffix(".png")
                output_base = temporary_path / f"ocr-{page}"
                run_sandboxed(
                    [
                        tesseract,
                        str(image_path),
                        str(output_base),
                        "-l",
                        context.settings.ocr_languages,
                        "pdf",
                    ],
                    cwd=temporary_path,
                    profile=context.sandbox_profile,
                )
                ocr_page = output_base.with_suffix(".pdf")
                if not ocr_page.is_file() or ocr_page.stat().st_size == 0:
                    raise RuntimeError(f"Tesseract did not produce OCR output for page {page}")
                ocr_pages[page] = ocr_page

            reader = PdfReader(source)
            writer = PdfWriter()
            for page_number, native_page in enumerate(reader.pages, start=1):
                ocr_page_path = ocr_pages.get(page_number)
                if ocr_page_path is None:
                    writer.add_page(native_page)
                else:
                    writer.add_page(PdfReader(ocr_page_path).pages[0])
            with output_pdf.open("wb") as handle:
                writer.write(handle)

        confidence = _probe_page_confidence(output_pdf, selected_pages, context)
        low_confidence_pages = [page for page, score in confidence.items() if score < 0.5]
        return OcrResult(
            output_pdf=output_pdf,
            processed_pages=selected_pages,
            page_confidence=confidence,
            warnings=[f"OCR confidence below 0.50 on pages: {low_confidence_pages}"]
            if low_confidence_pages
            else [],
        )


def _probe_page_confidence(
    source: Path,
    pages: list[int],
    context: ExtractionContext,
) -> dict[int, float]:
    pdftoppm = context.settings.resolved_binary("pdftoppm_binary", "pdftoppm")
    tesseract = context.settings.resolved_binary("tesseract_binary", "tesseract")
    if not pdftoppm or not tesseract:
        return {page: 0.0 for page in pages}
    scores: dict[int, float] = {}
    with tempfile.TemporaryDirectory(prefix="proofcounsel-ocr-confidence-") as temporary:
        temporary_path = Path(temporary)
        for page in pages:
            image_base = temporary_path / f"page-{page}"
            run_sandboxed(
                [
                    pdftoppm,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-r",
                    "200",
                    "-png",
                    "-singlefile",
                    str(source.resolve()),
                    str(image_base),
                ],
                cwd=temporary_path,
                profile=context.sandbox_profile,
            )
            image_path = image_base.with_suffix(".png")
            result = run_sandboxed(
                [tesseract, str(image_path), "stdout", "-l", context.settings.ocr_languages, "tsv"],
                cwd=temporary_path,
                profile=context.sandbox_profile,
            )
            scores[page] = _mean_tesseract_confidence(result.stdout)
    return scores


def _mean_tesseract_confidence(tsv: str) -> float:
    weighted_total = 0.0
    total_weight = 0
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            continue
        if not text or confidence < 0:
            continue
        weight = max(len(text), 1)
        weighted_total += confidence * weight
        total_weight += weight
    return max(0.0, min(1.0, weighted_total / max(total_weight, 1) / 100.0))
