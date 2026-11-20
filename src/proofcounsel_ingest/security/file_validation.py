"""Extension, MIME, signature, archive, and size validation before parsing."""

from __future__ import annotations

import mimetypes
import zipfile
from pathlib import Path
from typing import cast

import filetype  # type: ignore[import-untyped]
from charset_normalizer import from_bytes
from pydantic import BaseModel, Field

from proofcounsel_ingest.config import LimitsConfig

ALLOWED_MIME_BY_EXTENSION: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".tif": frozenset({"image/tiff"}),
    ".tiff": frozenset({"image/tiff"}),
    ".bmp": frozenset({"image/bmp", "image/x-ms-bmp"}),
    ".webp": frozenset({"image/webp"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".pptx": frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
    ".html": frozenset({"text/html"}),
    ".htm": frozenset({"text/html"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".markdown": frozenset({"text/markdown", "text/plain"}),
    ".csv": frozenset({"text/csv", "text/plain"}),
    ".tsv": frozenset({"text/tab-separated-values", "text/plain"}),
    ".json": frozenset({"application/json", "text/plain"}),
    ".jsonl": frozenset({"application/x-ndjson", "application/json", "text/plain"}),
    ".xml": frozenset({"application/xml", "text/xml", "text/plain"}),
    ".yaml": frozenset({"application/yaml", "text/yaml", "text/plain"}),
    ".yml": frozenset({"application/yaml", "text/yaml", "text/plain"}),
    ".rtf": frozenset({"application/rtf"}),
    ".doc": frozenset({"application/x-ole-storage"}),
    ".xls": frozenset({"application/x-ole-storage"}),
    ".ppt": frozenset({"application/x-ole-storage"}),
    ".odt": frozenset({"application/vnd.oasis.opendocument.text"}),
    ".ods": frozenset({"application/vnd.oasis.opendocument.spreadsheet"}),
    ".odp": frozenset({"application/vnd.oasis.opendocument.presentation"}),
}


class UploadValidation(BaseModel):
    accepted: bool
    extension: str
    detected_mime: str
    byte_size: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, int | str | bool | float] = Field(default_factory=dict)


def validate_upload(path: Path, limits: LimitsConfig) -> UploadValidation:
    resolved = path.expanduser().resolve(strict=True)
    extension = resolved.suffix.lower()
    reasons: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, int | str | bool | float] = {}

    if not resolved.is_file():
        return UploadValidation(
            accepted=False,
            extension=extension,
            detected_mime="application/octet-stream",
            byte_size=0,
            reasons=["source is not a regular file"],
        )
    byte_size = resolved.stat().st_size
    if extension not in ALLOWED_MIME_BY_EXTENSION:
        reasons.append(f"extension is not allowlisted: {extension or '<none>'}")
    if byte_size <= 0:
        reasons.append("file is empty")
    if byte_size > limits.max_file_bytes:
        reasons.append(f"file exceeds max_file_bytes={limits.max_file_bytes}")

    detected_mime = detect_mime(resolved)
    allowed = ALLOWED_MIME_BY_EXTENSION.get(extension, frozenset())
    if allowed and detected_mime not in allowed:
        reasons.append(f"extension/signature mismatch: {extension} does not allow {detected_mime}")

    if detected_mime.startswith("image/"):
        _validate_image(resolved, limits, reasons, metadata)
    if detected_mime == "application/pdf":
        _validate_pdf(resolved, limits, reasons, metadata)
    if detected_mime.startswith(("application/vnd.openxmlformats-officedocument", "application/vnd.oasis.opendocument")):
        _validate_ooxml_archive(resolved, limits, reasons, metadata)

    return UploadValidation(
        accepted=not reasons,
        extension=extension,
        detected_mime=detected_mime,
        byte_size=byte_size,
        reasons=reasons,
        warnings=warnings,
        metadata=metadata,
    )


def detect_mime(path: Path) -> str:
    header = path.read_bytes()[:8192]
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"{\\rtf"):
        return "application/rtf"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"
    if header.startswith(b"PK\x03\x04"):
        document_mime = _detect_zip_document(path)
        return document_mime or "application/zip"
    guessed = filetype.guess_mime(header)
    if guessed:
        return cast(str, guessed)

    lowered = header.lstrip().lower()
    if lowered.startswith(b"<!doctype html") or b"<html" in lowered[:2048]:
        return "text/html"
    if path.suffix.lower() == ".json":
        return "application/json"
    if path.suffix.lower() == ".jsonl":
        return "application/x-ndjson"
    if path.suffix.lower() == ".csv":
        return "text/csv"
    if path.suffix.lower() == ".tsv":
        return "text/tab-separated-values"
    if path.suffix.lower() in {".yaml", ".yml"}:
        return "application/yaml"
    if path.suffix.lower() == ".xml" and lowered.startswith((b"<?xml", b"<")):
        return "application/xml"
    if from_bytes(header).best() is not None:
        guessed_by_extension, _ = mimetypes.guess_type(path.name)
        if guessed_by_extension in {"text/markdown", "text/plain"}:
            return guessed_by_extension
        return "text/plain"
    return "application/octet-stream"


def _detect_zip_document(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                mimetype = archive.read("mimetype").decode("ascii", errors="strict").strip()
                if mimetype.startswith("application/vnd.oasis.opendocument"):
                    return mimetype
    except (OSError, UnicodeError, zipfile.BadZipFile):
        return None
    if "word/document.xml" in names:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "xl/workbook.xml" in names:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "ppt/presentation.xml" in names:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return None


def _validate_ooxml_archive(
    path: Path,
    limits: LimitsConfig,
    reasons: list[str],
    metadata: dict[str, int | str | bool | float],
) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            total_uncompressed = sum(entry.file_size for entry in entries)
            metadata["archive_entries"] = len(entries)
            metadata["archive_uncompressed_bytes"] = total_uncompressed
            if len(entries) > limits.max_archive_entries:
                reasons.append(f"archive exceeds max_archive_entries={limits.max_archive_entries}")
            if total_uncompressed > limits.max_uncompressed_bytes:
                reasons.append(f"archive exceeds max_uncompressed_bytes={limits.max_uncompressed_bytes}")
            for entry in entries:
                member = Path(entry.filename)
                if member.is_absolute() or ".." in member.parts:
                    reasons.append(f"archive contains unsafe path: {entry.filename}")
                    break
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        reasons.append(f"invalid OOXML archive: {exc}")


def _validate_image(
    path: Path,
    limits: LimitsConfig,
    reasons: list[str],
    metadata: dict[str, int | str | bool | float],
) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            width, height = image.size
            pixels = width * height
            metadata["image_width"] = width
            metadata["image_height"] = height
            metadata["image_pixels"] = pixels
            if pixels > limits.max_image_pixels:
                reasons.append(f"image exceeds max_image_pixels={limits.max_image_pixels}")
            image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError) as exc:
        reasons.append(f"invalid or unsafe image: {exc}")


def _validate_pdf(
    path: Path,
    limits: LimitsConfig,
    reasons: list[str],
    metadata: dict[str, int | str | bool | float],
) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        page_count = len(reader.pages)
        metadata["pdf_pages"] = page_count
        if page_count > limits.max_pages:
            reasons.append(f"PDF exceeds max_pages={limits.max_pages}")
    except ImportError as exc:  # pragma: no cover - PDF dependency is optional for text-only installs
        reasons.append(f"PDF validation backend unavailable: {exc}")
    except Exception as exc:
        reasons.append(f"invalid PDF: {type(exc).__name__}: {exc}")
