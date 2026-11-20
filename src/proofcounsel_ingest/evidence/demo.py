"""Disposable loopback demonstration with fictional files and two separate identities."""

import hashlib
import json
import os
import secrets
import socket
import tempfile
import threading
import webbrowser
from pathlib import Path
from uuid import uuid4

import uvicorn

from . import notebook, service
from .__main__ import loop
from .api import create_app
from .contracts import Matter, Member, Principal
from .ingestion import work_one
from .store import Store

SOURCES = {
    "Agreement.txt": (
        "agreement",
        "Instrument C-7. A and B may each terminate for convenience on 30 days' written notice.",
    ),
    "Amendment.txt": (
        "amendment",
        "Signed amendment to C-7. From 1 January 2026 the parties agree to 45 days' written notice for convenience termination.",
    ),
    "Side letter.txt": (
        "other",
        "Signed C-7 side letter effective 1 January 2026. Subsidiary A alone retains 30 days' written notice for convenience termination. The amendment continues to apply to Subsidiary B.",
    ),
}


def main():
    with tempfile.TemporaryDirectory(prefix="evidence-demo-") as tmp, socket.socket() as sock:
        root = Path(tmp)
        store = Store(root / "demo")
        tenant = uuid4()
        owner, editor = [Principal(tenant=tenant, actor=uuid4(), reviewer=r) for r in (True, False)]
        tokens = {secrets.token_urlsafe(28): p for p in (editor, owner)}
        auth = {hashlib.sha256(t.encode()).hexdigest(): p for t, p in tokens.items()}
        mid = service.create(store, owner, Matter(name="Practice matter · Alder contract"))["source_set_id"]
        service.member(store, owner, mid, Member(actor=editor.actor, permission="editor"))
        sources = {}
        for name, (role, body) in SOURCES.items():
            path = root / name
            path.write_bytes(body.encode())
            sources[name] = {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest(), "role": role}
        config = root / "sources.json"
        config.write_text(json.dumps({mid: sources}))
        os.environ.update(EVIDENCE_LIBRARY_CONFIG=str(config), EVIDENCE_LOCAL_ONLY="true")
        for name in ("Agreement.txt", "Amendment.txt"):
            notebook.fetch(store, editor, mid, notebook.Fetch(source=name))
            work_one(store)
        app = create_app(store=store, auth=auth, practice=True)
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/desk"
        stop = threading.Event()
        worker = threading.Thread(target=loop, args=(store, stop), daemon=True)
        worker.start()
        print(f"Practice workspace (fictional files only)\n{url}", flush=True)
        for token, p in tokens.items():
            print(("Reviewer code" if p.reviewer else "Researcher code") + "  " + token, flush=True)
        print(
            "Start with the researcher code. Under 'Bring a file into this matter', fetch Side letter.txt.\n"
            "Follow the steps in the browser. Press Ctrl+C here to end the practice session.",
            flush=True,
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

        # Wait for the listener before opening the user's browser.
        def launch():
            while not server.started:
                if stop.wait(0.1):
                    return
            webbrowser.open(url)

        threading.Thread(target=launch, daemon=True).start()
        try:
            server.run(sockets=[sock])
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            worker.join(timeout=5)
