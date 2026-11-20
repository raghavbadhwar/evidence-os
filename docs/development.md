# Develop and package Evidence OS

The application lives in `src/proofcounsel_ingest/evidence/`. The browser desk and the agent CLI call the same matter-scoped API. The surrounding `proofcounsel_ingest` package handles parsing and retrieval. The product snapshot originated at Claims OS commit `6a7e69f4a6766595ce7ee667593320ab3fd9dec0`.

## Local checks

From the repository root with uv available, run the practice and agent regression checks:

```sh
uv run --locked --python 3.12 --group evidence-test --group pdf --group office pytest -q tests/evidence/test_practice_doctor.py tests/evidence/test_agent_cli.py
```

For an interactive exercise, use the root Start launcher. For the persistent workspace, follow the [administrator guide](matter-desk.md#for-an-administrator). The disposable practice exercise uses local search. Hosted review requires the firm’s approval and a separately configured Codex login.

## Package a release

From a clean, committed checkout, run:

```sh
mkdir -p dist
python3 scripts/package_practice.py dist
```

The packager reads the committed Git tree and creates a versioned ZIP with the launchers, setup guide, source and dependency lock. Extract it into a new folder and run Doctor, then complete the researcher-to-reviewer walkthrough. Attach the verified archive to the corresponding private release as `Evidence-OS-Quickstart.zip`.

Report setup issues with the operating system, release tag and first failed Doctor check. Keep access codes and client material in the firm’s approved environment.
