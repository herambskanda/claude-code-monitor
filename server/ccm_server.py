#!/usr/bin/env python3
"""CCM server (runs on your main PC). Python 3.8+, stdlib only.

  ccm_server.py serve  [--port 8787] [--host 0.0.0.0] [--data DIR]   receive agent uploads
  ccm_server.py import FILE_OR_DIR ...                                load export files by hand
  ccm_server.py bundle --server URL [--server URL2] [--label NAME]   write a pre-configured agent
  ccm_server.py report                                                quick usage report (5h/weekly %)
  ccm_server.py token                                                 print the upload token

Data lives in DIR (default ./data next to this script): ccm.db, inbox/, token.
"""
import argparse
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccm_calc  # noqa: E402

SERVER_VERSION = "1.0.0"
SCHEMA_VERSION = 1
MAX_BODY = 512 * 1024 * 1024
# Uploads without a token are accepted only from these networks (Tailscale already authenticates devices).
DEFAULT_ALLOW = ["100.64.0.0/10", "fd7a:115c:a1e0::/48", "127.0.0.0/8", "::1/128"]


def parse_nets(cidrs):
    return [ipaddress.ip_network(c, strict=False) for c in cidrs]


def ip_allowed(ip, nets):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in nets if a.version == n.version)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS devices(
  device_id TEXT PRIMARY KEY, machine_id TEXT, label TEXT, hostname TEXT, os TEXT, os_release TEXT,
  tz_offset_min INTEGER, agent_version TEXT, python TEXT, first_seen TEXT, last_seen TEXT, last_file TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  account_uuid TEXT PRIMARY KEY, email TEXT, org_uuid TEXT, org_name TEXT, org_type TEXT,
  rate_limit_tier TEXT, billing_type TEXT, display_name TEXT, org_role TEXT, nickname TEXT);
CREATE TABLE IF NOT EXISTS device_cfgs(
  device_id TEXT, cfg TEXT, account_uuid TEXT, updated TEXT, PRIMARY KEY(device_id, cfg));
CREATE TABLE IF NOT EXISTS sessions(
  device_id TEXT, sid TEXT, cfg TEXT, title TEXT, title_kind TEXT, project TEXT, cwd TEXT, branch TEXT,
  worktree INTEGER, entrypoint TEXT, version TEXT, first_ts TEXT, last_ts TEXT, first_epoch REAL,
  last_epoch REAL, active_s INTEGER, prompts INTEGER, calls INTEGER, web_requests INTEGER, tools_json TEXT,
  mcp_json TEXT, subagents_json TEXT, tokens_json TEXT, ctx_peak INTEGER, ctx_avg INTEGER,
  compactions_json TEXT, cost_json TEXT, account_uuid TEXT, account_conf TEXT, updated TEXT,
  PRIMARY KEY(device_id, sid));
CREATE TABLE IF NOT EXISTS calls(
  device_id TEXT, sid TEXT, sub TEXT, mid TEXT, ts TEXT, ts_epoch REAL, model TEXT, inp INTEGER, out INTEGER,
  cr INTEGER, cw5 INTEGER, cw1 INTEGER, think INTEGER, ctx INTEGER, web INTEGER, speed TEXT, effort TEXT,
  tools_json TEXT, PRIMARY KEY(device_id, sid, sub, mid));
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts_epoch);
CREATE TABLE IF NOT EXISTS util_snapshots(
  fetched_ms INTEGER, account_uuid TEXT, device_id TEXT, raw_json TEXT, PRIMARY KEY(fetched_ms, account_uuid));
CREATE TABLE IF NOT EXISTS price_table(
  pattern TEXT PRIMARY KEY, in_p REAL, out_p REAL, cr_p REAL, cw5_p REAL, cw1_p REAL, note TEXT);
