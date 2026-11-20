"""Package a committed practice workspace with a locked uv quickstart."""

import argparse
import io
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build(destination, revision):
    repo = Path(__file__).resolve().parents[1]

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo)

    commit = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
    folder = f"Evidence-OS-Quickstart-{commit[:7]}"
    contents = git(
        "archive",
        "--format=zip",
        commit,
        "src/proofcounsel_ingest",
        "config/parser-defaults.toml",
        "pyproject.toml",
        "uv.lock",
        "docs/practice-setup.md",
        "AGENT-SETUP.md",
        "Start Evidence OS.command",
        "Start Evidence OS.cmd",
        "Doctor Evidence OS.command",
        "Doctor Evidence OS.cmd",
        "scripts/practice_setup.py",
    )
    output = destination / (folder + ".zip")
    with ZipFile(io.BytesIO(contents)) as sources, ZipFile(output, "x", compression=ZIP_DEFLATED) as release:
        for item in sources.infolist():
            if item.is_dir():
                continue
            name = "START HERE.md" if item.filename == "docs/practice-setup.md" else item.filename
            data = sources.read(item)
            if name == "pyproject.toml":
                data += b'\n[tool.uv]\ndefault-groups = ["evidence", "pdf", "office"]\n'
            item.filename = folder + "/" + name
            item.compress_type = ZIP_DEFLATED
            release.writestr(item, data)
        release.writestr(folder + "/.python-version", "3.12\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Existing output directory")
    parser.add_argument("--revision", default="HEAD", help="Committed revision to package")
    args = parser.parse_args()
    print(build(args.destination, args.revision))
