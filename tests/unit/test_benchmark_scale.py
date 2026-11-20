from __future__ import annotations

import json
from pathlib import Path

from benchmarks.scale_generator import generate_scale_corpus


def test_scale_generator_emits_requested_documents_and_gold_questions(tmp_path: Path) -> None:
    manifest_path, questions_path = generate_scale_corpus(tmp_path, document_count=4, questions_per_document=3)
    manifest = json.loads(manifest_path.read_text())
    questions = [json.loads(line) for line in questions_path.read_text().splitlines()]
    assert len(manifest["documents"]) == 4
    assert len(questions) == 12
    assert all(question["expected_texts"] for question in questions)
    assert all((tmp_path / document["path"]).is_file() for document in manifest["documents"])
