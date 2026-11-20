from __future__ import annotations

import json
from pathlib import Path

from benchmarks.corpus_contract import validate_corpus
from benchmarks.legal_corpus_workflow import adjudicate_reviews, bootstrap_candidate_corpus


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _approve_packet(path: Path, reviewer_id: str, *, disagree: bool = False) -> None:
    rows = _read_jsonl(path)
    for index, row in enumerate(rows):
        text = f"Evidence {index + 1}"
        row.update(
            {
                "query": f"What is evidence {index + 1}?" + (" changed" if disagree else ""),
                "expected_labels": [
                    {"answer_text": text, "locator": {"format": "text", "line_start": 1}}
                ],
                "evaluation_mode": "model" if index == 0 else "deterministic",
                "repeats": 5 if index == 0 else 1,
                "reviewer_id": reviewer_id,
                "reviewer_role": "legal",
                "status": "approved",
                "reviewed_at": "2026-07-13T01:00:00Z",
            }
        )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_bootstrap_hashes_deduplicates_and_family_splits_sources(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "contract-v1.txt").write_text("Evidence 1", encoding="utf-8")
    (source / "contract-v2.txt").write_text("Evidence 2", encoding="utf-8")
    (source / "duplicate.txt").write_text("Evidence 1", encoding="utf-8")
    (source / "adversarial-injection.html").write_text("<p>Evidence 3</p>", encoding="utf-8")

    result = bootstrap_candidate_corpus(source, tmp_path / "candidate", question_slots_per_document=2)
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    packet = _read_jsonl(Path(result["reviewer_a_path"]))

    assert result["document_count"] == 3
    assert result["question_slot_count"] == 6
    assert result["duplicate_paths"] == ["duplicate.txt"]
    versions = [document for document in manifest["documents"] if document["path"].startswith("contract-v")]
    assert len({document["source_family"] for document in versions}) == 1
    assert len({document["split"] for document in versions}) == 1
    assert all(len(document["source_sha256"]) == 64 for document in manifest["documents"])
    assert any(document["adversarial"] for document in manifest["documents"])
    assert all(row["query"] == "" and row["expected_labels"] == [] for row in packet)


def test_two_independent_exact_reviews_emit_a_contract_valid_gold_corpus(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "adversarial-contract.txt").write_text("Evidence 1", encoding="utf-8")
    (source / "ordinary-contract.txt").write_text("Evidence 2", encoding="utf-8")
    candidate = bootstrap_candidate_corpus(
        source,
        tmp_path / "candidate",
        question_slots_per_document=1,
    )
    reviewer_a = Path(candidate["reviewer_a_path"])
    reviewer_b = Path(candidate["reviewer_b_path"])
    _approve_packet(reviewer_a, "legal-reviewer-a")
    _approve_packet(reviewer_b, "legal-reviewer-b")
    output_manifest = tmp_path / "gold" / "corpus_manifest.json"
    output_questions = tmp_path / "gold" / "questions.jsonl"

    result = adjudicate_reviews(
        Path(candidate["manifest_path"]),
        reviewer_a,
        reviewer_b,
        source_root=source,
        output_manifest_path=output_manifest,
        output_questions_path=output_questions,
        adjudicator_id="legal-lead",
        reviewed_at="2026-07-13T02:00:00Z",
    )
    validation = validate_corpus(
        output_manifest,
        output_questions,
        source_root=source,
        min_documents=2,
        min_questions=2,
        max_questions=2,
    )

    assert result["passed"]
    assert result["agreement_count"] == 2
    assert validation["passed"]


def test_review_disagreement_fails_without_publishing_gold_outputs(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "adversarial-contract.txt").write_text("Evidence 1", encoding="utf-8")
    candidate = bootstrap_candidate_corpus(source, tmp_path / "candidate", question_slots_per_document=1)
    reviewer_a = Path(candidate["reviewer_a_path"])
    reviewer_b = Path(candidate["reviewer_b_path"])
    _approve_packet(reviewer_a, "legal-reviewer-a")
    _approve_packet(reviewer_b, "legal-reviewer-b", disagree=True)
    output_manifest = tmp_path / "gold" / "corpus_manifest.json"
    output_questions = tmp_path / "gold" / "questions.jsonl"

    result = adjudicate_reviews(
        Path(candidate["manifest_path"]),
        reviewer_a,
        reviewer_b,
        source_root=source,
        output_manifest_path=output_manifest,
        output_questions_path=output_questions,
        adjudicator_id="legal-lead",
        reviewed_at="2026-07-13T02:00:00Z",
    )

    assert not result["passed"]
    assert result["disagreement_slots"]
    assert not output_manifest.exists()
    assert not output_questions.exists()


