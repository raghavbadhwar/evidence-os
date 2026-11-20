from benchmarks.evidence_qualification import GOLD, grade, run, summarize


def test_qualification_reports_correct_decisions_and_calls():
    report = run()
    assert report["scores"]["correct_supported"] == {"count": 2, "total": 2}
    assert report["scores"]["correct_abstention"] == {"count": 4, "total": 4}
    assert report["scores"]["unexpected_release"]["count"] == 0
    locate = [r for r in report["cases"] if r["route"] == "locate"]
    assert all(r["retrieved_all_decisive"] for r in locate)
    assert [r["scripted_calls"] for r in locate] == [1, 0]


def test_report_denominator_and_wrong_citation():
    rows = [{"route": "find", "pass": "first", "ms": n} for n in range(1, 21)]
    assert summarize(rows)["find/first"] == {"n": 20, "p50_ms": 10.5, "p95_ms": 19}
    answer = {
        "status": "supported",
        "evidence": [],
        "statements": [{"text": "45 calendar days meets the minimum of 45 calendar days", "evidence_ids": []}],
    }
    assert grade(answer, "45 calendar days meets", GOLD)["unexpected_release"]
