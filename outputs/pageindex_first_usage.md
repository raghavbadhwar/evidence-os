# ProofCounsel V2 ingestion usage

The production path is the installed `proofcounsel-ingest` CLI. The two scripts in `outputs/` are one-release compatibility wrappers and emit a deprecation warning.

## Health check

```bash
uv run proofcounsel-ingest doctor
```

The health report lists Poppler, OCRmyPDF, Tesseract, and optional Office/Tika capabilities. A missing backend is reported explicitly; it is not treated as successful ingestion.

## Ingest a native PDF

```bash
uv run proofcounsel-ingest ingest /absolute/path/document.pdf \
  --artifact-root /absolute/path/artifacts
```

## Ingest a scanned or mixed PDF

```bash
uv run proofcounsel-ingest ingest /absolute/path/scanned.pdf \
  --artifact-root /absolute/path/artifacts \
  --enable-ocr
```

OCR is selective. Native text remains selected on trustworthy pages, while only deficient pages are processed through OCRmyPDF/Tesseract.

## Ingest Office, HTML, text, or images

```bash
uv run proofcounsel-ingest ingest /absolute/path/contract.docx --artifact-root /absolute/path/artifacts
uv run proofcounsel-ingest ingest /absolute/path/model.xlsx --artifact-root /absolute/path/artifacts
uv run proofcounsel-ingest ingest /absolute/path/deck.pptx --artifact-root /absolute/path/artifacts
uv run proofcounsel-ingest ingest /absolute/path/page.html --artifact-root /absolute/path/artifacts
uv run proofcounsel-ingest ingest /absolute/path/notice.png --artifact-root /absolute/path/artifacts --enable-ocr
```

## Exit codes

| Code | State | Meaning |
|---:|---|---|
| 0 | `ready` | Quality gates passed and indexes/citations can use the artifact |
| 10 | `needs_ocr` | OCR is required but not enabled or available |
| 11 | `partial` | Evidence exists but a quality gate failed; not queryable by default |
| 20 | `quarantined` | Upload validation, malware policy, or production security policy rejected the file |
| 30 | `failed` | Parsing or publication failed without publishing a partial artifact |

## Canonical artifact layout

```text
<artifact-root>/documents/<tenant>/<source-sha256>/<extractor-version>/<config-hash>/
  artifact_manifest.json
  ingestion_manifest.json
  blocks.jsonl
  evidence_variants.jsonl
  pages.json
  structure.json
  pageindex.json
  index_versions.json
  indexes/
  assets/
  derived/
```

Artifacts are immutable and content-addressed. Reusing the same source and configuration creates a new audit run but reuses the verified artifact. A different file with the same filename cannot reuse the artifact.

## Compatibility commands

```bash
uv run python outputs/universal_pageindex_ingest.py \
  --input /absolute/path/document.pdf \
  --output-dir /absolute/path/legacy-view \
  --query "Article 14 equality"

uv run python outputs/pageindex_first_builder.py \
  --pdf /absolute/path/document.pdf \
  --output-dir /absolute/path/legacy-view \
  --query "Article 14 equality"
```

Compatibility outputs mirror the previous filenames while pointing to canonical V2 artifacts. They do not restore the old silent-success or filename-cache behavior.