def test_legal_lead_can_resolve_a_recorded_disagreement(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "adversarial-contract.txt").write_text("Evidence 1", encoding="utf-8")
    candidate = bootstrap_candidate_corpus(source, tmp_path / "candidate", question_slots_per_document=1)
    reviewer_a = Path(candidate["reviewer_a_path"])
    reviewer_b = Path(candidate["reviewer_b_path"])
    _approve_packet(reviewer_a, "legal-reviewer-a")
    _approve_packet(reviewer_b, "legal-reviewer-b", disagree=True)
    resolution = tmp_path / "resolution.jsonl"
    resolution_rows = _read_jsonl(reviewer_a)
    resolution_rows[0]["reviewer_id"] = "legal-lead"
    resolution.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in resolution_rows),
        encoding="utf-8",
    )
    output_manifest = tmp_path / "gold" / "corpus_manifest.json"
    output_questions = tmp_path / "gold" / "questions.jsonl"

    result = adjudicate_reviews(
        Path(candidate["manifest_path"]),
        reviewer_a,
        reviewer_b,
        source_root=source,
        output_manifest_path=output_manifest,
        output_questions_path=output_questions,
        adjudicator_id="legal-lead",
        reviewed_at="2026-07-13T02:00:00Z",
        resolution_path=resolution,
    )
    final_question = _read_jsonl(output_questions)[0]

    assert result["passed"]
    assert result["adjudicated_slots"]
    assert final_question["review"]["agreement"] == "adjudicated"
    assert final_question["review"]["independent_reviewer_ids"] == [
        "legal-reviewer-a",
        "legal-reviewer-b",
    ]


def test_same_reviewer_identity_cannot_fill_both_blinded_packets(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    (source / "adversarial-contract.txt").write_text("Evidence 1", encoding="utf-8")
    candidate = bootstrap_candidate_corpus(source, tmp_path / "candidate", question_slots_per_document=1)
    reviewer_a = Path(candidate["reviewer_a_path"])
    reviewer_b = Path(candidate["reviewer_b_path"])
    _approve_packet(reviewer_a, "same-legal-reviewer")
    _approve_packet(reviewer_b, "same-legal-reviewer")

    result = adjudicate_reviews(
        Path(candidate["manifest_path"]),
        reviewer_a,
        reviewer_b,
        source_root=source,
        output_manifest_path=tmp_path / "gold" / "corpus_manifest.json",
        output_questions_path=tmp_path / "gold" / "questions.jsonl",
        adjudicator_id="legal-lead",
        reviewed_at="2026-07-13T02:00:00Z",
    )

    assert not result["passed"]
    assert any("two independent reviewer IDs" in error for error in result["errors"])


def test_source_tampering_blocks_adjudication_publication(tmp_path: Path) -> None:
    source = tmp_path / "sources"
    source.mkdir()
    source_path = source / "adversarial-contract.txt"
    source_path.write_text("Evidence 1", encoding="utf-8")
    candidate = bootstrap_candidate_corpus(source, tmp_path / "candidate", question_slots_per_document=1)
    reviewer_a = Path(candidate["reviewer_a_path"])
    reviewer_b = Path(candidate["reviewer_b_path"])
    _approve_packet(reviewer_a, "legal-reviewer-a")
    _approve_packet(reviewer_b, "legal-reviewer-b")
    source_path.write_text("Changed after review", encoding="utf-8")
    output_manifest = tmp_path / "gold" / "corpus_manifest.json"

    result = adjudicate_reviews(
        Path(candidate["manifest_path"]),
        reviewer_a,
        reviewer_b,
        source_root=source,
        output_manifest_path=output_manifest,
        output_questions_path=tmp_path / "gold" / "questions.jsonl",
        adjudicator_id="legal-lead",
        reviewed_at="2026-07-13T02:00:00Z",
    )

    assert not result["passed"]
    assert any("source hash does not match" in error for error in result["errors"])
    assert not output_manifest.exists()
