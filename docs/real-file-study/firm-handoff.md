# Firm pilot handoff

**Decision.** Use local rules to suggest which agreement a document belongs to. A reviewer confirms membership and separately determines the document's legal effect. Keep GLiNER2 as an offline comparison. Limited synthetic tests cannot establish the accuracy of either approach on firm files.

## What the model comparison actually measured

The register contains seven fictional agreements with repeat counterparties, date collisions and similarly named subsidiaries. Each example asks for one agreement ID or an abstention. The benchmark sees agreement metadata and text; gold labels enter only the scorer. There are 12 development examples (8 linkable) and 12 separately designated comparison examples (5 linkable). These sets were authored together in English. They are a diagnostic split, with no independent lawyers or representative prevalence. The inputs here are text; OCR and real file ingestion were tested separately.

| Split and implementation | Correct links / linkable | Wrong links | Correct abstentions / should abstain |
|---|---:|---:|---:|
| Development, initial ID match | 3/8 | 1 | 3/4 |
| Development, initial ID + parties/date/title | 6/8 | 1 | 3/4 |
| Development, initial fields + GLiNER2 | 8/8 | 1 | 3/4 |
| Development, revised fields | 7/8 | 0 | 4/4 |
| Development, revised fields + GLiNER2 | 7/8 | 0 | 4/4 |
| Held-out comparison, revised ID only | 2/5 | 0 | 7/7 |
| Held-out comparison, revised fields | 5/5 | 0 | 7/7 |
| Held-out comparison, revised fields + GLiNER2 | 5/5 | 0 | 7/7 |

The initial model helped match abbreviated company names. Normalizing company suffixes gave the rules that capability, but introduced a tie between a company and its similarly named subsidiary. The shipped matcher prefers the more specific name within an overlapping span and checks for conflicts across passages. Five wrong suggestions on eight additional challenge cases exposed weaknesses involving explicit denials, incidental IDs, references absent from the register and conflicting pages. The final matcher handles all eight examples. Those eight are regression checks after repair. They cannot estimate the remaining error rate. Other incidental references, previously unseen identifier conventions, numeric or non-English dates and OCR corruption can still produce inconclusive or wrong suggestions.

