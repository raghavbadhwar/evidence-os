# Start your first Evidence OS matter

Use the fictional Alder contract to see how a side letter changes a lawyer’s answer. The session runs on your computer. You need a browser and internet access for the first setup. You can do the whole exercise without a Codex account.

## Open the desk

1. On GitHub, choose **Code → Download ZIP**. If someone sent you the Quickstart ZIP, use that. Extract the **whole folder** somewhere you can write, such as Downloads.
2. Open the extracted folder. On **Mac**, double-click **Start Evidence OS.command**. On **Windows**, double-click **Start Evidence OS.cmd**. On **Linux**, open a terminal in the folder and run `sh "Start Evidence OS.command"`.
3. If the launcher offers to install **uv**, choose **Y** if your computer permits it. This tool sets up Python 3.12 and the required packages. On a managed device, IT can install uv through its normal software channel first. [Official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
4. Leave the launcher window open. It shows a browser address, a **Researcher code** and a **Reviewer code**. Copy the Researcher code, paste it into the browser and click **Open my matters**. If the browser stays closed, open the printed address on this computer.

## Complete one review

The **Your first matter** panel points to the next action. Keep the prefilled question, search mode and date during the exercise.

1. Click **Fetch file** to add the preselected **Side letter.txt**.
2. Click **Search this matter**. Under **Passages to check**, use **Open original** to inspect the amendment and side letter. Tick **Use this passage** for the sources supporting your conclusion.
3. Write a proposed answer and click **Send answer for review**. Check your reading against the packet. On 1 March 2026 the convenience route requires 30 days for A and 45 for B. The packet leaves the date of service unresolved.
4. Click **Close workspace**, sign in with the **Reviewer code**, and expand the pending answer under **Open questions and saved answers**. Click **Read supporting passages**, enter the reason for your decision and approve or decline.
5. After approval, click **Search this matter** again. You should see **Reviewed answer**, the answer you approved and its supporting passages.

Press **Ctrl+C** in the launcher window to finish. A normal shutdown clears the fictional matter. Reopen the launcher to practise again. The installed dependencies are reused.

## Check or repair the installation

Close the running demo. Double-click **Doctor Evidence OS.command** on Mac or **Doctor Evidence OS.cmd** on Windows. On Linux, run `sh "Doctor Evidence OS.command"`. The doctor checks the extracted files, Python 3.12, package versions, app and document readers, temporary database storage and the local browser connection. If uv is missing it offers to install it. It repairs the practice software and repeats the checks. When you see **READY**, reopen **Start Evidence OS**.

To inspect without installing or repairing anything, run `sh "Start Evidence OS.command" doctor` on Mac/Linux, or `.\Start Evidence OS.cmd doctor` on Windows. Add `--fix` to repair. Agents using the editable Quickstart installation can run `evidence-agent doctor` or `evidence-agent doctor --fix`. A failed check exits with status 1.

Repairs apply to this extracted practice folder. If its Python environment is damaged, Doctor preserves the old environment in a folder named `.evidence-practice-venv.backup-…` and builds another. For missing release files, extract another copy of the ZIP. If downloads, disk access or loopback connections are blocked, give IT the failed check. Firm server configuration, login and hosted Sol are separate from this check.

## If you get stuck

| What you see | What to do |
| --- | --- |
| A security or software-installation policy blocks the launcher | Give IT the extracted folder and the message. IT can use its approved uv installation and run the command below. |
| Python or package download fails | Check the network and ask IT about its approved package sources. Keep the full error message, excluding the practice codes. Then reopen the launcher. |
| Access code rejected | Copy the code again from the window that printed this browser address. A new session issues new codes. |
| No checkboxes beside the passages | Under **Look for**, choose **Reviewed answer or related passages**, then search again. |
| A colleague cannot open your browser address | The address belongs to the computer running the demo. Send the ZIP for a separate practice session. |

IT can also launch from the extracted folder with its installed copy of uv:

```sh
uv run --locked --python 3.12 --no-dev --group evidence --group pdf --group office evidence-agent demo
```

For client work, use the firm’s approved shared deployment and individual accounts. The practice launcher uses fictional files, temporary storage and local search. Hosted Codex Sol inference requires separate firm approval.
