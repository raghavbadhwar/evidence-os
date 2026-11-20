# Set up Evidence OS with Codex or Claude Code

Open the extracted Quickstart folder or repository as a project in **Codex** or **Claude Code**. Copy the whole prompt below into that agent. It prepares the fictional practice installation and connects the agent you are using.

```text
Set up Evidence OS for me in this project. Carry out the steps and check the results.

1. Find the Start Evidence OS launcher in the current project. If it is missing, ask me to open the extracted Quickstart folder. Use this folder as the project workspace.
2. Run the check-only doctor from that folder. On Mac or Linux run sh "Start Evidence OS.command" doctor. On Windows PowerShell run & ".\Start Evidence OS.cmd" doctor. If the practice installation needs repair, rerun that command with --fix. Let me answer any installer consent prompt. Continue when Doctor reports READY. If it still fails, tell me the first failed check and what I can do about it.
3. Use the CLI that Doctor prepared. Its path is ./.evidence-practice-venv/bin/evidence-agent on Mac or Linux and .\.evidence-practice-venv\Scripts\evidence-agent.exe on Windows. Run it with --help, then with "connect codex --workspace ." if you are Codex or "connect claude --workspace ." if you are Claude Code. Pass each argument separately. Preserve any existing customized skill if the installer reports a conflict.
4. Verify that connect reports installed or already_installed and that the SKILL.md at its returned path exists. Report the client, Doctor result and skill path. This is a local setup check. Leave credentials, model settings and client files alone.
5. Tell me which Start launcher opens the fictional matter, and how to start a fresh session here to use the skill. Give me the full CLI path for that session. If I want the agent to query a matter, explain how IT supplies the workspace address and a scoped credential through its environment. Never ask me to paste an access code into chat.
```

**After setup**, open a fresh agent session in the same project. For a configured practice connection, try one of these prompts. If the CLI is outside the agent shell’s PATH, include the full CLI path from the setup report.

- **Codex** — `$evidence-os Find the signed side letter in the fictional Alder matter and quote the passage about Subsidiary A.`
- **Claude Code** — `/evidence-os Find the signed side letter in the fictional Alder matter and quote the passage about Subsidiary A.`

A working matter connection needs both `EVIDENCE_API_URL` and `EVIDENCE_AGENT_TOKEN` in the agent’s environment. For the practice server, use the origin printed by Start (the address before `/desk`) and the researcher identity. Use the local browser walkthrough if you only want to try the product. For client matters, use the firm-approved workspace and agent model endpoint.
