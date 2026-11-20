"""Run from the repository root. The worker and HTTP listener share one catalogue."""

import argparse
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from uuid import uuid4

from .ingestion import work_one
from .store import Store


def loop(store, stopping):
    log = logging.getLogger("evidence.worker")
    while not stopping.is_set():
        try:
            if not work_one(store):
                stopping.wait(0.4)
        except Exception:
            log.exception("Worker transaction failed")
            stopping.wait(2)


def main():
    p = argparse.ArgumentParser(prog="evidence-os")
    p.add_argument("command", choices=["init", "serve", "worker"])
    p.add_argument("--root", type=Path, default=Path(os.getenv("EVIDENCE_ROOT", ".evidence")))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--external-worker", action="store_true")
    args = p.parse_args()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(os.getenv("EVIDENCE_AUTH_FILE", str(args.root / "identities.json")))
    if args.command == "init":
        token = secrets.token_urlsafe(36)
        identity = {token: {"actor": str(uuid4()), "tenant": str(uuid4()), "reviewer": True}}
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fp:
            json.dump(identity, fp, indent=2)
        print(f"Created {path}. The object's key is your local bearer credential.")
        return
    store = Store(args.root, os.getenv("EVIDENCE_DATABASE_URL"))
    stop = threading.Event()
    if args.command == "worker":
        loop(store, stop)
        return
    import uvicorn

    from .api import create_app

    os.environ["EVIDENCE_AUTH_FILE"] = str(path)
    app = create_app(store=store)
    worker = threading.Thread(target=loop, args=(store, stop), daemon=True)
    if not args.external_worker:
        worker.start()
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        stop.set()
        if worker.is_alive():
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
