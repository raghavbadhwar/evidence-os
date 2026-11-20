from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from benchmarks.runner import run_benchmark
from proofcounsel_ingest.answer import AnswerProviderResult
from proofcounsel_ingest.citations import CitationRequest
from proofcounsel_ingest.models import SourceLocator
from proofcounsel_ingest.prompt_boundary import GroundedAnswer, GroundedClaim

ROOT = Path(__file__).resolve().parents[2]


class EvidenceEchoProvider:
    def __init__(self, expected_text: str) -> None:
        self.expected_text = expected_text
        self.calls = 0

    def generate(self, prompt):  # type: ignore[no-untyped-def]
        self.calls += 1
        envelope = next(
            json.loads(message.content)
            for message in prompt.evidence
            if self.expected_text in message.content
        )
        locator = SourceLocator.model_validate(envelope["locator"])
        citation = CitationRequest(
            tenant_id=UUID(envelope["tenant_id"]),
            document_id=UUID(envelope["document_id"]),
            source_sha256=envelope["source_sha256"],
            block_id=UUID(envelope["block_id"]),
            answer_span=envelope["text"],
            quoted_text=envelope["text"],
            locator=locator,
        )
        answer = GroundedAnswer(
            answer=envelope["text"],
            claims=[GroundedClaim(text=envelope["text"], citation=citation)],
        )
        return AnswerProviderResult(
            answer=answer,
            model="evidence-echo",
            version="v1",
            input_tokens=20,
            output_tokens=10,
            provider_cost=0.01,
        )


def test_benchmark_runs_model_questions_at_adjudicated_repeat_count(tmp_path: Path) -> None:
    expected = "The filing deadline is 15 September 2026."
    (tmp_path / "document.txt").write_text(
        f"# Filing Deadline\n\n{expected}\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "corpus_manifest.json"
    questions_path = tmp_path / "questions.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "documents": [
                    {"id": "filing", "path": "document.txt", "slice": "text"}
                ],
            }
        ),
        encoding="utf-8",
    )
    questions_path.write_text(
        json.dumps(
            {
                "id": "filing-deadline",
                "document_id": "filing",
                "query": "When is the filing deadline?",
                "expected_labels": [
                    {
                        "answer_text": expected,
                        "locator": {"format": "text", "line_start": 3},
                    }
                ],
                "evaluation_mode": "model",
                "repeats": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    provider = EvidenceEchoProvider(expected)

    report = run_benchmark(
        manifest_path,
        questions_path,
        repeats=1,
        project_root=ROOT,
        source_root=tmp_path,
        answer_provider=provider,
    )

    assert provider.calls == 2
    assert report["run_count"] == 2
    assert report["slices"]["text"]["retrieval_recall_at_20"] == 1.0
    assert report["slices"]["text"]["gold_locator_accuracy"] == 1.0
    assert report["slices"]["text"]["grounded_answer_success_rate"] == 1.0
    assert report["slices"]["text"]["average_answer_input_tokens"] == 20.0
