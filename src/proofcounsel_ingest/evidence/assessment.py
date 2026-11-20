"""Bounded evidence assessment and optional independent first-time notice reading."""

import os
import re
import time

from .contracts import Draft, ExceptionReading, NoticeFinding, Proposal
from .library import packet_requirements, required_file_gaps
from .providers import ProviderFailure, meter, study_trace
from .retrieval import evidence, find, full_packet, packet_for, protect
from .service import (
    NOTICE_COVERAGE_POLICY,
    POLICY,
    current_docs,
    current_receipt,
    event,
    notice,
    notice_coverage_policy,
    read_run,
    scoped,
    snapshot,
)
from .store import Problem, digest, dump, uid

RECOVERY = {
    "authority": "amendment effective supersedes governing order precedence",
    "condition": "schedule definition conditions exception unless provided",
    "event": "delivery service received receipt notice date",
    "other": "definitions amendments exceptions effective receipt",
}


def assess(store, providers, actor, mid, req):
    metrics = {
        "selection_calls": 0,
        "reasoning_calls": 0,
        "verification_calls": 0,
        "exception_reader_calls": 0,
        "recovery_rounds": 0,
        "provider_usage": [],
    }
    rid = uid()
    trace = (
        {"assessment_id": rid, "calls": []} if os.getenv("EVIDENCE_STUDY_TRACE", "").lower() in {"1", "true"} else None
    )
    trace_token = study_trace.set(trace)
    token = meter.set(metrics)
    try:
        return _assess(store, providers, actor, mid, req, metrics, rid)
    finally:
        meter.reset(token)
        study_trace.reset(trace_token)
        if trace and trace["calls"]:
            with store.tx(True) as db:
                event(db, mid, actor.actor, "provider_dispatch_trace", dump(trace))


