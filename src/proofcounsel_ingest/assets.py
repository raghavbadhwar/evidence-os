"""Tiered page/image verification assets with immutable identifiers."""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
from pathlib import Path
from uuid import UUID, uuid5

from pydantic import BaseModel

from proofcounsel_ingest.artifacts import ArtifactWriter
from proofcounsel_ingest.extractors.base import ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import DocumentFormat
from proofcounsel_ingest.security.sandbox import SandboxExecutionError, run_sandboxed

PAGE_SUFFIX = re.compile(r"-(\d+)\.(?:jpg|png)$", re.IGNORECASE)


class AssetRecord(BaseModel):
    artifact_id: UUID
    page_number: int
    tier: str
    mime_type: str
    relative_path: str
    sha256: str
    byte_size: int


def persist_verification_assets(
    source: Path,
    extraction: ExtractionResult,
    context: ExtractionContext,
    artifacts: ArtifactWriter,
) -> list[AssetRecord]:
    if extraction.format == DocumentFormat.PDF:
        records = _persist_pdf_assets(source, extraction, context, artifacts)
    elif extraction.format == DocumentFormat.IMAGE:
        records = _persist_image_assets(source, extraction, artifacts)
    else:
        return []
    low_by_page = {record.page_number: record for record in records if record.tier == "low"}
    extraction.pages = [
        page.model_copy(
            update={
                "image_artifact_id": low_by_page[page.page_number].artifact_id
                if page.page_number in low_by_page
                else page.image_artifact_id
            }
        )
        for page in extraction.pages
    ]
    artifacts.write_json("assets/manifest.json", [record.model_dump(mode="json") for record in records])
    return records


def _persist_pdf_assets(
    source: Path,
    extraction: ExtractionResult,
    context: ExtractionContext,
    artifacts: ArtifactWriter,
) -> list[AssetRecord]:
    pdftocairo = context.settings.resolved_binary("pdftocairo_binary", "pdftocairo")
    pdftoppm = context.settings.resolved_binary("pdftoppm_binary", "pdftoppm")
    renderer = pdftocairo or pdftoppm
    if not renderer:
        extraction.warnings.append("no PDF renderer available; verification images were not generated")
        return []
    document_id = extraction.pages[0].document_id if extraction.pages else UUID(int=0)
    records: list[AssetRecord] = []
    try:
        with tempfile.TemporaryDirectory(prefix="proofcounsel-assets-") as temporary:
            temporary_path = Path(temporary)
            low_prefix = temporary_path / "low"
            run_sandboxed(
                [
                    renderer,
                    "-jpeg",
                    "-jpegopt",
                    "quality=65",
                    "-scale-to",
                    "900",
                    str(source.resolve()),
                    str(low_prefix),
                ],
                cwd=temporary_path,
                profile=context.sandbox_profile,
            )
            for image_path in sorted(temporary_path.glob("low-*.jpg")):
                page_number = _page_number(image_path)
                records.append(
                    _write_asset(
                        artifacts,
                        document_id,
                        page_number,
                        "low",
                        "image/jpeg",
                        f"assets/pages/{page_number:05d}-low.jpg",
                        image_path.read_bytes(),
                    )
                )

            high_pages = sorted(
                set(extraction.ocr_required_units)
                | {int(page) for page in extraction.metadata.get("ocr_processed_pages", [])}
            )
            for page_number in high_pages:
                high_prefix = temporary_path / f"high-{page_number}"
                run_sandboxed(
                    [
                        renderer,
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        "-r",
                        "200",
                        "-png",
                        "-singlefile",
                        str(source.resolve()),
                        str(high_prefix),
                    ],
                    cwd=temporary_path,
                    profile=context.sandbox_profile,
                )
                high_path = high_prefix.with_suffix(".png")
                records.append(
                    _write_asset(
                        artifacts,
                        document_id,
                        page_number,
                        "high",
                        "image/png",
                        f"assets/pages/{page_number:05d}-high.png",
                        high_path.read_bytes(),
                    )
                )
    except (OSError, SandboxExecutionError) as exc:
        extraction.warnings.append(f"verification image generation failed: {type(exc).__name__}: {exc}")
    if len([record for record in records if record.tier == "low"]) != extraction.expected_units:
        extraction.warnings.append("verification image coverage is incomplete")
    return records


def _persist_image_assets(
    source: Path,
    extraction: ExtractionResult,
    artifacts: ArtifactWriter,
) -> list[AssetRecord]:
    from PIL import Image, ImageOps

    if not extraction.pages:
        return []
    document_id = extraction.pages[0].document_id
    with Image.open(source) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        high_buffer = io.BytesIO()
        normalized.save(high_buffer, format="PNG")
        thumbnail = normalized.copy()
        thumbnail.thumbnail((900, 900))
        low_buffer = io.BytesIO()
        thumbnail.save(low_buffer, format="JPEG", quality=70, optimize=True)
    return [
        _write_asset(
            artifacts,
            document_id,
            1,
            "low",
            "image/jpeg",
            "assets/pages/00001-low.jpg",
            low_buffer.getvalue(),
        ),
        _write_asset(
            artifacts,
            document_id,
            1,
            "high",
            "image/png",
            "assets/pages/00001-high.png",
            high_buffer.getvalue(),
        ),
    ]


def _write_asset(
    artifacts: ArtifactWriter,
    document_id: UUID,
    page_number: int,
    tier: str,
    mime_type: str,
    relative_path: str,
    payload: bytes,
) -> AssetRecord:
    artifacts.write_bytes(relative_path, payload)
    return AssetRecord(
        artifact_id=uuid5(document_id, relative_path),
        page_number=page_number,
        tier=tier,
        mime_type=mime_type,
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )


def _page_number(path: Path) -> int:
    match = PAGE_SUFFIX.search(path.name)
    if not match:
        raise ValueError(f"could not determine rendered page number from {path.name}")
    return int(match.group(1))
