"""Check and repair the disposable practice installation without importing its dependencies."""

import argparse
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".evidence-practice-venv"
BIN = ENV / ("Scripts" if os.name == "nt" else "bin")
PYTHON = BIN / ("python.exe" if os.name == "nt" else "python")
AGENT = BIN / ("evidence-agent.exe" if os.name == "nt" else "evidence-agent")
REQUIRED = (
    "pyproject.toml",
    "uv.lock",
    "config/parser-defaults.toml",
    "src/proofcounsel_ingest/evidence/demo.py",
    "src/proofcounsel_ingest/evidence/desk.html",
    "src/proofcounsel_ingest/evidence/viewer.html",
)
IMPORT_CHECK = (
    "from proofcounsel_ingest.evidence.demo import main; from pageindex.flash import page_index_flash; "
    "from pypdf import PdfReader; from docx import Document; from pptx import Presentation; "
    "from openpyxl import load_workbook; from PIL import Image"
)


def works(args, *, quiet=True):
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


@contextmanager
def setup_lock():
    # The OS releases this lock if setup is interrupted.
    with (ROOT / ".evidence-practice.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another setup or repair may be running. Close it and try again.") from exc
        yield


def host_checks():
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        raise RuntimeError(
            "Missing release files: " + ", ".join(missing) + ". Extract the complete ZIP into a fresh folder."
        )
    print("OK  Release files and dependency lock are present.")
    with tempfile.TemporaryFile(dir=ROOT):
        pass
    with tempfile.TemporaryDirectory() as tmp:
        db = sqlite3.connect(Path(tmp) / "test.sqlite")
        try:
            db.execute("CREATE TABLE practice (value TEXT)")
            db.commit()
        finally:
            db.close()
    print("OK  Installation folder is writable and a temporary SQLite database can be created.")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
    print("OK  The browser desk can listen on this computer.")


def check_environment(uv, repair):
    if ENV.is_symlink() or (os.name == "nt" and ENV.is_junction()):
        raise RuntimeError("The practice environment points to another folder. Extract the ZIP into a fresh folder.")
    if ENV.exists() and not ((ENV / "pyvenv.cfg").is_file() or (ENV / ".evidence-managed").is_file()):
        raise RuntimeError(f"Unrecognized folder at {ENV.name}. Extract into a fresh folder to keep its contents safe.")
    sync = [
        uv,
        "sync",
        "--locked",
        "--python",
        "3.12",
        "--no-dev",
        "--group",
        "evidence",
        "--group",
        "pdf",
        "--group",
        "office",
    ]
    runtime = [PYTHON, "-I", "-B", "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 12))"]
    env_ok = (ENV / "pyvenv.cfg").is_file() and works(runtime)
    if not env_ok:
        print("NEEDS ATTENTION  Practice Python is missing or broken.")
        if not repair:
            return False
        if ENV.exists():
            saved = ENV.with_name(ENV.name + ".backup-" + uuid4().hex[:8])
            ENV.rename(saved)
            print(f"Saved the old environment as {saved.name}.")
    synced = env_ok and works([*sync, "--check", "--offline", "--no-python-downloads"])
    if not synced:
        print("NEEDS ATTENTION  The locked practice dependencies need installation or repair.")
        if not repair:
            return False
        if not works(sync, quiet=False):
            raise RuntimeError(
                "Package setup failed. Check available space, network and approved package sources, then run Doctor again."
            )
    if repair and not (ENV / ".evidence-managed").exists():
        (ENV / ".evidence-managed").touch()

    imports = [PYTHON, "-I", "-B", "-c", IMPORT_CHECK]
    for probe, label, reinstall in (
        (imports, "Practice app and document readers", ["--reinstall"]),
        ([AGENT, "--help"], "Evidence OS command", ["--reinstall-package", "proofcounsel-ingest"]),
    ):
        if not works(probe):
            print(f"NEEDS ATTENTION  {label} failed to start.")
            if not repair:
                return False
            print(f"Repairing {label.lower()} from the release lock.")
            if not works([*sync, *reinstall], quiet=False) or not works(probe, quiet=False):
                raise RuntimeError(f"{label} still fails its startup check. Give this report to IT.")
    if not synced and not works([*sync, "--check", "--offline", "--no-python-downloads"]):
        raise RuntimeError("The installed dependencies still differ from the release lock.")
    print("OK  Practice Python, locked packages, PDF and Office readers, and the Evidence OS command.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("start", "doctor", "fix"))
    parser.add_argument("uv")
    args = parser.parse_args()
    repair = args.mode != "doctor"
    os.environ["UV_PROJECT_ENVIRONMENT"] = str(ENV)
    print("Evidence OS practice doctor", flush=True)
    try:
        host_checks()
        if repair:
            with setup_lock():
                ready = check_environment(args.uv, True)
        else:
            ready = check_environment(args.uv, False)
        if not ready:
            print("Open Doctor Evidence OS to repair this installation.")
            return 1
        print("READY  The practice installation passed its checks.", flush=True)
        if args.mode == "start":
            print("Opening the fictional matter. Leave this window open.", flush=True)
            if os.name == "nt":
                return subprocess.call([AGENT, "demo"])
            os.execv(str(AGENT), [str(AGENT), "demo"])
        return 0
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"NEEDS ATTENTION  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
