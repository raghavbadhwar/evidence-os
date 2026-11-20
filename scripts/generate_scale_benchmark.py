"""Generate a synthetic scale corpus without making legal-quality claims."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.scale_generator import generate_scale_corpus


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--questions-per-document", type=int, default=5)
    args = parser.parse_args()
    manifest, questions = generate_scale_corpus(
        args.output,
        document_count=args.documents,
        questions_per_document=args.questions_per_document,
    )
    print(f"manifest={manifest}")
    print(f"questions={questions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