The model ran locally from [Fastino's model card](https://huggingface.co/fastino/gliner2-base-v1), revision `f9634218e53580c56edf0de97ca1a7d3f1c2354e`. The downloaded safetensors SHA-256 matched the publisher's LFS object `845fc4bd93c525b86124c58ab4f56c9eacf8587953086b14c501fab25957c007`. Offline inference used Python 3.11, gliner2 2.1.0, torch 2.15.0, transformers 4.57.6 and peft 0.18.1 on Apple silicon. Median extra extraction time was about 56 ms per short held-out text, excluding loading. The comparison uses extracted company spans to supplement registered party matching. It does not test fine-tuning, an LLM deciding legal effect, or every possible use of GLiNER2.

[First development results](linking-results/development-v1.json) · [Revised development results](linking-results/development-v2.json) · [Held-out results](linking-results/holdout-v2.json) · [Fixture inputs](../../benchmarks/matter_linking/cases.jsonl)

Reproduce from the repository root, using an isolated model environment and the verified model directory. Omit `--model` for the model-free controls.

```sh
HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 python -m benchmarks.matter_linking.compare --set development --revision 1 --model /approved/local-model
HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 python -m benchmarks.matter_linking.compare --set development --revision 2 --model /approved/local-model
HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 python -m benchmarks.matter_linking.compare --set holdout --revision 2 --model /approved/local-model
```

## Run the organizer

1. The contract owner supplies one frozen, permissioned export and an agreement register. Each register entry needs a unique ID, agreement title, party names and ISO agreement date. Start with the [synthetic register format](../../benchmarks/matter_linking/catalog.json). Make separate exports for different access groups. A flat export loses upstream mailbox and document permissions.
2. From the repository root run the following commands in a protected local workspace. Keep the report and register outside the intake folder. The output path must be new and on a filesystem supporting private files and hard links. Use `.html` for lawyer review or `.jsonl` for analysis.
3. Inspect suggested links by original relative path, cited text, page or other locator, source hash and, for attachments, the containing message hash and ordinal. Reconcile missing exhibits and email body references with the custodian. An absent source will never appear just because a file was readable. Verify drafts, execution and applicability separately.
4. Upload the accepted packet to its authorized Evidence OS source set, or have the custodian list the verified files for matter-scoped fetch in the [matter desk](../matter-desk.md). When two different files share a filename, choose distinct workspace names and retain their mapping to manifest paths. In the pilot workspace both functions remain operator-mediated. The organizer does not import a mailbox, automatically expose a file to another matter, or promote a suggestion into a legal rule.

```sh
uv sync --frozen --group pdf --group office --group evidence
.venv/bin/python -m proofcounsel_ingest.evidence.intake /approved/frozen-export --register /approved/register.json --output /approved/review.html
```

Originals stay in place. Temporary originals and extracted text are staged beside the report and removed on normal exit. An interrupted process can leave temporary private files. The report contains client text and inherits the deployment's retention obligations. The organizer reports exact duplicates with both locations, unreadable files, oversize files, skipped links and unresolved group membership. Exported EML attachments are inspected individually; message bodies, nested messages, cloud links and unsupported containers require manual reconciliation. OCR requires explicitly enabled local parser dependencies. The working-copy cap is 10 MiB per file.

## Firm acceptance

| Owner | Required before relying on the result | Status |
|---|---|---|
| Sponsoring partner and IT/security | Approved hosting, identities and roles, file sources, processors and permitted model spend, retention, backup target, incident owner, and acceptance criteria | Firm inputs pending |
| Pilot engineer | Qualified target OS, parser sandbox and upload scanning; identity and transport integration; restricted source storage; restore and revoked-access checks in that target | Local checks only. The loopback bearer-file setup is a pilot configuration |
| Firm records owner | Confirm the real source register, accountable file inventory, correct permissions and document relationships; record effort and unresolved attachments | Awaiting permissioned material |
| Legal reviewers | Lock reference results before viewing output, reconcile misses and false links, blind the comparison of signed answers and total lawyer time | [12-matter study protocol](protocol.md) prepared; field measurements pending |
| Product owner | Validate live model behavior, latency, costs and API failures in firm-approved environment. Check newly unaccounted documents, same-name imports, misleading source text and the post-amendment fallback | Scripted/live-local infrastructure checks cover distinct risks; hosted qualification pending |

For a broader production claim the repository also calls for an independently reviewed licensed legal corpus (at least 100 documents and 500–1,000 questions). The tracked benchmark manifest is a small engineering fixture. Its results cannot discharge that requirement. [Legal corpus workflow](../../benchmarks/LEGAL_GOLD_WORKFLOW.md)

## Local agent and lawyer checks

The branch now includes the [lawyer desk, agent CLI, operations guide and workflow diagram](../matter-desk.md). The persistent service and practice launcher use the same application. The launcher preloads a fictional agreement and amendment and lets the researcher fetch a signed side letter. An editor can record an open question, find the source and submit a proposed answer. Independent approval, current citations, exact question and as-of date are required for reuse. A records owner must register upstream changes.

| Check | Observed local result | What remains to test |
|---|---|---|
| CLI and authenticated API | Separate CLI processes against a loopback HTTP server went from missing question through fetch, exact file and reference search, byte-checked original download, proposal, independent approval and reuse. After registering another amendment, the old answer disappeared from current search. Unknown citations, changed source hash, same-name overwrite, wrong reviewer and cross-matter reads were rejected in the CLI/API checks. | Firm authentication, source permissions, records reconciliation and speed on target hardware. |
| Browser | In actual Chrome, a researcher saved an open question, fetched a file, opened its original, selected a passage and submitted a draft. A separate reviewer needed to open supporting passages before approval. The approved answer appeared on the next search. The 390-pixel viewport fit without horizontal scrolling. | Observe unfamiliar lawyers doing this unaided. Record time, errors and when they call for help. |
| Retrieval speed | 1,000 synthetic one-passage documents, 51 searches per mode, correct first result for all 153. Includes an absent-reference check. Warm in-process p95s were 1.75 ms for filename, 2.59 ms for reference and 5.05 ms for keyword question on this run. Separately, 21 fresh CLI processes queried a four-document loopback matter. The first call took 253 ms, warm median 91 ms and p95 107 ms. | Multiple clients, large or scanned files, office network, PostgreSQL, real naming conventions and real exception recall. Keyword timing uses seeded indexes and known IDs. It excludes the cost of ingestion, models and lawyer review. [Raw 1,000-document results](agent-search-results.json) |
| Actual Codex CLI with local Ollama | Codex CLI 0.171.0 used the locally installed Qwen3 1.7B Q4 model, with synthetic files and loopback only for inference. First attempt with the wider tool list made **zero** shell calls. With apps and plugins disabled it executed the absent-reference query twice and reported unresolved. For an existing reference it also queried twice, named the right side letter and said the delivery date was absent. It failed to provide the exact quotation requested and added an unsupported suggestion about signing. | This model failed the quotation task. For exact text use the direct CLI quotation or the browser’s source panel. Independent lawyer review is still required for proposed interpretations. These two model tasks do not estimate a hallucination rate. |

To reproduce the deterministic checks, run `python -m pytest -q tests/evidence/test_agent_cli.py tests/evidence/test_notebook.py` and `python -m benchmarks.agent_retrieval` in the installed evidence-test environment. The full isolated PostgreSQL suite also exercises backup/restore with a protected original-file copy. For an agent trial, point a fresh local Codex session at an approved local model, provide a synthetic matter-scoped agent credential to a local command wrapper, and check the executed commands and their returned citations. For the restricted local trials the relevant Codex flags were `--oss --local-provider ollama -m qwen3:1.7b --disable apps --disable plugins --disable in_app_browser --disable unified_exec`. Score both actual calls and the final answer. A fluent summary can introduce a new claim after a correct retrieval.

As provisional firm acceptance targets for this CLI, measure warm p95 below 250 ms for exact lookup and one second for keyword lookup on the firm's agreed corpus and concurrent-user load. Count unresolved and incomplete results, wrong sources, blocked access and fallback separately. The lawyer study's primary outcomes and stop rules remain in the [field protocol](protocol.md).

Before enrolling live matters, have three intended nontechnical users independently open the practice desk, find the signed exception, open the original and record the missing receipt. Give them the one-page user instructions above and keep the facilitator silent unless requested. Record completion, elapsed time, requests for help and any accidental claim of service. Proposed usability gate for this rehearsal: all three can open the original and leave service unresolved, and at least two finish the entire task within five minutes unaided. These are assumptions for revising the interface. Keep the fictional practice matter out of the field sample.

On the initial desk build at `3bd4169`, `python -m pytest -q` with isolated PostgreSQL test databases and Python 3.12 completed with **196 passed, six dependency deprecation warnings, zero skipped**. The separate restore check recovered four byte-identical originals, the approved notice rule and its audit record, denied unauthorized access and reproduced the cited follow-up. The fresh wheel contained both browser pages, the practice module and the installed CLI entry point. The macOS double-click launcher served the practice desk on a random loopback port.

### Required side letter listed before import

The initial desk continued returning an approved 45-day answer after the custodian added an unfetched side letter to the matter's approved file list. The records register now identifies required names, SHA-256 hashes and document roles. A change to those requirements invalidates the prior snapshot on the next request. The browser and CLI identify each missing, wrong-version, wrong-role or unready required file. Existing passages remain available for inspection. First review and hypothetical notice decisions withhold conclusions until the required versions are ready. A draft research note can still be saved. Its reviewer cannot approve it against an incomplete packet.

Chrome verified the missing-file warning, wrong-version warning, explicit replacement confirmation and display of the signed exception after indexing. A focused test under the local Codex sandbox exercised a previously approved answer, a cached hypothetical, the rule list, manual rule promotion, first review and a new approval attempt. Optional background references leave the required packet unchanged. Test with `python -m pytest -q tests/evidence/test_notebook.py`. The records owner must still identify relevant files missing from the register and notify the team about changes. A complete register does not establish that a reviewer interpreted each clause correctly.

With a synthetic register of 1,000 required files, all 153 known-target lookups returned the expected first hit. Warm p95 was 11.80 ms for filenames, 12.53 ms for exact references and 14.84 ms for keyword questions. This run used small indexed texts, SQLite, one client, and an in-process HTTP test client. Reproduce with `python -m benchmarks.agent_retrieval --required-library`. [Raw timing results](required-library-search-results.json)

The final isolated PostgreSQL run completed with **198 passed, six dependency deprecation warnings, zero skipped**. The restore check passed. Changing the register during a scripted model call also returned HTTP 409 and prevented publication of the earlier result.

### Correcting a reviewed service date

In a synthetic file, a 45-day rule applied to convenience termination. The notice was dispatched on March 1, delivered on March 2 and proposed termination on April 15, 2026. Recording dispatch as effective service produced **45 days, meets**. Recording signed delivery produced **44 days, falls short**. Before this change, the API had no way to withdraw the wrong receipt while retaining the same matter and sources.

A reviewer can now record, inspect and withdraw a delivery record in the contract review workspace. Withdrawal requires a reason, preserves the original in the catalogue and blocks reopening or reusing a supported assessment based on it. Fresh calculations with the withdrawn ID are unresolved. Retrying an identical registration by the same reviewer returns the existing ID, and replaying the identical withdrawn submission cannot reactivate it. Other receipts and hypothetical rules keep their independent validity.

Actual Chrome and installed CLI checks exercised both dates, withdrawal, a subsequent corrected answer with a receipt citation, and an assessment preceding service. The local Codex sandbox ran the two focused service regressions. The isolated PostgreSQL suite passed **199 tests with six dependency deprecation warnings**. The separate restore drill recovered the original files and audit trail, denied the old assessment, rejected an unknown credential and reproduced the 44-day calculation with the same two source passage IDs.

To repeat the restore drill on a Unix test host, install the project's `evidence-test` and `postgres` groups and place PostgreSQL's `initdb`, `pg_ctl`, `createdb`, `pg_dump` and `pg_restore` on the path. As a non-root user, run `python -m benchmarks.receipt_recovery` from the repository root. It creates a disposable, socket-only PostgreSQL cluster and two synthetic databases under a private temporary directory. No firm database or credentials are needed.

The software enforces reviewer permission, current cited evidence, date order, as-of date, applicable rule and verified conditions. The reviewer still determines which event legally constitutes service. A shared dispatch and delivery document can support a mistaken date entry. Grade this error before and after lawyer review in the field study.


### Separating a missing passage from a missed exception

An opt-in provider trace records the passage IDs in the serialized outgoing request to each hosted adapter. Only the matter owner can fetch the audit event. An offline transport test passed both the master and the side letter to the reasoning and verification adapters. The scripted response nevertheless chose 45 days and cited only the master. The audit contained the side-letter ID at both stages, and the assessment flagged the unexplained letter for the reviewer. A separate simulated HTTP 429 recorded the attempted IDs and error status. No external provider or client file was used. The trace concerns the request and response boundary; provider-internal attention remains unobservable.

Reproduce with `python -m pytest -q tests/evidence/test_providers.py -k study_trace`. Follow the [protocol](protocol.md) to join the owner-only audit event to the masked task. Legacy assessments and scripted providers that bypass the HTTP adapters have no transport trace. An interrupted process can lose its in-memory trace before the assessment exits. Treat missing traces as unknown.

With the transport trace enabled in the dedicated fixture, the focused Codex sandbox regression passed. The final isolated PostgreSQL suite completed with **200 passed, six dependency deprecation warnings and zero skipped**. Firm access, approval for the study processors and permissioned matters remain pending.
