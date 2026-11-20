from __future__ import annotations

from proofcounsel_ingest.extractors.base import ExtractorPort, UnsupportedFormatError

TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "application/x-ndjson",
        "application/xml",
        "text/xml",
        "application/yaml",
        "text/yaml",
    }
)


class ExtractorRouter:
    def __init__(self, fallback: ExtractorPort | None = None) -> None:
        self._fallback = fallback
        self._extractors: dict[str, ExtractorPort] = {}

    def for_mime(self, detected_mime: str) -> ExtractorPort:
        if detected_mime not in self._extractors:
            extractor = _lazy_extractor(detected_mime)
            if extractor is not None:
                self._extractors[detected_mime] = extractor
        extractor = self._extractors.get(detected_mime)
        if extractor is not None:
            return extractor
        if self._fallback is not None:
            return self._fallback
        raise UnsupportedFormatError(f"no extractor configured for MIME type {detected_mime}")

    def register(self, detected_mime: str, extractor: ExtractorPort) -> None:
        self._extractors[detected_mime] = extractor


def _lazy_extractor(detected_mime: str) -> ExtractorPort | None:
    if detected_mime in TEXT_MIMES:
        from proofcounsel_ingest.extractors.text import TextExtractor

        return TextExtractor()
    if detected_mime == "application/pdf":
        from proofcounsel_ingest.extractors.pdf_native import PdfNativeExtractor

        return PdfNativeExtractor()
    if detected_mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from proofcounsel_ingest.extractors.docx import DocxExtractor

        return DocxExtractor()
    if detected_mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        from proofcounsel_ingest.extractors.xlsx import XlsxExtractor

        return XlsxExtractor()
    if detected_mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        from proofcounsel_ingest.extractors.pptx import PptxExtractor

        return PptxExtractor()
    if detected_mime == "text/html":
        from proofcounsel_ingest.extractors.html import HtmlExtractor

        return HtmlExtractor()
    if detected_mime.startswith("image/"):
        from proofcounsel_ingest.extractors.image import ImageExtractor

        return ImageExtractor()
    return None
