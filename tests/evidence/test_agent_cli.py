"""Exercise the practice launcher and agent commands as separate OS processes."""

import json
import os
import select
import signal
import subprocess
import sys
import time

import httpx


def test_local_agent_and_practice_desk(tmp_path):
    command = [sys.executable, "-m", "proofcounsel_ingest.evidence.agent_cli"]
    server = subprocess.Popen(
        [*command, "demo"],
        env=dict(os.environ, BROWSER=sys.executable + " -V %s"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([server.stdout], [], [], 20)[0]
        assert "fictional" in server.stdout.readline()
        url = server.stdout.readline().strip()
        editor, reviewer = [server.stdout.readline().split()[-1] for _ in range(2)]
        origin = url.removesuffix("/desk")
        for _ in range(100):
            try:
                assert 'data-practice="true"' in httpx.get(url, timeout=1, trust_env=False).text
                break
            except httpx.ConnectError:
                time.sleep(0.05)

        def cli(*args, who=editor, success=True):
            r = subprocess.run(
                [*command, *args],
                text=True,
                capture_output=True,
                timeout=20,
                env=dict(os.environ, EVIDENCE_AGENT_TOKEN=who, EVIDENCE_API_URL=origin),
            )
            assert (r.returncode == 0) == success, r.stderr
            return json.loads(r.stdout if success else r.stderr)

        mid = cli("matters")[0]["id"]
        assert cli("query", mid, "XN-125", "--mode", "reference")["status"] == "unresolved"
        review = cli("notice-review", mid, "Alder Services", "convenience", "--as-of", "2026-03-01")
        assert review["status"] == "unresolved" and review["service"]["clock_started"] is None
        assert review["request"]["notice_review"] == {"party": "Alder Services", "route": "convenience"}
        q = "What does the C-7 side letter say about A?"
        assert cli("gap", mid, q)["status"] == "open"
        assert "Side letter.txt" in cli("sources", mid)
        assert cli("fetch", mid, "Side letter.txt")["status"] == "ready"
        v = cli("query", mid, "C-7", "--mode", "reference")
        span = next(s for s in v["evidence"] if s["filename"] == "Side letter.txt")
        assert cli("quote", mid, span["id"])["evidence"] == [span]
        dest = tmp_path / "letter.txt"
        assert cli("open", mid, span["document_id"], str(dest))["sha256"] == span["source_sha256"]
        assert dest.read_text() == span["text"]
        assert "error" in cli("open", mid, span["document_id"], str(dest), success=False)
        record = cli(
            "learn",
            mid,
            q,
            "--snapshot",
            v["snapshot"],
            "--answer",
            "A retains 30 days for convenience termination.",
            "--cite",
            span["id"],
        )
        assert cli("query", mid, q)["answer"] is None
        decision = [
            "review",
            mid,
            record["id"],
            "approve",
            "--reason",
            "Checked the signed letter and the cited party.",
        ]
        assert cli(*decision, success=False)["status"] == 403
        assert cli(*decision, who=reviewer)["status"] == "approved"
        assert cli("query", mid, q)["status"] == "reviewed"
        assert {n["status"] for n in cli("inbox", mid)["entries"]} == {"resolved", "approved"}
    finally:
        server.send_signal(signal.SIGINT)
        server.communicate(timeout=15)
    assert server.returncode == 0


def test_connect_installs_shared_project_skill_and_preserves_existing_files(tmp_path):
    command = [sys.executable, "-m", "proofcounsel_ingest.evidence.agent_cli", "connect"]
    env = {k: v for k, v in os.environ.items() if k != "EVIDENCE_AGENT_TOKEN"}

    def connect(agent, ok=True):
        run = subprocess.run([*command, agent, "--workspace", str(tmp_path)], capture_output=True, text=True, env=env)
        assert (run.returncode == 0) == ok, run.stderr
        return json.loads(run.stdout if ok else run.stderr)

    codex = tmp_path / ".agents/skills/evidence-os/SKILL.md"
    claude = tmp_path / ".claude/skills/evidence-os/SKILL.md"
    assert connect("codex")["skill"] == str(codex)
    assert connect("claude")["skill"] == str(claude)
    assert codex.read_bytes() == claude.read_bytes()
    assert connect("claude")["status"] == "already_installed"
    claude.write_text("Firm-authored instructions.")
    assert "error" in connect("claude", False)
    assert claude.read_text() == "Firm-authored instructions."
