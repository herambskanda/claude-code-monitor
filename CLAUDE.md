# Device setup instructions (for Claude Code)

When the user asks you to "set this up", "check readme and set it up for heramb" or similar, set up the CCM agent on THIS
device and produce one file to send to Heramb. Do it end to end without asking questions.
The agent is `agent/ccm_agent.py` in this repo: one Python file, standard library only. It reads Claude Code's local
conversation metadata (titles, project paths, token counts, tool names, timestamps, account email/plan) and writes it to
a single file. It never reads or sends prompt/response text, file contents or credentials, and it sends nothing over the
network unless a server is configured (none is by default). Do not read the user's transcripts yourself and do not change
anything outside this repo, `~/.ccm`, and the Claude `commands/` folders.

## Steps (use `py -3` instead of `python3` on Windows)

1. **Python 3.8+.** Run `python3 --version`. If missing: Windows `winget install -e --id Python.Python.3.12` (then use
   `py -3`); Debian/Ubuntu `sudo apt-get install -y python3`; macOS `xcode-select --install` or `brew install python`.
2. **Install the agent:** `python3 agent/ccm_agent.py install --label "<short name of this device>"`
   Use the hostname or something descriptive like "alice-laptop". This copies the agent to `~/.ccm` and adds a
   `/ccm-collect` slash command to every Claude config dir (so it can be re-run any time by typing `/ccm-collect`).
3. **Collect and create the file:** `python3 agent/ccm_agent.py share`
   Large histories can take a few minutes; that is normal. It prints `SHARE FILE: <path>` (on the Desktop, or the home
   folder if there is no Desktop) and opens the folder where possible.
4. **Tell the human, in 3 short lines:** what was installed, the exact path of the file, and:
   "Send this one file to Heramb on WhatsApp (or email). Whenever he asks again, open Claude Code and type
   `/ccm-collect`, then send the new file." Then stop. A later file is always complete, so only the newest one is needed.

Re-running these steps is safe (idempotent).

## Optional (only if Heramb says there is a server)
`SERVER_URL` in the repo root may list a server address. If it does, and the device can reach it, add `--schedule` to
step 2 and use `python3 agent/ccm_agent.py collect` in step 3 to upload directly instead of making a file.
