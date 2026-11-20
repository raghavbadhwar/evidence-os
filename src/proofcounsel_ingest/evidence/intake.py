"""Build a private JSONL manifest or HTML review report for a single approved, frozen directory."""

import argparse
import json
import os
import tempfile
from email import policy
from email.parser import BytesParser
from html import escape
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from pydantic import TypeAdapter

from .ingestion import extract, source_path
from .linking import Agreement, suggest
from .store import digest

FILE_LIMIT = 10 * 1024 * 1024


class Intake:
    def __init__(self, register, work):
        self.register = register
        self.work = work
        self.tenant = str(uuid4())
        self.matter = str(uuid4())
        self.seen = {}

    def analyze(self, data, name, row, source_ref):
        sha = digest(data)
        row["sha256"] = sha
        # The same bytes with different suffixes can take different parser routes.
        cache_key = (sha, Path(name).suffix.casefold())
        if cache_key in self.seen:
            original, summary = self.seen[cache_key]
            return {**row, **summary, "duplicate_of": original}
        doc = {"id": str(uuid4()), "matter": self.matter, "name": name, "sha": sha}
        with tempfile.TemporaryDirectory(dir=self.work) as scratch:
            root = Path(scratch)
            copy = source_path(root, doc)
            copy.parent.mkdir(parents=True)
            copy.write_bytes(data)
            blocks, structure = extract(root, doc, self.tenant)
        passages = [{"id": b[0], "text": b[3], "locator": json.loads(b[5])} for b in blocks]
        match = suggest(passages, self.register)
        citations = [{"locator": p["locator"], "text": p["text"]} for p in passages if p["id"] in match.passage_ids]
        summary = {
            "status": "suggested" if match.agreement else "manual_review",
            "agreement": match.agreement,
            "reason": match.reason,
            "structure": structure,
            "passages": len(blocks),
            "evidence": citations,
        }
        self.seen[cache_key] = (source_ref, {k: v for k, v in summary.items() if k != "evidence"})
        return {**row, **summary}

    def email_rows(self, data, row):
        message = BytesParser(policy=policy.default).parsebytes(data)
        mail_hash = digest(data)
        attachments = list(message.iter_attachments())
        yield {
            **row,
            "status": "email_inventory",
            "sha256": mail_hash,
            "message_id": str(message.get("Message-ID", "")),
            "subject": str(message.get("Subject", "")),
            "attachment_count": len(attachments),
            "reason": "Review the original email body and check for referenced files and linked documents.",
        }
        for i, part in enumerate(attachments, 1):
            name = PureWindowsPath(str(part.get_filename() or "")).name
            ref = {**row, "email_sha256": mail_hash, "attachment_index": i, "attachment_name": name}
            if part.is_multipart() or not name or name in {".", ".."} or any(ord(c) < 32 for c in name):
                yield {**ref, "status": "unexpanded_attachment"}
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload or len(payload) > FILE_LIMIT:
                    yield {**ref, "status": "unreadable_attachment"}
                    continue
                ref_name = f"{row['path']} [attachment {i}: {name}]"
                yield self.analyze(payload, name, ref, ref_name)
            except (ValueError, OSError, RuntimeError) as error:
                yield {**ref, "status": "unreadable_attachment", "detail": str(error)[:240]}


def inventory(folder, register, work):
    intake = Intake(register, work)
    errors = []
    for directory, dirs, files in os.walk(folder, onerror=errors.append):
        dirs.sort()
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                dirs.remove(name)
                yield {"path": str(path.relative_to(folder)), "status": "skipped_symlink"}
        for name in sorted(files):
            path = Path(directory) / name
            rel = str(path.relative_to(folder))
            row = {"path": rel}
            if path.is_symlink() or not path.is_file():
                yield {**row, "status": "skipped_nonregular"}
                continue
            try:
                with path.open("rb") as fh:
                    data = fh.read(FILE_LIMIT + 1)
                if len(data) > FILE_LIMIT:
                    yield {**row, "status": "over_upload_limit"}
                    continue
                if path.suffix.casefold() == ".eml":
                    yield from intake.email_rows(data, row)
                else:
                    yield intake.analyze(data, name, row, rel)
            except (OSError, ValueError, RuntimeError) as error:
                yield {**row, "status": "unreadable", "detail": str(error)[:240]}
    for error in errors:
        yield {"path": os.path.relpath(error.filename, folder), "status": "unreadable_directory"}


def write_report(rows, out):
    out.write(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Agreement intake review</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>body{font:16px/1.5 system-ui;max-width:56rem;margin:3rem auto;padding:0 1rem;color:#152d2a}"
        "article{border-top:1px solid #adbcba;padding:1rem 0;overflow-wrap:anywhere}h1{font:2.4rem Georgia}"
        "summary{cursor:pointer}pre{white-space:pre-wrap}small{color:#42524e}</style>"
        "<h1>Agreement intake review</h1><p>Check suggested links against the originals. "
        "An unreadable document or an absent attachment can change the answer.</p>"
    )
    for row in rows:
        title = row["path"]
        if "attachment_index" in row:
            title += f" · attachment {row['attachment_index']} · {row['attachment_name']}"
        out.write(f"<article><strong>{escape(title)}</strong><br>{escape(row['status'].replace('_', ' '))}")
        if row.get("agreement"):
            out.write(f" · {escape(row['agreement'])}")
        out.write(f"<p>{escape(row.get('reason') or row.get('detail') or '')}</p>")
        if row.get("duplicate_of"):
            out.write(f"<p>Identical bytes: {escape(row['duplicate_of'])}</p>")
        if "attachment_count" in row:
            out.write(f"<p>Attachments listed: {row['attachment_count']}</p>")
        for cite in row.get("evidence", []):
            loc = cite["locator"]
            label = f"Page {loc['page']}" if loc.get("page") else json.dumps(loc, ensure_ascii=False)
            out.write(
                f"<details><summary>Source passage · {escape(label)}</summary><pre>{escape(cite['text'])}</pre></details>"
            )
        if "sha256" in row:
            out.write(f"<small>SHA-256 {escape(row['sha256'])}</small>")
        out.write("</article>")
    out.write("</html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "--register", type=Path, required=True, help="JSON agreement register, outside the intake folder"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New .jsonl manifest or .html report, outside the intake folder"
    )
    args = parser.parse_args()
    folder = args.folder.resolve(strict=True)
    if not folder.is_dir() or any(p.resolve().is_relative_to(folder) for p in [args.output, args.register]):
        parser.error("choose a directory and keep the register and output outside it")
    if args.output.suffix.lower() not in {".jsonl", ".html"}:
        parser.error("output must be .jsonl or .html")
    if args.output.exists():
        raise FileExistsError(args.output)
    rows = TypeAdapter(list[Agreement]).validate_json(args.register.read_bytes())
    if not rows or len({r.id.casefold() for r in rows}) != len(rows):
        parser.error("register requires distinct agreement IDs")
    # Stage privately beside the destination; publish a complete file without replacing any prior manifest.
    with tempfile.TemporaryDirectory(dir=args.output.parent) as staging:
        tmp = Path(staging)
        manifest = tmp / "manifest"
        with os.fdopen(os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
            stream = inventory(folder, rows, tmp)
            if args.output.suffix.lower() == ".html":
                write_report(stream, out)
            else:
                for row in stream:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.link(manifest, args.output)


if __name__ == "__main__":
    main()
