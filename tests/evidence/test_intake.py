import json
import os
import stat
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from proofcounsel_ingest.evidence.intake import main
from proofcounsel_ingest.evidence.linking import Agreement, suggest

CORPUS = Path(__file__).resolve().parents[2] / "benchmarks/matter_linking"


def test_frozen_synthetic_family_suggestions():
    register = TypeAdapter(list[Agreement]).validate_json((CORPUS / "catalog.json").read_bytes())
    cases = [json.loads(line) for line in (CORPUS / "cases.jsonl").read_text().splitlines()]
    for case in cases:
        result = suggest([{"id": "passage-1", "text": case["text"]}], register)
        assert result.agreement == case["gold"], case
        assert bool(result.passage_ids) == bool(result.agreement)


def test_conflicting_reference_challenges():
    register = TypeAdapter(list[Agreement]).validate_json((CORPUS / "catalog.json").read_bytes())
    for line in (CORPUS / "challenge.jsonl").read_text().splitlines():
        case = json.loads(line)
        passages = [{"id": str(i), "text": t} for i, t in enumerate(case["passages"])]
        assert suggest(passages, register).agreement == case["gold"], case


def test_private_mixed_folder_manifest(tmp_path, monkeypatch):
    import pymupdf

    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "team-a").mkdir()
    (folder / "team-b").mkdir()
    (folder / "team-a/scan.txt").write_text("Side letter for MSA-407. The period remains thirty days for Alder.")
    (folder / "team-b/scan.txt").write_text("Side letter for MSA-4070. The amended period remains forty-five days.")
    (folder / "copy.txt").write_bytes((folder / "team-a/scan.txt").read_bytes())
    (folder / "broken.pdf").write_bytes(b"invalid PDF")
    outside = tmp_path / "out-of-scope.txt"
    outside.write_text("Private unrelated file.")
    (folder / "link.txt").symlink_to(outside)
    with (folder / "oversize.txt").open("wb") as f:
        f.truncate(10 * 1024 * 1024 + 1)
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 70), "Executed schedule for LIC-209. It changes the supplier address for written notices.")
    pdf.save(str(folder / "letter.pdf"))
    pdf.close()
    output = tmp_path / "review.jsonl"
    monkeypatch.setattr(
        sys, "argv", ["intake", str(folder), "--register", str(CORPUS / "catalog.json"), "--output", str(output)]
    )
    main()
    rows = {r["path"]: r for r in map(json.loads, output.read_text().splitlines())}
    assert set(rows) == {
        "copy.txt",
        "broken.pdf",
        "oversize.txt",
        "team-a/scan.txt",
        "team-b/scan.txt",
        "letter.pdf",
        "link.txt",
    }
    assert rows["link.txt"]["status"] == "skipped_nonregular"
    assert rows["oversize.txt"]["status"] == "over_upload_limit"
    assert rows["broken.pdf"]["status"] == "unreadable"
    assert rows["team-a/scan.txt"]["duplicate_of"] == "copy.txt"
    assert rows["team-a/scan.txt"]["agreement"] == "MSA-407"
    assert rows["team-b/scan.txt"]["agreement"] == "MSA-4070"
    assert rows["letter.pdf"]["agreement"] == "LIC-209"
    assert rows["letter.pdf"]["evidence"][0]["locator"]["page"] == 1
    assert stat.S_IMODE(os.stat(output).st_mode) == 0o600
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert before == output.read_bytes()


def test_exported_email_keeps_attachment_identity_and_missing_references_visible(tmp_path):
    from email.message import EmailMessage
    from tempfile import TemporaryDirectory

    from proofcounsel_ingest.evidence.intake import inventory

    register = TypeAdapter(list[Agreement]).validate_json((CORPUS / "catalog.json").read_bytes())
    mail = EmailMessage()
    mail["Subject"] = "Matter documents"
    mail["Message-ID"] = "<synthetic@example.invalid>"
    mail.set_content("Also see the separate signed receipt, which I cannot locate.")
    for body in (b"Supplement to MSA-407.", b"Supplement to LIC-209."):
        mail.add_attachment(body, maintype="text", subtype="plain", filename="scan.txt")
    path = tmp_path / "documents.eml"
    path.write_bytes(mail.as_bytes())
    with TemporaryDirectory() as work:
        rows = list(inventory(tmp_path, register, Path(work)))
    assert rows[0]["attachment_count"] == 2
    assert rows[0]["status"] == "email_inventory"
    assert [(r["attachment_index"], r["agreement"]) for r in rows[1:]] == [(1, "MSA-407"), (2, "LIC-209")]
    assert rows[1]["email_sha256"] == rows[0]["sha256"] == rows[2]["email_sha256"]
    assert len({r["sha256"] for r in rows}) == 3


def test_local_report_displays_evidence_as_plain_text():
    from io import StringIO

    from proofcounsel_ingest.evidence.intake import write_report

    output = StringIO()
    write_report(
        [
            {
                "path": "mail.eml",
                "attachment_index": 2,
                "attachment_name": "a&b.txt",
                "status": "suggested",
                "agreement": "A-9",
                "evidence": [{"locator": {"page": 4}, "text": "See <Terms>."}],
            }
        ],
        output,
    )
    page = output.getvalue()
    assert all(text in page for text in ("attachment 2", "a&amp;b.txt", "Page 4", "See &lt;Terms&gt;."))
    assert "<Terms>" not in page
