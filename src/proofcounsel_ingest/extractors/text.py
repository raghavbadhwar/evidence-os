"""Bounded, structured extraction for text, Markdown, CSV, JSON, XML, and YAML."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from proofcounsel_ingest.extractors.base import EvidenceVariant, ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)

MAX_BLOCK_CHARS = 12_000


class TextExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        del context
        payload = source.read_bytes()
        match = from_bytes(payload).best()
        if match is None:
            raise ValueError("text encoding could not be determined")
        text = str(match).replace("\r\n", "\n").replace("\r", "\n")
        suffix = source.suffix.lower()
        if suffix in {".csv", ".tsv"}:
            raw_blocks = _csv_blocks(text, delimiter="\t" if suffix == ".tsv" else ",")
        elif suffix == ".json":
            raw_blocks = _json_blocks(text)
        elif suffix == ".jsonl":
            raw_blocks = _jsonl_blocks(text)
        elif suffix == ".xml":
            raw_blocks = _xml_blocks(text)
        else:
            raw_blocks = _paragraph_blocks(text)

        blocks = [
            ContentBlock(
                document_id=identity.document_id,
                ordinal=ordinal,
                block_type=block_type,
                text=value,
                language="und",
                confidence=1.0,
                locator=SourceLocator(
                    format=DocumentFormat.TEXT,
                    line_start=line_start,
                    line_end=line_end,
                ),
                text_sha256=_sha256(value),
                metadata={"untrusted_evidence": True, "queryable_evidence": True, **metadata},
            )
            for ordinal, (block_type, value, line_start, line_end, metadata) in enumerate(raw_blocks)
            if value.strip()
        ]
        page = PageRecord(
            document_id=identity.document_id,
            page_number=1,
            native_text_coverage=1.0 if blocks else 0.0,
            warnings=[] if blocks else ["no text content extracted"],
            quality_metrics={
                "locator_coverage": 1.0 if blocks else 0.0,
                "accepted": 1.0 if blocks else 0.0,
            },
        )
        normalized = "\n".join(block.text for block in blocks)
        return ExtractionResult(
            format=DocumentFormat.TEXT,
            blocks=blocks,
            pages=[page],
            expected_units=1,
            extracted_units=1 if blocks else 0,
            warnings=page.warnings,
            metadata={
                "encoding": match.encoding or "unknown",
                "quality_profile": "text",
                "extraction_method": f"structured_text_{suffix.lstrip('.') or 'plain'}",
            },
            evidence_variants=[
                EvidenceVariant(
                    unit=1,
                    method="structured_text",
                    text=normalized,
                    text_sha256=_sha256(normalized),
                    confidence=1.0 if blocks else 0.0,
                    selected=bool(blocks),
                )
            ],
        )


RawBlock = tuple[BlockType, str, int, int, dict[str, Any]]


def _paragraph_blocks(text: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    lines = text.splitlines()
    block_start: int | None = None
    buffer: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal block_start, buffer
        if block_start is None or not buffer:
            block_start = None
            buffer = []
            return
        value = "\n".join(buffer).strip()
        block_type = BlockType.HEADING if re.match(r"^#{1,6}\s+", value) else BlockType.PARAGRAPH
        heading_match = re.match(r"^(#{1,6})\s+", value)
        for chunk in _chunks(value):
            blocks.append(
                (
                    block_type,
                    chunk,
                    block_start,
                    end_line,
                    {"heading_level": len(heading_match.group(1)) if heading_match else None},
                )
            )
        block_start = None
        buffer = []

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            flush(line_number - 1)
            continue
        if block_start is None:
            block_start = line_number
        buffer.append(line)
    flush(max(len(lines), 1))
    return blocks


def _csv_blocks(text: str, *, delimiter: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    previous_line = 0
    headers: list[str] = []
    for row_number, row in enumerate(reader, start=1):
        start_line = previous_line + 1
        end_line = max(reader.line_num, start_line)
        previous_line = end_line
        if row_number == 1:
            headers = [value.strip() or f"column_{index}" for index, value in enumerate(row, start=1)]
            value = delimiter.join(row)
            block_type = BlockType.HEADING
        else:
            pairs = [
                f"{headers[index] if index < len(headers) else f'column_{index + 1}'}={cell}"
                for index, cell in enumerate(row)
            ]
            value = delimiter.join(pairs)
            block_type = BlockType.TABLE_ROW
        if value.strip():
            blocks.append((block_type, value, start_line, end_line, {"row": row_number}))
    return blocks


def _json_blocks(text: str) -> list[RawBlock]:
    payload = json.loads(text)
    lines = text.splitlines()
    blocks: list[RawBlock] = []
    if isinstance(payload, dict):
        items: Iterable[tuple[str, Any]] = payload.items()
    elif isinstance(payload, list):
        items = ((str(index), value) for index, value in enumerate(payload))
    else:
        items = (("value", payload),)
    for key, value in items:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        line = _find_json_key_line(lines, key)
        for chunk_index, chunk in enumerate(_chunks(serialized)):
            blocks.append(
                (
                    BlockType.PARAGRAPH,
                    f"{key}: {chunk}",
                    line,
                    line,
                    {"json_pointer": f"/{_escape_json_pointer(key)}", "chunk": chunk_index},
                )
            )
    return blocks


def _jsonl_blocks(text: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        for chunk_index, chunk in enumerate(_chunks(serialized)):
            blocks.append(
                (
                    BlockType.PARAGRAPH,
                    chunk,
                    line_number,
                    line_number,
                    {"jsonl_record": line_number, "chunk": chunk_index},
                )
            )
    return blocks


def _xml_blocks(text: str) -> list[RawBlock]:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    root = etree.fromstring(text.encode("utf-8"), parser=parser)
    blocks: list[RawBlock] = []
    tree = root.getroottree()
    for element in root.iter():
        if len(element) or not (element.text and element.text.strip()):
            continue
        value = element.text.strip()
        source_line = element.sourceline
        line = max(source_line if isinstance(source_line, int) else 1, 1)
        blocks.append(
            (
                BlockType.PARAGRAPH,
                value,
                line,
                line,
                {"xml_path": tree.getpath(element), "xml_tag": str(element.tag)},
            )
        )
    return blocks


def _chunks(value: str) -> list[str]:
    if len(value) <= MAX_BLOCK_CHARS:
        return [value]
    return [value[index : index + MAX_BLOCK_CHARS] for index in range(0, len(value), MAX_BLOCK_CHARS)]


def _find_json_key_line(lines: list[str], key: str) -> int:
    needle = json.dumps(key, ensure_ascii=False)
    for line_number, line in enumerate(lines, start=1):
        if needle in line:
            return line_number
    return 1


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
