"""Validate that a benchmark corpus meets the legal-gold release contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, TypeGuard


def validate_corpus(
    manifest_path: Path,
    questions_path: Path,
    *,
    source_root: Path | None = None,
    min_documents: int = 100,
    min_questions: int = 500,
    max_questions: int = 1000,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    root = (source_root or manifest_path.parent.parent).expanduser().resolve()
    documents = manifest.get("documents")
    errors: list[str] = []
    if not isinstance(documents, list):
        documents = []
        errors.append("manifest.documents must be a list")
    _check_count("documents", len(documents), min_documents, None, errors)
    _check_count("questions", len(questions), min_questions, max_questions, errors)
    concurrency_repetitions = manifest.get("concurrency_repetitions")
    if not isinstance(concurrency_repetitions, int) or concurrency_repetitions < 3:
        errors.append("manifest.concurrency_repetitions must be at least 3")

    document_ids: set[str] = set()
    slices: Counter[str] = Counter()
    adversarial_documents = 0
    approved_documents = 0
    source_hashes: set[str] = set()
    family_splits: dict[str, set[str]] = {}
    for index, document in enumerate(documents):
        prefix = f"documents[{index}]"
        if not isinstance(document, dict):
            errors.append(f"{prefix} must be an object")
            continue
        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
        elif document_id in document_ids:
            errors.append(f"duplicate document id: {document_id}")
        else:
            document_ids.add(document_id)
        slice_name = document.get("slice")
        if not isinstance(slice_name, str) or not slice_name.strip():
            errors.append(f"{prefix}.slice must be a non-empty format label")
        else:
            slices[slice_name] += 1
        if document.get("adversarial") is True:
            adversarial_documents += 1
        split = document.get("split")
        if split not in {"train", "dev", "test"}:
            errors.append(f"{prefix}.split must be train, dev, or test")
        source_family = document.get("source_family")
        if not isinstance(source_family, str) or not source_family.strip():
            errors.append(f"{prefix}.source_family must be non-empty")
        elif split in {"train", "dev", "test"}:
            family_splits.setdefault(source_family, set()).add(split)
        if _approved_legal_review(document.get("review")):
            approved_documents += 1
        else:
            errors.append(f"{prefix}.review must contain an approved legal review")
        source_path = _validate_source_path(document.get("path"), root, prefix, errors)
        source_sha256 = document.get("source_sha256")
        if not _is_sha256(source_sha256):
            errors.append(f"{prefix}.source_sha256 must be a lowercase SHA-256 digest")
        elif source_sha256 in source_hashes:
            errors.append(f"duplicate source_sha256: {source_sha256}")
        else:
            source_hashes.add(source_sha256)
            if source_path is not None and _sha256_file(source_path) != source_sha256:
                errors.append(f"{prefix}.source_sha256 does not match source bytes")

    if adversarial_documents == 0:
        errors.append("corpus must include at least one adversarial document")
    for source_family, splits in sorted(family_splits.items()):
        if len(splits) > 1:
            errors.append(
                f"source_family {source_family!r} crosses leakage boundary splits: {sorted(splits)}"
            )

    question_ids: set[str] = set()
    approved_questions = 0
    model_questions = 0
    deterministic_questions = 0
    question_fingerprints: set[str] = set()
    for index, question in enumerate(questions):
        prefix = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{prefix} must be an object")
            continue
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
        elif question_id in question_ids:
            errors.append(f"duplicate question id: {question_id}")
        else:
            question_ids.add(question_id)
        document_id = question.get("document_id")
        if document_id not in document_ids:
            errors.append(f"{prefix}.document_id does not reference a manifest document")
        if not isinstance(question.get("query"), str) or not question["query"].strip():
            errors.append(f"{prefix}.query must be a non-empty string")
        _validate_expected_labels(question.get("expected_labels"), prefix, errors)
        if (
            isinstance(question.get("query"), str)
            and question["query"].strip()
            and isinstance(question.get("expected_labels"), list)
            and question["expected_labels"]
        ):
            fingerprint = _question_fingerprint(question)
            if fingerprint in question_fingerprints:
                errors.append(f"{prefix} duplicates another normalized query/label pair")
            else:
                question_fingerprints.add(fingerprint)
        if _approved_legal_review(question.get("review")):
            approved_questions += 1
        else:
            errors.append(f"{prefix}.review must contain an approved legal review")
        mode = question.get("evaluation_mode")
        repeats = question.get("repeats")
        if mode == "model":
            model_questions += 1
            if not isinstance(repeats, int) or repeats < 5:
                errors.append(f"{prefix}.repeats must be at least 5 for model evaluation")
        elif mode == "deterministic":
            deterministic_questions += 1
            if repeats != 1:
                errors.append(f"{prefix}.repeats must equal 1 for deterministic evaluation")
        else:
            errors.append(f"{prefix}.evaluation_mode must be 'model' or 'deterministic'")

    if model_questions == 0:
        errors.append("corpus must include at least one model-involved question")
    if deterministic_questions == 0:
        errors.append("corpus must include at least one deterministic question")
    return {
        "schema_version": "1.0",
        "passed": not errors,
        "errors": errors,
        "document_count": len(documents),
        "question_count": len(questions),
        "slice_counts": dict(sorted(slices.items())),
        "adversarial_document_count": adversarial_documents,
        "approved_document_count": approved_documents,
        "approved_question_count": approved_questions,
        "model_question_count": model_questions,
        "deterministic_question_count": deterministic_questions,
        "concurrency_repetitions": concurrency_repetitions,
    }


def _check_count(
    label: str,
    actual: int,
    minimum: int,
    maximum: int | None,
    errors: list[str],
) -> None:
    if actual < minimum:
        errors.append(f"{label} count {actual} is below minimum {minimum}")
    if maximum is not None and actual > maximum:
        errors.append(f"{label} count {actual} exceeds maximum {maximum}")


def _approved_legal_review(review: Any) -> bool:
    independent_reviewers = review.get("independent_reviewer_ids") if isinstance(review, dict) else None
    return (
        isinstance(review, dict)
        and review.get("status") == "approved"
        and review.get("reviewer_role") == "legal"
        and isinstance(review.get("reviewer_id"), str)
        and bool(review["reviewer_id"].strip())
        and isinstance(review.get("reviewed_at"), str)
        and bool(review["reviewed_at"].strip())
        and isinstance(independent_reviewers, list)
        and len({item for item in independent_reviewers if isinstance(item, str) and item.strip()}) >= 2
        and review.get("agreement") in {"exact", "adjudicated"}
    )


def _validate_source_path(
    value: Any,
    root: Path,
    prefix: str,
    errors: list[str],
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.path must be a non-empty string")
        return None
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root):
        errors.append(f"{prefix}.path escapes source_root")
        return None
    elif not candidate.is_file():
        errors.append(f"{prefix}.path does not exist: {value}")
        return None
    return candidate


def _validate_expected_labels(value: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}.expected_labels must contain answer and locator labels")
        return
    for label_index, label in enumerate(value):
        label_prefix = f"{prefix}.expected_labels[{label_index}]"
        if not isinstance(label, dict):
            errors.append(f"{label_prefix} must be an object")
            continue
        if not isinstance(label.get("answer_text"), str) or not label["answer_text"].strip():
            errors.append(f"{label_prefix}.answer_text must be non-empty")
        locator = label.get("locator")
        if not isinstance(locator, dict) or not locator:
            errors.append(f"{label_prefix}.locator must be a non-empty exact locator")
            continue
        if not isinstance(locator.get("format"), str) or not locator["format"].strip():
            errors.append(f"{label_prefix}.locator.format must be non-empty")
        coordinate_keys = {
            "page_start",
            "line_start",
            "cell_range",
            "slide_number",
            "xpath",
            "part",
            "bbox",
        }
        if not coordinate_keys.intersection(locator):
            errors.append(f"{label_prefix}.locator must contain an exact source coordinate")


def _question_fingerprint(question: dict[str, Any]) -> str:
    query = " ".join(str(question.get("query", "")).casefold().split())
    labels = question.get("expected_labels")
    canonical_labels = json.dumps(labels if isinstance(labels, list) else [], sort_keys=True)
    return hashlib.sha256(
        f"{question.get('document_id')}\n{query}\n{canonical_labels}".encode()
    ).hexdigest()


def _is_sha256(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/corpus_manifest.json"))
    parser.add_argument("--questions", type=Path, default=Path("benchmarks/questions.jsonl"))
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--min-documents", type=int, default=100)
    parser.add_argument("--min-questions", type=int, default=500)
    parser.add_argument("--max-questions", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_corpus(
        args.manifest,
        args.questions,
        source_root=args.source_root,
        min_documents=args.min_documents,
        min_questions=args.min_questions,
        max_questions=args.max_questions,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
