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

## Setup (once)

**Main PC.** Install Tailscale, sign in, then:
```
python3 server/ccm_server.py serve --port 8787     # uploads from Tailscale addresses need no token
echo "http://<this-pc-tailscale-ip>:8787" > SERVER_URL   # commit + push so devices can find the server
```
By default only Tailscale (`100.64.0.0/10`) and loopback may upload without a token. Use `--allow 192.168.0.0/16` to also
allow your LAN, or `--require-token` to demand the bearer token (printed at start) from everyone.

**Each device.** Put Tailscale on it (same tailnet), clone this repo, open Claude Code in it and say
*"check readme and set it up for heramb"*. Claude follows [CLAUDE.md](CLAUDE.md): it installs the agent, adds the
`/ccm-collect` slash command to every Claude config dir (`~/.claude`, `~/.claude-*`, `$CLAUDE_CONFIG_DIR`), schedules a run
every 2 hours (cron / Task Scheduler) and does the first upload. Manual equivalent:
`python3 agent/ccm_agent.py install --schedule --label my-laptop` (Windows: `py -3`).

**Without Tailscale or a published SERVER_URL:** `python3 server/ccm_server.py bundle --server http://host:8787` writes
`dist/ccm_agent.py` with the URL and token baked in (private: it contains the token). Or use the file route below.

## Daily use

On any device type `/ccm-collect` in Claude Code (or `python3 ~/.ccm/ccm_agent.py`). It reads new transcript data
only (a repeat run takes about a second), writes `~/.ccm/outbox/ccm-<device>-<time>.ndjson.gz` and POSTs it to the first
reachable server. If your PC is off the file stays in `outbox/` and goes out on the next run, so nothing is lost.

No network path? `python3 ccm_agent.py export` prints the file path; copy it any way you like and run
`python3 server/ccm_server.py import <files or folder>` on the main PC. Imports are idempotent.

Devices on different networks need *some* route to the main PC. Same LAN (Proxmox VMs): nothing to do. Remote devices:
a private mesh such as Tailscale (peer to peer, no hosting) or self-hosted WireGuard/Headscale. The token travels in
clear over plain HTTP, so use it over LAN or a VPN only.

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
