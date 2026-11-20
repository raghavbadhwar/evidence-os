"""Generate a deterministic synthetic corpus for scale and concurrency checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_scale_corpus(
    output_root: Path,
    *,
    document_count: int = 100,
    questions_per_document: int = 5,
) -> tuple[Path, Path]:
    if document_count <= 0 or questions_per_document <= 0:
        raise ValueError("document_count and questions_per_document must be positive")
    root = output_root.expanduser().resolve()
    documents_root = root / "documents"
    documents_root.mkdir(parents=True, exist_ok=True)
    manifest_documents: list[dict[str, str]] = []
    questions: list[dict[str, Any]] = []
    for document_number in range(document_count):
        document_id = f"scale-{document_number:04d}"
        relative_path = f"documents/{document_id}.txt"
        target_facts = [
            f"Target fact {question_number} for {document_id}: value-{document_number:04d}-{question_number:02d}."
            for question_number in range(questions_per_document)
        ]
        body = [
            f"# Synthetic document {document_id}",
            "Section 1 General Terms",
            f"This document belongs to deterministic scale slice {document_number:04d}.",
            "Section 2 Target Facts",
            *target_facts,
            "Section 3 Closing",
            "Synthetic evidence is for performance and failure testing, not legal accuracy claims.",
        ]
        (root / relative_path).write_text("\n".join(body) + "\n", encoding="utf-8")
        manifest_documents.append(
            {"id": document_id, "path": relative_path, "slice": "synthetic_text_scale"}
        )
        for question_number, expected in enumerate(target_facts):
            questions.append(
                {
                    "id": f"{document_id}-q{question_number:02d}",
                    "document_id": document_id,
                    "query": f"What is target fact {question_number} for {document_id}?",
                    "expected_texts": [expected],
                }
            )
    manifest_path = root / "corpus_manifest.json"
    questions_path = root / "questions.jsonl"
    manifest_path.write_text(
        json.dumps({"schema_version": "1.0", "documents": manifest_documents}, indent=2) + "\n",
        encoding="utf-8",
    )
    questions_path.write_text(
        "".join(json.dumps(question, sort_keys=True) + "\n" for question in questions),
        encoding="utf-8",
    )
    return manifest_path, questions_path
