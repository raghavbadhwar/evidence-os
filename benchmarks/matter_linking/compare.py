"""Offline comparison. Gold is read only by the scorer; no client files or hosted calls."""

import argparse
import json
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
DASHES = dict.fromkeys(range(0x2010, 0x2015), "-")
SUFFIX = re.compile(r"\s+(limited|ltd|inc|llc|plc)\.?$", re.I)
LABELS = ["company name", "agreement title", "agreement reference number", "agreement date"]


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def contains(text, phrase):
    return bool(phrase) and f" {phrase} " in f" {text} "


def match(text, catalog, method, entities=None, revision=1):
    raw = text.translate(DASHES).upper()
    ids = [r["id"] for r in catalog if re.search(r"(?<![\w-])" + re.escape(r["id"]) + r"(?![\w-])", raw)]
    if revision >= 2 and re.search(
        r"\b(example only|for (?:illustration|comparison) only|training (?:guide|example))\b", text, re.I
    ):
        return None
    if ids or method == "id":
        return ids[0] if len(ids) == 1 else None
    normalized = normalize(text)
    companies = {normalize(SUFFIX.sub("", s)) for s in (entities or {}).get("company name", [])}
    candidates = []
    for row in catalog:
        d = date.fromisoformat(row["date"])
        formats = [d.isoformat(), d.strftime("%-d %B %Y"), d.strftime("%B %-d, %Y")]
        if not any(contains(normalized, normalize(f)) for f in formats):
            continue
        title = contains(normalized, normalize(row["title"]))
        parties = 0
        for name in row["parties"]:
            stem = normalize(SUFFIX.sub("", name))
            match_name = stem if revision >= 2 else normalize(name)
            parties += contains(normalized, match_name) or stem in companies
        if parties == 2 or (title and parties):
            candidates.append((parties + int(title), row["id"]))
    if not candidates:
        return None
    best = max(score for score, _ in candidates)
    winners = [rid for score, rid in candidates if score == best]
    return winners[0] if len(winners) == 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path)
    ap.add_argument("--set", choices=["development", "holdout"], required=True)
    ap.add_argument("--revision", choices=[1, 2], type=int, default=1)
    args = ap.parse_args()
    catalog = json.loads((HERE / "catalog.json").read_text())
    cases = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines()]
    model = None
    if args.model:
        from gliner2 import GLiNER2

        model = GLiNER2.from_pretrained(str(args.model.resolve()), local_files_only=True)
    rows = []
    for case in cases:
        if case["set"] != args.set:
            continue
        text = case["text"]
        start = time.perf_counter()
        entities = model.extract_entities(text, LABELS)["entities"] if model else None
        inference_ms = round((time.perf_counter() - start) * 1000, 1) if model else None
        for method in ["id", "fields", "fields+model"] if model else ["id", "fields"]:
            prediction = match(text, catalog, method, entities if method == "fields+model" else None, args.revision)
            outcome = (
                ("correct_link" if prediction else "correct_abstention")
                if prediction == case["gold"]
                else ("wrong_link" if prediction else "miss")
            )
            rows.append(
                dict(
                    case=case["case"],
                    method=method,
                    predicted=prediction,
                    expected=case["gold"],
                    outcome=outcome,
                    model_ms=inference_ms if method == "fields+model" else None,
                )
            )
    scores = {
        m: dict(Counter(r["outcome"] for r in rows if r["method"] == m)) for m in sorted({r["method"] for r in rows})
    }
    print(json.dumps(dict(split=args.set, revision=args.revision, scores=scores, rows=rows), indent=2))


if __name__ == "__main__":
    main()
