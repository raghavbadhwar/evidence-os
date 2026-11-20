"""Bootstrap and adjudicate a leakage-safe, counsel-reviewed legal benchmark corpus."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SLICE_BY_EXTENSION = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".txt": "text",
    ".md": "markdown",
    ".csv": "csv",
    ".json": "json",
    ".xml": "xml",
    ".rtf": "rtf",
    ".odt": "odt",
    ".ods": "ods",
    ".odp": "odp",
}


def bootstrap_candidate_corpus(
    source_root: Path,
    output_root: Path,
    *,
    question_slots_per_document: int = 5,
    corpus_version: str = "candidate-v1",
    adversarial_patterns: tuple[str, ...] = ("*adversarial*", "*injection*", "*malicious*"),
) -> dict[str, Any]:
    if question_slots_per_document <= 0:
        raise ValueError("question_slots_per_document must be positive")
    source = source_root.expanduser().resolve(strict=True)
    output = output_root.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    duplicate_paths: list[str] = []
    seen_hashes: set[str] = set()
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        slice_name = SLICE_BY_EXTENSION.get(path.suffix.casefold())
        if slice_name is None:
            continue
        relative_path = path.relative_to(source).as_posix()
        source_sha256 = _sha256_file(path)
        if source_sha256 in seen_hashes:
            duplicate_paths.append(relative_path)
            continue
        seen_hashes.add(source_sha256)
        document_id = f"legal-{source_sha256[:20]}"
        source_family = _source_family(relative_path)
        document = {
            "id": document_id,
            "path": relative_path,
            "source_sha256": source_sha256,
            "source_family": source_family,
            "slice": slice_name,
            "split": _split_for_family(source_family),
            "adversarial": _matches_any(relative_path, adversarial_patterns),
            "review": {"status": "pending", "reviewer_role": "legal"},
        }
        documents.append(document)
        for slot_number in range(1, question_slots_per_document + 1):
            slots.append(
                {
                    "slot_id": f"{document_id}-slot-{slot_number:02d}",
                    "id": f"{document_id}-slot-{slot_number:02d}",
                    "document_id": document_id,
                    "source_sha256": source_sha256,
                    "source_path": relative_path,
                    "slice": slice_name,
                    "split": document["split"],
                    "adversarial": document["adversarial"],
                    "query": "",
                    "expected_labels": [],
                    "evaluation_mode": "unassigned",
                    "repeats": 0,
                    "reviewer_id": "",
                    "reviewer_role": "legal",
                    "status": "pending",
                    "reviewed_at": "",
                }
            )
    manifest = {
        "schema_version": "1.0",
        "corpus_version": corpus_version,
        "concurrency_repetitions": 3,
        "question_slots_per_document": question_slots_per_document,
        "documents": documents,
    }
    manifest_path = output / "corpus_manifest.candidate.json"
    candidate_questions_path = output / "questions.candidate.jsonl"
    reviewer_a_path = output / "review_packet_a.jsonl"
    reviewer_b_path = output / "review_packet_b.jsonl"
    _write_json(manifest_path, manifest)
    _write_jsonl(candidate_questions_path, slots)
    _write_jsonl(reviewer_a_path, ({**slot, "reviewer_slot": "A"} for slot in slots))
    _write_jsonl(reviewer_b_path, ({**slot, "reviewer_slot": "B"} for slot in slots))
    return {
        "schema_version": "1.0",
        "document_count": len(documents),
        "question_slot_count": len(slots),
        "duplicate_paths": duplicate_paths,
        "manifest_path": str(manifest_path),
        "candidate_questions_path": str(candidate_questions_path),
        "reviewer_a_path": str(reviewer_a_path),
        "reviewer_b_path": str(reviewer_b_path),
    }


def adjudicate_reviews(
    manifest_path: Path,
    reviewer_a_path: Path,
    reviewer_b_path: Path,
    *,
    source_root: Path,
    output_manifest_path: Path,
    output_questions_path: Path,
    adjudicator_id: str,
    reviewed_at: str,
    resolution_path: Path | None = None,
) -> dict[str, Any]:
    if not adjudicator_id.strip() or not reviewed_at.strip():
        raise ValueError("adjudicator_id and reviewed_at are required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("manifest.documents must be a list")
    document_by_id = {
        document["id"]: document for document in documents if isinstance(document, dict) and "id" in document
    }
    source_errors = _manifest_source_errors(documents, source_root)
    if source_errors:
        return {
            "schema_version": "1.0",
            "passed": False,
            "errors": source_errors,
            "agreement_count": 0,
            "disagreement_slots": [],
        }
    expected_slots = {
        f"{document_id}-slot-{slot_number:02d}"
        for document_id in document_by_id
        for slot_number in range(1, int(manifest.get("question_slots_per_document", 0)) + 1)
    }
    reviewer_a = _review_rows(reviewer_a_path)
    reviewer_b = _review_rows(reviewer_b_path)
    resolutions = _review_rows(resolution_path) if resolution_path is not None else {}
    errors: list[str] = []
    if set(reviewer_a) != expected_slots:
        errors.append("reviewer A slot set does not match the candidate manifest")
    if set(reviewer_b) != expected_slots:
        errors.append("reviewer B slot set does not match the candidate manifest")
    agreed_questions: list[dict[str, Any]] = []
    disagreement_slots: list[str] = []
    adjudicated_slots: list[str] = []
    reviewer_ids_by_document: dict[str, set[str]] = {document_id: set() for document_id in document_by_id}
    adjudicated_documents: set[str] = set()
    for slot_id in sorted(expected_slots & set(reviewer_a) & set(reviewer_b)):
        left = reviewer_a[slot_id]
        right = reviewer_b[slot_id]
        left_errors = _review_row_errors(left, slot_id, "A", document_by_id)
        right_errors = _review_row_errors(right, slot_id, "B", document_by_id)
        errors.extend(left_errors)
        errors.extend(right_errors)
        left_reviewer = str(left.get("reviewer_id", "")).strip()
        right_reviewer = str(right.get("reviewer_id", "")).strip()
        if left_reviewer and left_reviewer == right_reviewer:
            errors.append(f"{slot_id} must be reviewed by two independent reviewer IDs")
            left_errors.append("reviewers are not independent")
        selected = left
        agreement = "exact"
        if _review_payload(left) != _review_payload(right):
            disagreement_slots.append(slot_id)
            resolution = resolutions.get(slot_id)
            if resolution is None:
                continue
            resolution_errors = _review_row_errors(
                resolution,
                slot_id,
                "adjudicator",
                document_by_id,
            )
            if str(resolution.get("reviewer_id", "")).strip() != adjudicator_id:
                resolution_errors.append(f"adjudicator {slot_id} reviewer_id does not match adjudicator_id")
            errors.extend(resolution_errors)
            if resolution_errors:
                continue
            selected = resolution
            agreement = "adjudicated"
            adjudicated_slots.append(slot_id)
        if left_errors or right_errors:
            continue
        document_id = str(selected["document_id"])
        reviewer_ids_by_document[document_id].update((left_reviewer, right_reviewer))
        if agreement == "adjudicated":
            adjudicated_documents.add(document_id)
        labels = _canonical_labels(selected["expected_labels"])
        agreed_questions.append(
            {
                "id": slot_id,
                "document_id": document_id,
                "query": _normalize_text(str(selected["query"])),
                "expected_texts": [label["answer_text"] for label in labels],
                "expected_labels": labels,
                "evaluation_mode": selected["evaluation_mode"],
                "repeats": int(selected["repeats"]),
                "review": {
                    "status": "approved",
                    "reviewer_role": "legal",
                    "reviewer_id": adjudicator_id,
                    "reviewed_at": reviewed_at,
                    "independent_reviewer_ids": sorted((left_reviewer, right_reviewer)),
                    "agreement": agreement,
                },
            }
        )
    unresolved_disagreements = sorted(set(disagreement_slots) - set(adjudicated_slots))
    if unresolved_disagreements:
        errors.append(
            f"review disagreements require adjudication: {', '.join(unresolved_disagreements)}"
        )
    unexpected_resolutions = sorted(set(resolutions) - set(disagreement_slots))
    if unexpected_resolutions:
        errors.append(f"resolution rows do not correspond to disagreements: {unexpected_resolutions}")
    if errors:
        return {
            "schema_version": "1.0",
            "passed": False,
            "errors": errors,
            "agreement_count": len(agreed_questions),
            "disagreement_slots": disagreement_slots,
            "adjudicated_slots": adjudicated_slots,
        }
    final_documents = []
    for document in documents:
        document_id = document["id"]
        reviewer_ids = sorted(reviewer_ids_by_document[document_id])
        final_documents.append(
            {
                **document,
                "review": {
                    "status": "approved",
                    "reviewer_role": "legal",
                    "reviewer_id": adjudicator_id,
                    "reviewed_at": reviewed_at,
                    "independent_reviewer_ids": reviewer_ids,
                    "agreement": "adjudicated" if document_id in adjudicated_documents else "exact",
                },
            }
        )
    final_manifest = {**manifest, "documents": final_documents}
    _write_json(output_manifest_path, final_manifest)
    _write_jsonl(output_questions_path, agreed_questions)
    return {
        "schema_version": "1.0",
        "passed": True,
        "errors": [],
        "agreement_count": len(agreed_questions),
        "disagreement_slots": disagreement_slots,
        "adjudicated_slots": adjudicated_slots,
        "manifest_path": str(output_manifest_path),
        "questions_path": str(output_questions_path),
    }


def _review_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        slot_id = row.get("slot_id") if isinstance(row, dict) else None
        if not isinstance(slot_id, str) or not slot_id:
            raise ValueError(f"{path}:{line_number} has no slot_id")
        if slot_id in rows:
            raise ValueError(f"duplicate slot_id in {path}: {slot_id}")
        rows[slot_id] = row
    return rows


def _manifest_source_errors(documents: list[Any], source_root: Path) -> list[str]:
    root = source_root.expanduser().resolve(strict=True)
    errors: list[str] = []
    family_splits: dict[str, set[str]] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            errors.append(f"documents[{index}] must be an object")
            continue
        relative_path = document.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            errors.append(f"documents[{index}] has no source path")
            continue
        source_path = (root / relative_path).resolve()
        if not source_path.is_relative_to(root) or not source_path.is_file():
            errors.append(f"documents[{index}] source path is missing or escapes source_root")
            continue
        if _sha256_file(source_path) != document.get("source_sha256"):
            errors.append(f"documents[{index}] source hash does not match current bytes")
        source_family = document.get("source_family")
        split = document.get("split")
        if isinstance(source_family, str) and isinstance(split, str):
            family_splits.setdefault(source_family, set()).add(split)
    for source_family, splits in sorted(family_splits.items()):
        if len(splits) > 1:
            errors.append(f"source family {source_family!r} crosses splits: {sorted(splits)}")
    return errors


def _review_row_errors(
    row: dict[str, Any],
    slot_id: str,
    reviewer_slot: str,
    document_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    prefix = f"reviewer {reviewer_slot} {slot_id}"
    errors: list[str] = []
    document = document_by_id.get(str(row.get("document_id")))
    if document is None:
        errors.append(f"{prefix} references an unknown document")
        return errors
    if row.get("source_sha256") != document.get("source_sha256"):
        errors.append(f"{prefix} source hash does not match the manifest")
    if row.get("reviewer_role") != "legal" or row.get("status") != "approved":
        errors.append(f"{prefix} is not an approved legal review")
    if not str(row.get("reviewer_id", "")).strip() or not str(row.get("reviewed_at", "")).strip():
        errors.append(f"{prefix} has incomplete reviewer identity or timestamp")
    if not str(row.get("query", "")).strip():
        errors.append(f"{prefix} has an empty query")
    labels = row.get("expected_labels")
    if not _valid_review_labels(labels):
        errors.append(f"{prefix} has no exact answer/locator labels")
    mode = row.get("evaluation_mode")
    repeats = row.get("repeats")
    if mode == "model" and (not isinstance(repeats, int) or repeats < 5):
        errors.append(f"{prefix} requires at least five model repetitions")
    elif mode == "deterministic" and repeats != 1:
        errors.append(f"{prefix} requires one deterministic repetition")
    elif mode not in {"model", "deterministic"}:
        errors.append(f"{prefix} has an invalid evaluation mode")
    return errors


def _valid_review_labels(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    coordinate_keys = {
        "page_start",
        "line_start",
        "cell_range",
        "slide_number",
        "xpath",
        "part",
        "bbox",
    }
    for label in value:
        if not isinstance(label, dict) or not str(label.get("answer_text", "")).strip():
            return False
        locator = label.get("locator")
        if (
            not isinstance(locator, dict)
            or not str(locator.get("format", "")).strip()
            or not coordinate_keys.intersection(locator)
        ):
            return False
    return True


def _review_payload(row: dict[str, Any]) -> str:
    payload = {
        "document_id": row.get("document_id"),
        "source_sha256": row.get("source_sha256"),
        "query": _normalize_text(str(row.get("query", ""))),
        "expected_labels": _canonical_labels(row.get("expected_labels")),
        "evaluation_mode": row.get("evaluation_mode"),
        "repeats": row.get("repeats"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_labels(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    labels = [
        {
            "answer_text": _normalize_text(str(label.get("answer_text", ""))),
            "locator": label.get("locator"),
        }
        for label in value
        if isinstance(label, dict)
    ]
    return sorted(labels, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))


def _split_for_family(source_family: str) -> str:
    bucket = int(hashlib.sha256(source_family.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 70:
        return "train"
    if bucket < 85:
        return "dev"
    return "test"


def _source_family(relative_path: str) -> str:
    path = Path(relative_path)
    normalized_stem = re.sub(
        r"(?:[-_. ](?:v|ver|version|rev|copy))?[-_. ]?\d+$",
        "",
        path.stem.casefold(),
    )
    return f"{path.parent.as_posix().casefold()}::{normalized_stem}"


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(fnmatch.fnmatch(lowered, pattern.casefold()) for pattern in patterns)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--source-root", type=Path, required=True)
    bootstrap.add_argument("--output-root", type=Path, required=True)
    bootstrap.add_argument("--question-slots", type=int, default=5)
    bootstrap.add_argument("--corpus-version", default="candidate-v1")
    bootstrap.add_argument("--adversarial-pattern", action="append")
    adjudicate = subparsers.add_parser("adjudicate")
    adjudicate.add_argument("--manifest", type=Path, required=True)
    adjudicate.add_argument("--source-root", type=Path, required=True)
    adjudicate.add_argument("--reviewer-a", type=Path, required=True)
    adjudicate.add_argument("--reviewer-b", type=Path, required=True)
    adjudicate.add_argument("--resolution", type=Path)
    adjudicate.add_argument("--output-manifest", type=Path, required=True)
    adjudicate.add_argument("--output-questions", type=Path, required=True)
    adjudicate.add_argument("--output-report", type=Path, required=True)
    adjudicate.add_argument("--adjudicator-id", required=True)
    adjudicate.add_argument("--reviewed-at", required=True)
    args = parser.parse_args()
    if args.command == "bootstrap":
        patterns = tuple(args.adversarial_pattern or ("*adversarial*", "*injection*", "*malicious*"))
        result = bootstrap_candidate_corpus(
            args.source_root,
            args.output_root,
            question_slots_per_document=args.question_slots,
            corpus_version=args.corpus_version,
            adversarial_patterns=patterns,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["document_count"] else 1
    result = adjudicate_reviews(
        args.manifest,
        args.reviewer_a,
        args.reviewer_b,
        source_root=args.source_root,
        output_manifest_path=args.output_manifest,
        output_questions_path=args.output_questions,
        adjudicator_id=args.adjudicator_id,
        reviewed_at=args.reviewed_at,
        resolution_path=args.resolution,
    )
    _write_json(args.output_report, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