CREATE TABLE IF NOT EXISTS account_overrides(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_uuid TEXT, device_id TEXT, project_like TEXT,
  from_ts TEXT, to_ts TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS account_capacity(
  account_uuid TEXT PRIMARY KEY, cap_5h REAL, cap_7d REAL, note TEXT);
CREATE TABLE IF NOT EXISTS imports(
  sha256 TEXT PRIMARY KEY, filename TEXT, received_at TEXT, records INTEGER, device_id TEXT);
"""


def data_dir(arg):
    d = Path(arg or os.environ.get("CCM_DATA") or (Path(__file__).resolve().parent / "data"))
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    return d


def open_db(d):
    conn = sqlite3.connect(str(d / "ccm.db"), timeout=30)
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    n = conn.execute("SELECT count(*) FROM price_table").fetchone()[0]
    if n == 0:
        with conn:
            conn.executemany("INSERT INTO price_table VALUES(?,?,?,?,?,?,?)", ccm_calc.DEFAULT_PRICES)
    return conn


def get_token(d):
    p = d / "token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    t = secrets.token_urlsafe(24)
    p.write_text(t, encoding="utf-8")
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    return t


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- ingest

def j(v):
    return json.dumps(v, separators=(",", ":")) if v is not None else None


def ingest_records(conn, records, filename):
    """Upsert one export file. Idempotent: sessions are replaced (they carry full aggregates),
    calls and snapshots are keyed, so overlapping exports never double count."""
    meta = next((r for r in records if r.get("rec") == "meta"), None)
    if not meta or meta.get("schema") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing meta/schema (expected schema %d)" % SCHEMA_VERSION)
    dev = meta["device"]
    did = dev["device_id"]
    counts = {"sessions": 0, "calls": 0, "util": 0}
    with conn:
        conn.execute("INSERT OR IGNORE INTO devices(device_id,first_seen) VALUES(?,?)", (did, now_iso()))
        conn.execute(
            "UPDATE devices SET machine_id=?,label=?,hostname=?,os=?,os_release=?,tz_offset_min=?,agent_version=?,"
            "python=?,last_seen=?,last_file=? WHERE device_id=?",
            (dev.get("machine_id"), dev.get("label"), dev.get("hostname"), dev.get("os"), dev.get("os_release"),
             dev.get("tz_offset_min"), dev.get("agent_version"), dev.get("python"), now_iso(), filename, did))
        for r in records:
            t = r.get("rec")
            if t == "account":
                conn.execute("INSERT OR IGNORE INTO accounts(account_uuid) VALUES(?)", (r["account_uuid"],))
                conn.execute(
                    "UPDATE accounts SET email=?,org_uuid=?,org_name=?,org_type=?,rate_limit_tier=?,billing_type=?,"
                    "display_name=?,org_role=? WHERE account_uuid=?",
                    (r.get("email"), r.get("org_uuid"), r.get("org_name"), r.get("org_type"),
                     r.get("rate_limit_tier"), r.get("billing_type"), r.get("display_name"), r.get("org_role"),
                     r["account_uuid"]))
                conn.execute("INSERT OR REPLACE INTO device_cfgs VALUES(?,?,?,?)",
                             (did, r.get("cfg"), r["account_uuid"], now_iso()))
            elif t == "session":
                cost = r.get("cost")
                conn.execute(
                    "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, r["id"], r.get("cfg"), r.get("title"), r.get("title_kind"), r.get("project"),
                     r.get("cwd"), r.get("branch"), 1 if r.get("worktree") else 0, r.get("entrypoint"),
                     r.get("version"), r.get("first_ts"), r.get("last_ts"), ccm_calc.ts_epoch(r.get("first_ts")),
                     ccm_calc.ts_epoch(r.get("last_ts")), r.get("active_s"), r.get("prompts"), r.get("calls"),
                     r.get("web_requests"), j(r.get("tools")), j(r.get("mcp")), j(r.get("subagents")),
                     j(r.get("tokens")), r.get("ctx_peak"), r.get("ctx_avg"), j(r.get("compactions")), j(cost),
                     r.get("account_uuid"), r.get("account_conf"), now_iso()))
                counts["sessions"] += 1
            elif t == "call":
                e = ccm_calc.ts_epoch(r.get("ts"))
                if e is None:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, r["sid"], r.get("sub") or "", r["mid"], r["ts"], e, r.get("model"), r.get("inp", 0),
                     r.get("out", 0), r.get("cr", 0), r.get("cw5", 0), r.get("cw1", 0), r.get("think", 0),
                     r.get("ctx", 0), r.get("web", 0), r.get("speed"), r.get("effort"), j(r.get("tools") or [])))
                counts["calls"] += 1
            elif t == "util":
                body = {k: v for k, v in r.items() if k != "rec"}
                conn.execute("INSERT OR IGNORE INTO util_snapshots VALUES(?,?,?,?)",
                             (body["fetched_ms"], body.get("account_uuid") or "", did, j(body)))
                counts["util"] += 1
    return did, counts


def ingest_bytes(conn, data, filename, d=None):
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT filename FROM imports WHERE sha256=?", (sha,)).fetchone()
    if row:
        return {"status": "duplicate", "file": row[0]}
    try:
        text = gzip.decompress(data).decode("utf-8") if data[:2] == b"\x1f\x8b" else data.decode("utf-8")
        records = [json.loads(l) for l in text.splitlines() if l.strip()]
    except (OSError, ValueError, EOFError) as e:
        raise ValueError("cannot read export: %s" % e)
    did, counts = ingest_records(conn, records, filename)
    with conn:
        conn.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (sha, filename, now_iso(), len(records), did))
    if d is not None:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:80] or "export.ndjson.gz"
        (d / "inbox" / ("%s-%s" % (sha[:8], safe))).write_bytes(data)
    counts["status"] = "ok"
    counts["device_id"] = did
    return counts


# --------------------------------------------------------------------------- commands

class Handler(BaseHTTPRequestHandler):
    server_version = "ccm/" + SERVER_VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "version": SERVER_VERSION})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ingest":
            return self._json(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if auth:  # explicit credentials must be right
            ok = hmac.compare_digest(auth, "Bearer " + self.server.token)
        else:     # no credentials: only from allowed networks (default: Tailscale + loopback)
            ok = (not self.server.require_token) and ip_allowed(self.client_address[0], self.server.allowed_nets)
        if not ok:
            return self._json(401, {"error": "unauthorized (token required or address not allowed)"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json(413, {"error": "bad length"})
        data = self.rfile.read(n)
        try:
            conn = open_db(self.server.data)
            try:
                res = ingest_bytes(conn, data, self.headers.get("X-CCM-Filename") or "upload.ndjson.gz",
                                   self.server.data)
            finally:
                conn.close()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except sqlite3.Error as e:
            return self._json(500, {"error": "db: %s" % e})
        self._json(200, res)


def cmd_serve(a):
    d = data_dir(a.data)
    open_db(d).close()
    token = a.token or get_token(d)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.token, srv.data = token, d
    srv.require_token = a.require_token
    srv.allowed_nets = parse_nets(DEFAULT_ALLOW + (a.allow or []))
    print("CCM server listening on %s:%d (data: %s)" % (a.host, a.port, d))
    if a.require_token:
        print("token required for every upload: %s" % token)
    else:
        print("tokenless uploads allowed from: %s" % ", ".join(str(n) for n in srv.allowed_nets))
        print("(a bearer token is also accepted: %s)" % token)
    print("try: ccm_server.py bundle --server http://%s:%d" % (guess_ip(), a.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


def cmd_import(a):
    d = data_dir(a.data)
    conn = open_db(d)
    files = []
    for p in a.paths:
        pp = Path(p)
        files.extend(sorted(pp.glob("*.ndjson*")) if pp.is_dir() else [pp])
    for f in files:
        try:
            res = ingest_bytes(conn, f.read_bytes(), f.name, d)
        except (ValueError, OSError) as e:
            print("FAIL %s: %s" % (f, e))
            continue
        print("%s %s %s" % (res["status"], f.name, {k: v for k, v in res.items() if k in ("sessions", "calls", "util")}))
    return 0


def guess_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def cmd_bundle(a):
    d = data_dir(a.data)
    token = a.token or get_token(d)
    servers = a.server or ["http://%s:%d" % (guess_ip(), 8787)]
    src = Path(a.agent) if a.agent else Path(__file__).resolve().parent.parent / "agent" / "ccm_agent.py"
    text = src.read_text(encoding="utf-8")
    baked = {"servers": servers, "token": token}
    if a.label:
        baked["label"] = a.label
    pat = re.compile(r"(# >>> CCM-BAKED-CONFIG[^\n]*\n)BAKED_CONFIG = .*?\n(# <<< CCM-BAKED-CONFIG)", re.S)
    if not pat.search(text):
        print("agent file has no CCM-BAKED-CONFIG block: %s" % src)
        return 1
    new = pat.sub(lambda m: "%sBAKED_CONFIG = %s\n%s" % (m.group(1), json.dumps(baked), m.group(2)), text)
    out = Path(a.out) if a.out else Path(__file__).resolve().parent.parent / "dist" / "ccm_agent.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(new, encoding="utf-8")
    try:
        os.chmod(str(out), 0o600)  # it contains the token
    except OSError:
        pass
    print("wrote %s (servers: %s)" % (out, ", ".join(servers)))
    print("On each device, tell Claude Code: run `python3 %s install` (Windows: `py -3 %s install`)"
          % (out.name, out.name))
    return 0


def pct(v):
    return "  n/a" if v is None else "%5.1f%%" % v


def cmd_report(a):
    d = data_dir(a.data)
    conn = open_db(d)
    res = ccm_calc.compute(conn)
    print("calls: %d   API-equivalent total: $%.2f" % (res["calls"], res["total_usd"]))
    accts = {r[0]: r[1:] for r in conn.execute("SELECT account_uuid,email,org_type,rate_limit_tier FROM accounts")}
    print("\nAccounts / calibrated capacity (API-equivalent $ per window; estimate)")
    for acct, c in res["caps"].items():
        info = accts.get(acct) or ("?", "?", "?")
        print("  %-28s %-12s %-24s [%s] 5h cap %s (n=%d)  weekly cap %s (n=%d)" % (
            info[0], info[1], info[2], c.get("source", "?"),
            "$%.2f" % c["cap_5h"] if c["cap_5h"] else "n/a", c["n_5h"],
            "$%.2f" % c["cap_7d"] if c["cap_7d"] else "n/a", c["n_7d"]))
    print("  note: calibration is a LOWER bound until every device using the account has uploaded\n"
          "  (usage elsewhere inflates utilization), so conversation % are upper bounds until then.")
    print("\nLatest real snapshots (what Claude Code itself reported)")
    for acct, ss in res["snaps"].items():
        s = ss[-1]
        f5, f7 = s.get("five_hour"), s.get("seven_day")
        print("  %s @ %s: 5h %s%%  weekly %s%%" % (
            (accts.get(acct) or ("?",))[0], time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["fetched_s"])),
            f5["utilization"] if f5 else "n/a", f7["utilization"] if f7 else "n/a"))
    names = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT device_id,sid,title,project FROM sessions")}
    labels = {r[0]: r[1] for r in conn.execute("SELECT device_id,label FROM devices")}
    ss = [s for s in res["sessions"].values() if s["pct5_peak"] is not None]
    ss.sort(key=lambda s: -s["pct5_peak"])
    print("\nTop conversations by share of one 5h window (estimate)")
    print("  %-7s %-7s %-8s %-12s %-18s %s" % ("5h-peak", "5h-sum", "weekly", "device", "project", "title"))
    for s in ss[:a.top]:
        t, p = names.get((s["device_id"], s["sid"]), ("?", "?"))
        print("  %s %s %s  %-12s %-18s %s" % (pct(s["pct5_peak"]), pct(s["pct5_sum"]), pct(s["pct7_sum"]),
                                           (labels.get(s["device_id"]) or "?")[:12], (p or "?")[:18], (t or "")[:50]))
    return 0


def cmd_dashboard(a):
    import ccm_dashboard  # server/ccm_dashboard.py (local web dashboard)
    return ccm_dashboard.run(data_dir(a.data), a.host, a.port, a.open)


def _gh_token(a):
    import getpass
    if a.token_stdin:
        return sys.stdin.read().strip()
    if a.token or os.environ.get("CCM_GH_TOKEN"):
        return (a.token or os.environ["CCM_GH_TOKEN"]).strip()
    if not sys.stdin.isatty():
        raise SystemExit("no token: pipe it in with --token-stdin (e.g. `pbpaste | ccm_server.py github-init "
                         "--repo X --token-stdin`) or set CCM_GH_TOKEN")
    return getpass.getpass("GitHub token (hidden): ").strip()


def cmd_github_init(a):
    import ccm_gh
    d = data_dir(a.data)
    try:
        code = ccm_gh.init(d, a.repo, _gh_token(a), a.passphrase)
    except (ccm_gh.GhError, ValueError) as e:
        print("ERROR: %s" % e)
        return 1
    print("GitHub transport ready for %s.\n" % a.repo)
    print("JOIN CODE (treat like a password; send it to each helper once):\n\n%s\n" % code)
    print("Each device: open Claude Code, then say:\n  Clone https://github.com/herambskanda/claude-code-monitor, "
          "read its CLAUDE.md and set it up for heramb. Join code: <the code above>")
    return 0


def cmd_join_code(a):
    import ccm_gh
    code = ccm_gh.join_code(data_dir(a.data))
    print(code or "not configured: run github-init first")
    return 0 if code else 1


def cmd_request(a):
    import ccm_gh
    try:
        req = ccm_gh.request_collect(data_dir(a.data), a.device or None, a.auto_hours, not a.no_trigger)
    except (ccm_gh.GhError, ValueError) as e:
        print("ERROR: %s" % e)
        return 1
    print("request %s sent to %s (devices check every ~10 min; auto_hours=%s)" % (
        req.get("id"), ", ".join(a.device) if a.device else "all devices", req.get("auto_hours")))
    return 0


def cmd_pull(a):
    import ccm_gh
    d = data_dir(a.data)
    while True:
        conn = open_db(d)
        try:
            res = ccm_gh.pull_all(d, conn)
        except (ccm_gh.GhError, ValueError, OSError) as e:
            print("pull failed: %s" % e)
            res = []
        finally:
            conn.close()
        for r in res:
            if r["status"] != "unchanged":
                print("%s: %s %s" % (r["branch"], r["status"], {k: v for k, v in r.items()
                                                                 if k in ("sessions", "calls", "error")}))
        if not a.watch:
            return 0
        time.sleep(a.watch)


def cmd_set_capacity(a):
    conn = open_db(data_dir(a.data))
    row = conn.execute("SELECT account_uuid,email FROM accounts WHERE account_uuid=? OR email=? OR nickname=?",
                       (a.account, a.account, a.account)).fetchone()
    if not row:
        print("unknown account %r; known: %s" % (a.account, ", ".join(
            r[0] for r in conn.execute("SELECT email FROM accounts"))))
        return 1
    with conn:
        conn.execute("INSERT OR IGNORE INTO account_capacity(account_uuid) VALUES(?)", (row[0],))
        if a.cap5h is not None:
            conn.execute("UPDATE account_capacity SET cap_5h=? WHERE account_uuid=?", (a.cap5h or None, row[0]))
        if a.cap7d is not None:
            conn.execute("UPDATE account_capacity SET cap_7d=? WHERE account_uuid=?", (a.cap7d or None, row[0]))
    print("capacity for %s updated (0 clears the override)" % row[1])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="CCM server")
    ap.add_argument("--data", help="data dir (default ./data next to this script, or $CCM_DATA)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token")
    p.add_argument("--allow", action="append", help="extra CIDR allowed to upload without a token, e.g. 192.168.0.0/16")
    p.add_argument("--require-token", action="store_true", help="always require the bearer token")
    p = sub.add_parser("import")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("bundle")
    p.add_argument("--server", action="append", help="URL the devices use to reach this PC (repeatable)")
    p.add_argument("--label")
    p.add_argument("--token")
    p.add_argument("--agent", help="agent source (default ../agent/ccm_agent.py)")
    p.add_argument("--out", help="output file (default ../dist/ccm_agent.py)")
    p = sub.add_parser("report")
    p.add_argument("--top", type=int, default=15)
    p = sub.add_parser("github-init", help="set up the private GitHub data repo transport; prints the join code")
    p.add_argument("--repo", required=True, help="owner/name of the PRIVATE data repo")
    p.add_argument("--token", help="fine-grained token (or env CCM_GH_TOKEN, or prompt)")
    p.add_argument("--token-stdin", action="store_true", help="read the token from stdin (e.g. pbpaste |)")
    p.add_argument("--passphrase", help="encryption passphrase (default: generated)")
    sub.add_parser("join-code", help="print the join code again")
    p = sub.add_parser("request", help="ask all (or some) devices to upload fresh data now")
    p.add_argument("--device", action="append", help="device label (repeatable); default all")
    p.add_argument("--auto-hours", type=float, help="devices also upload by themselves every N hours (0 = off)")
    p.add_argument("--no-trigger", action="store_true", help="only change --auto-hours, do not request now")
    p = sub.add_parser("pull", help="fetch new device uploads from GitHub, decrypt and import")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="keep pulling every N seconds")
    p = sub.add_parser("dashboard", help="local web dashboard (127.0.0.1 only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--open", action="store_true", help="open the browser")
    p = sub.add_parser("set-capacity", help="override calibrated 5h/weekly capacity (API-equivalent $)")
    p.add_argument("account", help="email, uuid or nickname")
    p.add_argument("--cap5h", type=float)
    p.add_argument("--cap7d", type=float)
    sub.add_parser("token")
    a = ap.parse_args(argv)
    if a.cmd == "token":
        print(get_token(data_dir(a.data)))
        return 0
    return {"serve": cmd_serve, "import": cmd_import, "bundle": cmd_bundle, "report": cmd_report,
            "set-capacity": cmd_set_capacity, "dashboard": cmd_dashboard,
            "github-init": cmd_github_init, "join-code": cmd_join_code, "request": cmd_request,
            "pull": cmd_pull}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
