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

## Setup (once per device) - works with any GitHub account (or none)

**One-time on the main PC** (about 5 minutes): create a PRIVATE GitHub repo for the data (e.g. `claude-code-monitor-data`)
and a fine-grained personal access token limited to that repo with *Contents: Read and write*
(GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens). Then:
```
python3 server/ccm_server.py github-init --repo <you>/claude-code-monitor-data     # asks for the token, prints a JOIN CODE
```
**On each device**, in any folder, open Claude Code and say (helpers do not need a GitHub account; the join code carries
the token and an encryption passphrase, and the agent never uses the device's own git/GitHub login):

> Clone https://github.com/<you>/claude-code-monitor, read its CLAUDE.md and set it up for <name>. Join code: ccm1....

Claude installs the agent (`~/.ccm`, plus a `/ccm-collect` slash command in every Claude config dir), schedules a check
every 10 minutes and does the first upload. Each upload is a complete snapshot, encrypted on the device (scrypt +
HMAC-SHA256 stream cipher, standard library only) and force-pushed as a single parentless commit to its own branch
`device/<label>-<id>` of the data repo, so the repo never grows.

**On demand, from the main PC:**
```
python3 server/ccm_server.py request                 # all devices upload within ~10 minutes (or --device NAME)
python3 server/ccm_server.py pull                    # fetch, decrypt and import new uploads (--watch 60 to loop)
python3 server/ccm_server.py request --auto-hours 24 --no-trigger    # optional: devices also upload daily by themselves
python3 server/ccm_server.py dashboard --open
```
Revoke access any time by deleting the token in GitHub; change the passphrase with `github-init --passphrase ...` and
re-issue the join code. Treat the join code like a password: it grants read/write on the data repo.

**No GitHub at all:** omit the join code. Claude then runs `share`, which writes ONE complete file
(`ccm-<device>-<date>.ndjson.gz`) to the Desktop for you to send by WhatsApp/email. Import with
`python3 server/ccm_server.py import <files or folder>`; newer files from the same device just update the older data.

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
