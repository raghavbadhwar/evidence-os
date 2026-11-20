# First-time contract review

## Objective

Help a lawyer identify the controlling clause on an unfamiliar, permissioned matter and inspect exactly why it applies. Start with commercial termination and renewal notices. Keep first-time discovery and reuse of a reviewed rule separately measurable.

## Acceptance scenario

An agreement specifies 30 days. A later amendment specifies 45. A signed side letter stored as Commercial arrangements.pdf preserves 30 for Subsidiary A. An unsigned draft proposes another period. Test both subsidiaries, dates before and after the amendment, ambiguous signature evidence, and an absent delivery receipt. Distinguish the contractual period from proof that service occurred. For each answer report the party, termination route, operative date, controlling passages, and unresolved facts. Filenames and user-assigned document roles cannot establish authority.

## Engineering priorities

1. Discover candidate relationships between original passages across the authorized matter. Preserve both ends of each relationship, the entities and clauses affected, effective interval, and evidence for execution or uncertainty. Keep model-proposed and lawyer-approved interpretations distinguishable.
2. For each proposed conclusion search for evidence that could overturn it, including later changes, narrower exceptions, different routes and missing event evidence. Include complete original passages. Escalate conflicting or incomplete premises. Keep scope and source-version checks at retrieval, review and release.
3. Let reviewers correct scope and bind the correction to its source. Show which prior conclusions may need review after a change. Use conservative invalidation whenever dependencies are uncertain.
4. Retain the current lean stack. Measure first-time questions separately from repeat scenarios and count initial lawyer time. Use full-packet reading as a baseline when the packet fits.

## Qualification

Begin with reproducible synthetic fixtures. Keep gold labels outside model and retrieval inputs. For field evaluation target 30–50 unseen, permissioned matters as an initial diagnostic set, with independent lawyer annotations and adjudication. Reserve fresh matters for the final comparison. Compare Evidence OS, full-packet reasoning, hybrid retrieval with the same reasoner, and current lawyer workflow. Measure materially wrong releases at matched answer coverage, recall of every decisive passage, appropriate abstention, first/repeat p50 and p95, reviewer minutes and total cost per correct completed matter. Label unavailable measurements. Synthetic results alone cannot establish real-world superiority.

## Independent exception reader

A study operator can set `EVIDENCE_NOTICE_EXCEPTION_READER=true` before starting the Evidence OS server. The default is the original first-review workflow. The browser’s first-time notice form and the agent CLI reach the same assessment. For example, with a provisioned matter ID and credential:

```sh
evidence-agent notice-review "$MATTER_ID" "Alder Services" convenience --as-of 2026-03-01
```

After the first reader and verifier produce a reviewable period, the optional reader gets the same bounded original packet, party, route and date in a fresh request. It has no access to either preceding answer. It uses the configured reasoning model, so the errors of the two readings can be correlated. A single passage matching “<party> may terminate for <route> on <period> days of written notice” can skip the additional call. Other wordings and multi-passage packets get the check. Already unresolved, oversized or incomplete files retain the original manual fallback. The existing matter permissions, hosted-processing opt-in, server local-only switch and shared 35-second assessment budget apply.

Dates and execution evidence still receive the existing relationship check. The new comparison requires the same numeric period, controlling passage ID set and contractual conditions (ignoring order, case and repeated spaces), and no reader-reported gaps. Equivalent clauses cited in different places or conditions phrased differently may cause an escalation. The viewer shows the competing provisional readings and source links. The CLI returns the same details in `exception_check`. An unavailable reader, invalid citation or disagreement withholds the proposal and prevents approval. A lawyer can inspect the originals and use the usual manual workflow. A matching result still needs lawyer approval. Neither reader establishes service.

The assessment records the policy, extra-call count, reader latency and normal provider usage. Optional dispatch tracing labels the call `notice_exception_reading`. Changing the setting requires fresh first-review assessments for the selected policy. Previously approved reusable rules keep their own source-version and permission checks. Use separate study workspaces without prior rules when comparing policies. For automated first review, configure [hosted Codex Sol](matter-desk.md#hosted-codex-sol). The loopback model probe below processes only its four built-in fictional passages.

### Reproduce the development comparison

```sh
python -m benchmarks.first_time_notice
python -m benchmarks.first_time_notice --exception-reader
python -m benchmarks.first_time_notice --omit-side-letter
python -m benchmarks.first_time_notice --omit-side-letter --exception-reader
```

Each row covers the same six synthetic first questions. The injected fault makes the first reader ignore every side letter. The verifier is scripted to agree. The independent reader uses a controlled grammar. Counts below describe proposals before lawyer review.

| Scripted first reader | Extra check | Numeric proposals / 6 | Wrong numeric proposals | Extra reader calls |
|---|---|---:|---:|---:|
| Reads all sources | Off | 5 | 0 | 0 |
| Reads all sources | On | 5 | 0 | 5 |
| Omits the side letter | Off | 6 | 2 | 0 |
| Omits the side letter | On | 2 | 0 | 6 |

With the injected fault, the check caught the wrong period for Alder and the unsupported period when execution was uncertain. It also escalated two Birch answers whose numeric period was correct but whose cited evidence omitted the competing exception. All four went to manual review. Lawyer minutes and real-file miss rates remain unmeasured.

For the historical local-model comparison, the probe used a loopback Ollama service with an already-installed `qwen3:1.7b` model and cloud processing disabled. Run `python -m benchmarks.local_exception_reader` (or pass `--port PORT`). This sends the production exception-reader prompt and schema to the local model. On the signed fictional packet, Qwen cited the side letter for both parties and returned 30 days for both. Birch required 45. It also reported gaps for both readings, which would prevent approval. This probe exercises the reader only.

### Historical local workflow check

Run `python -m benchmarks.local_exception_reader --workflow` with the approved local-model environment. This creates separate temporary matters with hosted processing disabled, ingests originals, calls the real first-review endpoint, and reports the reviewed-answer state, timing and model usage. The expected periods enter the score report only.

On the development Mac, Qwen3 1.7B left both fictional tasks unresolved. On the single-clause task it put the established period in the field for outstanding questions. Its amended-file output failed validation. No answer was released. A separate test with scripted responses exercised the complete proposal, verification and independent reading sequence, including the 30-day exception and citation traces. The local inference path and the fitness of a particular model are separate acceptance decisions.

### Codex Sol workflow check

On 20 November 2026 the signed-in Codex CLI 0.171.0 ran `gpt-6.2-sol` on three fictional first reviews with `EVIDENCE_LOCAL_ONLY=false EVIDENCE_NOTICE_EXCEPTION_READER=false python -m benchmarks.codex_notice`. The initial run exposed ambiguous instructions about citing unsigned drafts. After clarifying the distinction between controlling citations and draft relationships, the two amended cases cited the framework, variation and signed side letter. A separate Sol verification pass still disputed a proposed relationship in each amended case. On the single-clause file it found insufficient support for the contractual minimum. All three final assessments required lawyer review, all withheld a confirmed period, and none started a service clock or released an answer. This small replay verifies the model route and identifies remaining review issues. It does not estimate accuracy on firm matters.
