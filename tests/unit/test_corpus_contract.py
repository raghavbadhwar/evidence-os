from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmarks.corpus_contract import validate_corpus


def _review() -> dict[str, object]:
    return {
        "status": "approved",
        "reviewer_role": "legal",
        "reviewer_id": "reviewer-opaque-1",
        "reviewed_at": "2026-07-13T00:00:00Z",
        "independent_reviewer_ids": ["legal-a", "legal-b"],
        "agreement": "exact",
    }


def test_corpus_contract_accepts_reviewed_labeled_mixed_evaluation(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Alpha evidence", encoding="utf-8")
    (tmp_path / "b.txt").write_text("Beta evidence", encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "concurrency_repetitions": 3,
        "documents": [
            {
                "id": "a",
                "path": "a.txt",
                "slice": "text",
                "split": "test",
                "source_family": "family-a",
                "source_sha256": hashlib.sha256(b"Alpha evidence").hexdigest(),
                "adversarial": True,
                "review": _review(),
            },
            {
                "id": "b",
                "path": "b.txt",
                "slice": "text",
                "split": "test",
                "source_family": "family-b",
                "source_sha256": hashlib.sha256(b"Beta evidence").hexdigest(),
                "adversarial": False,
                "review": _review(),
            },
        ],
    }
    questions = [
        {
            "id": "q1",
            "document_id": "a",
            "query": "What is alpha?",
            "expected_labels": [
                {
                    "answer_text": "Alpha evidence",
                    "locator": {"format": "text", "line_start": 1},
                }
            ],
            "evaluation_mode": "model",
            "repeats": 5,
            "review": _review(),
        },
        {
            "id": "q2",
            "document_id": "b",
            "query": "What is beta?",
            "expected_labels": [
                {
                    "answer_text": "Beta evidence",
                    "locator": {"format": "text", "line_start": 1},
                }
            ],
            "evaluation_mode": "deterministic",
            "repeats": 1,
            "review": _review(),
        },
    ]
    manifest_path = tmp_path / "corpus_manifest.json"
    questions_path = tmp_path / "questions.jsonl"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    questions_path.write_text(
        "".join(json.dumps(question) + "\n" for question in questions),
        encoding="utf-8",
    )

    result = validate_corpus(
        manifest_path,
        questions_path,
        source_root=tmp_path,
        min_documents=2,
        min_questions=2,
        max_questions=2,
    )

    assert result["passed"]
    assert result["errors"] == []


def test_corpus_contract_rejects_unreviewed_unlabeled_pilot(tmp_path: Path) -> None:
    (tmp_path / "pilot.txt").write_text("Pilot", encoding="utf-8")
    manifest_path = tmp_path / "corpus_manifest.json"
    questions_path = tmp_path / "questions.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": "pilot", "path": "pilot.txt", "slice": "text"}
                ]
            }
        ),
        encoding="utf-8",
    )
    questions_path.write_text(
        json.dumps(
            {
                "id": "q1",
                "document_id": "pilot",
                "query": "What is the answer?",
                "expected_texts": ["Pilot"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = validate_corpus(manifest_path, questions_path, source_root=tmp_path)

    assert not result["passed"]
    assert any("documents count" in error for error in result["errors"])
    assert any("approved legal review" in error for error in result["errors"])
    assert any("expected_labels" in error for error in result["errors"])
    assert any("model-involved" in error for error in result["errors"])


def test_corpus_contract_rejects_tampered_bytes_and_family_split_leakage(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("Beta", encoding="utf-8")
    manifest = {
        "concurrency_repetitions": 3,
        "documents": [
            {
                "id": "a",
                "path": "a.txt",
                "slice": "text",
                "split": "train",
                "source_family": "same-matter",
                "source_sha256": "0" * 64,
                "adversarial": True,
                "review": _review(),
            },
            {
                "id": "b",
                "path": "b.txt",
                "slice": "text",
                "split": "test",
                "source_family": "same-matter",
                "source_sha256": hashlib.sha256(b"Beta").hexdigest(),
                "adversarial": False,
                "review": _review(),
            },
        ],
    }
    labels = [{"answer_text": "Alpha", "locator": {"format": "text", "line_start": 1}}]
    questions = [
        {
            "id": "q1",
            "document_id": "a",
            "query": "Alpha?",
            "expected_labels": labels,
            "evaluation_mode": "model",
            "repeats": 5,
            "review": _review(),
        },
        {
            "id": "q2",
            "document_id": "b",
            "query": "Beta?",
            "expected_labels": [
                {"answer_text": "Beta", "locator": {"format": "text", "line_start": 1}}
            ],
            "evaluation_mode": "deterministic",
            "repeats": 1,
            "review": _review(),
        },
    ]
    manifest_path = tmp_path / "corpus_manifest.json"
    questions_path = tmp_path / "questions.jsonl"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    questions_path.write_text(
        "".join(json.dumps(question) + "\n" for question in questions), encoding="utf-8"
    )

    result = validate_corpus(
        manifest_path,
        questions_path,
        source_root=tmp_path,
        min_documents=2,
        min_questions=2,
        max_questions=2,
    )

    assert not result["passed"]
    assert any("does not match source bytes" in error for error in result["errors"])
    assert any("crosses leakage boundary" in error for error in result["errors"])
