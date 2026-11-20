# ProofCounsel Legal Gold Corpus Workflow

This workflow separates candidate generation from legal approval. Bootstrap output is never gold and cannot pass `corpus_contract.py` until independent reviewers complete and adjudicate every slot.

## 1. Bootstrap candidate sources

Place at least 100 licensed legal documents under one source root. Keep related versions in similarly named files so they receive the same `source_family` and split.

```bash
.venv/bin/python benchmarks/legal_corpus_workflow.py bootstrap \
  --source-root /path/to/legal-sources \
  --output-root work/legal-gold-candidate \
  --question-slots 5 \
  --corpus-version legal-gold-v1
```

The command hashes and deduplicates sources, assigns family-safe train/dev/test splits, marks adversarial filenames, and creates two blank blinded packets:

- `review_packet_a.jsonl`
- `review_packet_b.jsonl`

No model answers or suggested labels are included in either packet.

## 2. Independent legal review

Two different legal reviewers independently fill every packet row:

- `query`: realistic legal question grounded in the assigned document.
- `expected_labels`: one or more exact `answer_text` and `locator` objects.
- `evaluation_mode`: `model` or `deterministic`.
- `repeats`: at least 5 for model questions; exactly 1 for deterministic questions.
- `reviewer_id`, `reviewer_role: legal`, `status: approved`, and `reviewed_at`.

Locators must include `format` and an exact coordinate such as `page_start`, `line_start`, `cell_range`, `slide_number`, `xpath`, `part`, or `bbox`.

Reviewers must not see each other's packet, benchmark outputs, model answers, or test-split scores before both packets are frozen.

## 3. Resolve disagreements

Run adjudication without a resolution file first. Exact agreements publish directly; disagreements fail closed and are listed in the report. A legal lead may create `resolution.jsonl` containing only disputed slot IDs, final labels, and `reviewer_id` equal to the CLI `--adjudicator-id`.

```bash
.venv/bin/python benchmarks/legal_corpus_workflow.py adjudicate \
  --manifest work/legal-gold-candidate/corpus_manifest.candidate.json \
  --source-root /path/to/legal-sources \
  --reviewer-a work/legal-gold-candidate/review_packet_a.jsonl \
  --reviewer-b work/legal-gold-candidate/review_packet_b.jsonl \
  --resolution work/legal-gold-candidate/resolution.jsonl \
  --output-manifest work/legal-gold-v1/corpus_manifest.json \
  --output-questions work/legal-gold-v1/questions.jsonl \
  --output-report work/legal-gold-v1/adjudication_report.json \
  --adjudicator-id legal-lead-opaque-id \
  --reviewed-at 2026-07-13T00:00:00Z
```

Source bytes are rehashed during adjudication. Changed, missing, duplicated, or split-leaking sources cannot publish gold outputs.

## 4. Validate the gold contract

```bash
.venv/bin/python benchmarks/corpus_contract.py \
  --manifest work/legal-gold-v1/corpus_manifest.json \
  --questions work/legal-gold-v1/questions.jsonl \
  --source-root /path/to/legal-sources \
  --output work/legal-gold-v1/validation_report.json
```

The default contract requires at least 100 documents, 500-1,000 questions, adversarial coverage, two independent legal reviewer IDs, exact locators, mixed model/deterministic cases, and at least three concurrency repetitions.

## 5. Run the benchmark

Model-labelled questions require a local provider factory with no arguments that returns an object implementing `generate(prompt)`. The provider must return the structured `AnswerProviderResult`, `GroundedAnswer`, or equivalent mapping expected by `proofcounsel_ingest.answer`.

```bash
.venv/bin/python benchmarks/runner.py \
  --manifest work/legal-gold-v1/corpus_manifest.json \
  --questions work/legal-gold-v1/questions.jsonl \
  --source-root /path/to/legal-sources \
  --answer-provider-factory your_package.provider:create_provider \
  --output work/legal-gold-v1/benchmark_report.json
```

Per-question repetition counts are taken from the adjudicated question rows. Model runs record grounded-answer success, abstention, citation verification, model/version, token usage, latency, provider cost, and prompt-injection signals. Deterministic rows never call the provider.

Do not tune prompts, routing thresholds, or models against the test split. Any change after looking at test results requires a new corpus version and untouched test families.
