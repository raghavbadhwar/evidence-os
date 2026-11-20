import json
import subprocess
import time
from pathlib import Path

import pytest

from proofcounsel_ingest.evidence.codex_models import CodexSol
from proofcounsel_ingest.evidence.providers import ProviderFailure


@pytest.mark.parametrize("state", ["ok", "blocked", "tool", "failed", "expired"])
def test_hosted_sol_dispatch_and_failures(monkeypatch, state):
    monkeypatch.setenv("EVIDENCE_LOCAL_ONLY", "true" if state == "blocked" else "false")
    monkeypatch.setenv("EVIDENCE_OLLAMA_MODEL", "unused")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setattr("shutil.which", lambda executable: "/bin/codex")
    called = []

    def execute(cmd, **kw):
        called.append(True)
        assert cmd[cmd.index("-m") + 1] == "gpt-6.2-sol"
        assert 'model_provider="openai"' in cmd and "--oss" not in cmd
        assert "OPENAI_BASE_URL" not in kw["env"]
        assert Path(kw["cwd"]).is_dir() and kw["timeout"] > 0
        assert cmd[-1] == "-" and "original sentence" in kw["input"]
        assert "original sentence" not in str(cmd)
        dest = Path(cmd[cmd.index("-o") + 1])
        dest.write_text('{"answer":"found"}')
        item = {"type": "command_execution" if state == "tool" else "agent_message"}
        events = [
            {"type": "item.completed", "item": {"type": "error", "message": "Skill index warning."}},
            {"type": "item.completed", "item": item},
            {"type": "turn.completed", "usage": {}},
        ]
        return subprocess.CompletedProcess(cmd, int(state == "failed"), "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr(subprocess, "run", execute)
    provider = CodexSol()
    args = (
        "read",
        {},
        "Read only.",
        {"passages": [{"id": "one", "text": "original sentence"}]},
        time.monotonic() + (-1 if state == "expired" else 30),
    )
    if state == "ok":
        assert json.loads(provider.structured(*args)) == {"answer": "found"}
    else:
        with pytest.raises(ProviderFailure):
            provider.structured(*args)
    assert bool(called) == (state not in {"blocked", "expired"})


def test_default_app_requires_hosted_consent_even_with_saved_model_settings(tmp_path, monkeypatch):
    import hashlib
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from benchmarks.first_time_notice import SIDE, ask, load
    from proofcounsel_ingest.evidence.api import create_app
    from proofcounsel_ingest.evidence.contracts import Principal
    from proofcounsel_ingest.evidence.store import Store

    monkeypatch.setenv("EVIDENCE_LOCAL_ONLY", "false")
    monkeypatch.setenv("EVIDENCE_OLLAMA_MODEL", "old-setting:1")
    monkeypatch.setattr(CodexSol, "structured", lambda *args: pytest.fail("unconsented model dispatch"))
    store = Store(tmp_path)
    user = Principal(tenant=uuid4(), actor=uuid4(), reviewer=True)
    auth = {hashlib.sha256(b"fixture").hexdigest(): user}
    with TestClient(create_app(store, auth), headers={"Authorization": "Bearer fixture"}) as client:
        base, _ = load(client, store, SIDE, allow_hosted=False)
        review = ask(client, base, "Alder Services", "2026-03-01")
        assert review["provider_versions"] == CodexSol().version
        assert review["status"] == "unresolved"
        assert review["metrics"]["reasoning_calls"] == 0