def _assess(store, providers, actor, mid, req, metrics, rid):
    started = time.monotonic()
    local_inference = getattr(providers, "runs_locally", False)
    draft = isinstance(req, Draft)
    mode = "draft" if draft else req.mode
    query = "\n".join(req.statements) if draft else req.question
    question = f"As of {req.as_of.isoformat()}: {query}"
    first_review = not draft and req.notice_review is not None
    request = req.model_dump(mode="json", exclude=None if first_review else {"notice_review"})
    with store.tx() as db:
        m = scoped(db, actor, mid)
        snap = snapshot(m)
        if (trace := study_trace.get()) is not None:
            trace["snapshot"] = snap
        key = digest([snap, m["decisions"], str(actor.actor), mode, request, providers.version, POLICY])
        if first_review:
            coverage_policy = notice_coverage_policy()
            key = digest([key, coverage_policy])
        if mode == "notice" and req.receipt_id and not current_receipt(db, mid, snap, req.receipt_id):
            key = digest([key, "receipt unavailable"])
        for cached in db.all("SELECT * FROM ev_runs WHERE matter=? AND cache_key=?", (mid, key)):
            value = read_run(db, m, cached)
            if value["status"] in {"supported", "reviewed", "quotation", "matches"}:
                for p in value["evidence"]:
                    evidence(db, mid, p["id"])
                return {
                    **value,
                    "cache_hit": True,
                    "original_metrics": value["metrics"],
                    "metrics": {**metrics, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)},
                }
        out = {
            "assessment_id": rid,
            "source_set_id": mid,
            "snapshot": snap,
            "policy": POLICY,
            "as_of": str(req.as_of),
            "mode": mode,
            "request": request,
            "status": "unresolved",
            "statements": [],
            "proposals": [],
            "evidence": [],
            "gaps": [],
            "cache_hit": False,
            "provider_versions": providers.version,
            "metrics": metrics,
        }
        if first_review:
            out["coverage_policy"] = coverage_policy
            out["notice_review_epoch"] = m["decisions"]
            out["service"] = {
                "clock_started": None,
                "gap": "Actual service requires a current reviewer-verified service receipt.",
            }
        docs = current_docs(db, mid)
        gaps_by_file = {d["name"]: d["status"] for d in docs if d["status"] != "ready"}
        gaps_by_file.update(required_file_gaps(m, docs))
        missing = [f"{name} ({reason})" for name, reason in gaps_by_file.items()]
        if mode == "quote":
            p = evidence(db, mid, req.evidence_id)
            out.update(status="quotation", evidence=[p], statements=[{"text": p["text"], "evidence_ids": [p["id"]]}])
        elif mode == "find":
            out["evidence"] = find(db, mid, query)
            out["gaps"] = missing
            out["status"] = "matches" if out["evidence"] else "unresolved"
            if not out["evidence"]:
                out["gaps"].append("no matching passages")
        elif not docs or missing:
            out["gaps"] = missing or ["source set is empty"]
        elif mode == "notice":
            notice(db, m, req, out)
        elif first_review:
            packet = full_packet(db, mid)
            compact = [{k: p[k] for k in ("id", "document_id", "text")} for p in packet]
            if not packet or len(dump(compact).encode()) > 32000:
                out.update(
                    evidence=find(db, mid, query),
                    gaps=["Full contract packet exceeds the first-time review budget. Review the original documents."],
                )
            else:
                out["evidence"] = packet
                if (not local_inference and not m["hosted"]) or not providers.available(reason=True):
                    out["gaps"] = ["hosted model processing disabled or credentials unavailable"]
        else:
            anchors = find(db, mid, query)
            mandatory, overflow = protect(db, mid, anchors)
            packet = packet_for(anchors, mandatory)
            if overflow or not packet:
                out["gaps"] = [overflow or "no relevant passages retrieved"]
            elif (not local_inference and not m["hosted"]) or not providers.available(reason=mode == "interpret"):
                out["evidence"] = packet[:16]
                out["gaps"] = ["hosted model processing disabled or credentials unavailable"]
    if mode in {"draft", "interpret", "locate"} and not out["gaps"]:

        def gate():
            with store.tx() as db:
                cur = scoped(db, actor, mid)
                if (
                    snapshot(cur) != snap
                    or (not local_inference and not cur["hosted"])
                    or cur["decisions"] != m["decisions"]
                ):
                    raise Problem(409, "scope changed before provider dispatch")

        try:
            deadline = started + 35
            if first_review:
                discover_notice(req, providers, compact, deadline, gate, out)
            else:
                selection = anchors or mandatory[:16]
                gap = "other"
                for attempt in range(2):
                    gate()
                    out["metrics"]["selection_calls"] += 1
                    selected, gap = providers.select(question, selection, mandatory, deadline)
                    options = {p["id"]: p for p in selection}
                    if not set(selected) <= set(options):
                        raise ProviderFailure("unknown retrieval ID")
                    chosen = [options[k] for k in selected]
                    packet = packet_for(chosen, mandatory)
                    if gap == "complete" and packet:
                        break
                    if attempt == 0:
                        out["metrics"]["recovery_rounds"] = 1
                        with store.tx() as db:
                            scoped(db, actor, mid)
                            more = find(db, mid, query + " " + RECOVERY.get(gap, RECOVERY["other"]), 32)
                            previous = {p["id"] for p in selection}
                            selection = (
                                packet_for(chosen, [p for p in more if p["id"] not in previous])[:16] or selection
                            )
                            additions, overflow = protect(db, mid, selection)
                            if overflow:
                                raise ProviderFailure(overflow)
                            mandatory = packet_for(mandatory, additions)
                if gap != "complete" or not packet:
                    raise ProviderFailure("missing evidence after recovery: " + gap)
                compact = [{k: p[k] for k in ("id", "filename", "role", "heading", "text")} for p in packet]
                if len(dump(compact).encode()) > 40000:
                    raise ProviderFailure("complete evidence exceeds packet budget")
                if mode == "locate":
                    out.update(status="matches", evidence=packet)
                else:
                    gate()
                    if draft:
                        claims = [{"text": s, "evidence_ids": [p["id"] for p in packet]} for s in req.statements]
                    else:
                        out["metrics"]["reasoning_calls"] += 1
                        claims = providers.propose(question, compact, deadline)
                    claims = Proposal.model_validate({"statements": claims}).model_dump(mode="json")["statements"]
                    ids = {p["id"] for p in packet}
                    if any(not set(c["evidence_ids"]) <= ids for c in claims):
                        raise ProviderFailure("proposal references unknown evidence")
                    verdicts = verify_claims(providers, question, compact, claims, deadline, gate, out)
                    out.update(
                        status="needs_review",
                        evidence=packet,
                        proposals=[{**c, "verdict": v} for c, v in zip(claims, verdicts, strict=True)],
                        gaps=[f"statement {i + 1}: {v}" for i, v in enumerate(verdicts) if v != "supported"],
                    )
        except Problem:
            raise
        except Exception as error:
            out["gaps"] = [
                str(error)
                if isinstance(error, ProviderFailure)
                else "provider unavailable or invalid structured response"
            ]
    out["metrics"]["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    with store.tx(True) as db:
        current = scoped(db, actor, mid, lock=True)
        if snapshot(current) != snap or current["decisions"] != m["decisions"]:
            raise Problem(409, "evidence or reviewed rules changed during assessment")
        if (
            mode == "notice"
            and req.receipt_id
            and out["status"] == "supported"
            and not current_receipt(db, mid, snap, req.receipt_id)
        ):
            raise Problem(409, "service receipt withdrawn during assessment")
        for p in out["evidence"]:
            evidence(db, mid, p["id"])
        # Retry unresolved results. Cache only completed results and reviewable proposals.
        persist_key = key if out["status"] != "unresolved" else ""
        manifest = {
            "documents": docs,
            "required_files": packet_requirements(current),
            "links": db.all("SELECT parent,child FROM ev_links WHERE matter=? ORDER BY parent,child", (mid,)),
            "revision": current["revision"],
            "policy": POLICY,
        }
        db.execute("INSERT INTO ev_snapshots VALUES(?,?,?) ON CONFLICT DO NOTHING", (snap, mid, dump(manifest)))
        db.execute("INSERT INTO ev_runs VALUES(?,?,?,?,?)", (out["assessment_id"], mid, snap, persist_key, dump(out)))
        event(db, mid, actor.actor, "assessment_" + out["status"], out["assessment_id"])
    return out


def discover_notice(req, providers, packet, deadline, gate, out):
    scope = {**req.notice_review.model_dump(), "as_of": str(req.as_of)}
    question = (
        f"What is the contractual minimum for {scope['party']} on the {scope['route']} route "
        f"as of {scope['as_of']}? Check the original clauses, changes, execution and exceptions. "
        "Do not determine actual service or a clock start."
    )
    gate()
    out["metrics"]["reasoning_calls"] += 1
    finding = NoticeFinding.model_validate(providers.propose_notice(scope, packet, deadline)).model_dump(mode="json")
    claims = []
    gaps = finding["gaps"]
    days = finding["minimum_calendar_days"]
    controlling = finding["evidence_ids"]
    for r in finding["relationships"]:
        ids = list(dict.fromkeys([r["from_evidence_id"], r["to_evidence_id"], *r["execution_evidence_ids"]]))
        parties = ", ".join(r["parties"])
        start = r["effective_from"] or "unspecified start"
        end = r["effective_until"] or "unspecified end"
        execution = r["execution"]
        claims.append(
            {
                "text": (
                    f"{r['from_evidence_id']} is a {r['kind']} of {r['to_evidence_id']} for {parties} "
                    f"on the {r['route']} route, from {start} (inclusive) to {end} (exclusive). "
                    f"Execution status is {execution}. Evidence IDs for execution are "
                    f"{', '.join(r['execution_evidence_ids']) or 'unavailable'}."
                ),
                "evidence_ids": ids,
            }
        )
        if (
            execution == "uncertain"
            and scope["party"].casefold() in {p.casefold() for p in r["parties"]}
            and scope["route"].casefold() == r["route"].casefold()
            and (not r["effective_from"] or r["effective_from"] <= scope["as_of"])
            and (not r["effective_until"] or scope["as_of"] < r["effective_until"])
        ):
            gaps.append(f"Execution is uncertain for a potential controlling passage ({r['from_evidence_id']}).")
        if execution == "draft" and r["from_evidence_id"] in controlling:
            gaps.append("The proposed controlling citations include an unsigned draft.")

    known = {p["id"] for p in packet}
    cited = set(controlling).union(*(set(c["evidence_ids"]) for c in claims))
    explained_docs = {p["document_id"] for p in packet if p["id"] in cited}
    out["unexplained_documents"] = sorted({p["document_id"] for p in packet} - explained_docs)
    if not cited <= known:
        raise ProviderFailure("notice finding references unknown evidence")
    if not gaps and (days is None or not controlling):
        gaps.append("The controlling contractual notice period could not be established.")
    if not gaps:
        conditions = ", ".join(finding["conditions"])
        text = (
            f"For {scope['party']} on the {scope['route']} route as of {scope['as_of']}, "
            f"the contractual minimum is {days} calendar days."
        )
        if conditions:
            text += f" The contractual conditions are {conditions}."
        claims.append({"text": text, "evidence_ids": controlling})

    proposals = []
    if claims:
        verdicts = verify_claims(providers, question, packet, claims, deadline, gate, out)
        for i, r in enumerate(finding["relationships"]):
            r["verdict"] = verdicts[i]
            if verdicts[i] != "supported":
                gaps.append(f"Relationship at passage {r['from_evidence_id']}: {verdicts[i]}.")
        if len(verdicts) > len(finding["relationships"]):
            verdict = verdicts[-1]
            proposals = [{**claims[-1], "verdict": verdict}]
            if verdict != "supported":
                gaps.append(f"Contractual minimum: {verdict}.")
    if gaps:
        finding["minimum_calendar_days"] = None
    out.update(
        status="needs_review" if proposals else "unresolved",
        notice_review={**scope, **finding},
        proposals=proposals,
        gaps=gaps,
    )

    if out["coverage_policy"] != NOTICE_COVERAGE_POLICY and not gaps and proposals:
        simple_clause = (
            rf"{re.escape(scope['party'])} may terminate for {re.escape(scope['route'])} "
            rf"on {days} days of written notice\.?"
        )
        if len(packet) == 1 and re.fullmatch(simple_clause, " ".join(packet[0]["text"].split()), re.IGNORECASE):
            out["exception_check"] = {"status": "skipped_simple"}
        else:
            check_exceptions(providers, scope, packet, deadline, gate, out)


def check_exceptions(providers, scope, packet, deadline, gate, out):
    gate()
    finding = out["notice_review"]
    check = out["exception_check"] = {"status": "failed", "first_days": finding["minimum_calendar_days"]}
    out["metrics"]["exception_reader_calls"] += 1
    started = time.monotonic()
    try:
        reading = ExceptionReading.model_validate(providers.read_exceptions(scope, packet, deadline)).model_dump(
            mode="json"
        )
        if time.monotonic() > deadline:
            raise ProviderFailure("Independent exception reader exceeded the assessment deadline.")
        references = set(reading["evidence_ids"])
        if not references <= {p["id"] for p in packet}:
            raise ProviderFailure("Independent exception reader cited an unknown passage.")
        check["reader"] = reading
        gaps = out["gaps"]
        gaps.extend(f"Independent reader: {gap}" for gap in reading["gaps"])
        if reading["minimum_calendar_days"] != finding["minimum_calendar_days"]:
            gaps.append("The readers reached different contractual notice periods.")
        if not references or references != set(finding["evidence_ids"]):
            gaps.append("The readers could not establish the same controlling passages.")

        def terms(conditions):
            return {" ".join(c.casefold().split()) for c in conditions}

        if terms(reading["conditions"]) != terms(finding["conditions"]):
            gaps.append("The readers reported different contractual conditions.")
        check["status"] = "disagreement" if gaps else "agreed"
    except Problem:
        raise
    except Exception as exc:
        out["gaps"].append(
            str(exc)
            if isinstance(exc, ProviderFailure)
            else "Independent exception reader unavailable or returned an invalid response."
        )
    finally:
        check["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    if check["status"] != "agreed":
        finding["minimum_calendar_days"] = None
        out.update(status="unresolved", proposals=[])


def verify_claims(providers, question, packet, claims, deadline, gate, out):
    gate()
    out["metrics"]["verification_calls"] += 1
    verdicts = providers.verify(question, packet, claims, deadline)
    if len(verdicts) != len(claims) or any(v not in {"supported", "insufficient", "conflict"} for v in verdicts):
        raise ProviderFailure("incomplete verification")
    if time.monotonic() > deadline:
        raise ProviderFailure("assessment deadline exceeded")
    return verdicts
