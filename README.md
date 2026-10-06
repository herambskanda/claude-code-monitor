# CCM: Claude Code usage collector

> **Claude Code: if the user asks you to "set this up" / "set it up for heramb", follow [CLAUDE.md](CLAUDE.md) exactly.**

Collects Claude Code usage from every device (macOS, Linux, Windows, WSL, Proxmox VMs) into one SQLite database on your main PC.
Python 3.8+, standard library only. No cloud, no hosting.

```
agent/ccm_agent.py    runs on each device (reads local transcripts, writes + sends one export file)
server/ccm_server.py  runs on your main PC (receives uploads, imports files, builds the per-device agent, reports)
server/ccm_calc.py    cost per call, 5h / weekly windows, capacity calibration, per-conversation %
tests/                python3 -m unittest discover -s tests
```

## Setup (once per device) - no accounts, no network setup

On each device, in any folder, open Claude Code and say:

> Clone https://github.com/herambskanda/claude-code-monitor, read its CLAUDE.md and set it up for heramb.

Claude installs the agent (`~/.ccm`, plus a `/ccm-collect` slash command in every Claude config dir) and writes **one
file** (`ccm-<device>-<date>.ndjson.gz`, a few MB) to the Desktop. Send that file to the main PC by WhatsApp, email, USB,
whatever. Whenever you want fresh data, type `/ccm-collect` on that device and send the new file. Each file is a complete
snapshot, so only the newest one per device is needed, and importing the same file twice is harmless.
Manual equivalent: `python3 agent/ccm_agent.py install --label my-laptop` then `python3 agent/ccm_agent.py share`
(Windows: `py -3`).

**Main PC:** save the received files in one folder and import them:
```
python3 server/ccm_server.py import ~/Downloads/ccm-*.ndjson.gz
python3 server/ccm_server.py dashboard --open
```

### Optional: automatic upload over a private network
If you ever want devices to push on a schedule: run `python3 server/ccm_server.py serve` on the main PC, put the
Tailscale (or LAN) URL in `SERVER_URL`, and have each device `install --schedule`. By default only Tailscale
(`100.64.0.0/10`) and loopback may upload without a token; `--allow CIDR` adds networks and `--require-token` demands the
bearer token. `server/ccm_server.py bundle --server URL` writes `dist/ccm_agent.py` with URL and token baked in
(private: it contains the token). This needs Tailscale (or similar) signed in on every device, so it is not the default.

## On the main PC

```
python3 server/ccm_server.py report                     # accounts, calibration, top conversations by % of a 5h window
python3 server/ccm_server.py set-capacity you@x.com --cap5h 300 --cap7d 2000   # optional manual capacity ($ API-equivalent)
```
Data is in `server/data/ccm.db` (tables: devices, accounts, device_cfgs, sessions, calls, util_snapshots, price_table,
account_overrides, account_capacity). Phase 2 (dashboard, filters, spike detection) reads this database.

## What is collected

Per conversation: title, project (cwd, branch, worktree), entrypoint, version, first/last time, active time, prompt count,
API calls, tool counts, MCP server/tool counts, subagents, tokens per model (in/out/cache read/cache write 5m+1h/thinking),
peak and average context, compactions, Claude Code's own cost total, and the account. Per API call: time, model, tokens,
context size, tools. Per run: Claude Code's cached 5h/weekly utilization snapshot.
**Never**: prompt or response text (only a 60-character label when a conversation has no title), file contents, tool
inputs, credentials. Account fields are whitelisted (uuid, email, org, plan).

## Things to know

- **Accounts.** Transcripts do not record the account. Each conversation gets the account logged in for its config dir
  when the agent first saw it (`account_conf`: `observed`, `assumed` for history that pre-dates the install, or
  `ambiguous` if the login changed since the previous run). Fix mistakes with rows in `account_overrides`.
- **Percentages are estimates.** Anthropic does not publish limits. Capacity is calibrated from Claude Code's cached
  utilization snapshots (one is stored per run, so accuracy grows with runs). Calibration is a lower bound until every device
  that uses the account has uploaded, because usage elsewhere inflates the real meter; claude.ai chat usage also counts and
  is invisible here. Use `set-capacity` if you know better.
- **Output tokens** in transcripts are about 5% below Claude Code's own totals (streaming records keep partial output
  counts); cache tokens match exactly. Claude Code's own per-session cost is exported next to them.
- **History.** Claude Code deletes transcripts after about 30 days. The agent keeps its own archive in `~/.ccm/archive.sqlite`,
  so history survives from the first run onward.
- The transcript prefilter assumes Claude Code's compact JSON lines (`"type":"assistant"`).
