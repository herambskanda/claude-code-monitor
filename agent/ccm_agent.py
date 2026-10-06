#!/usr/bin/env python3
"""CCM agent: collects Claude Code usage metadata on this device.

Single file, Python 3.8+, standard library only (macOS, Linux, Windows, WSL).

  python3 ccm_agent.py install     copy to ~/.ccm and add the /ccm-collect slash command
  python3 ccm_agent.py collect     scan transcripts, write an export file, send it (default)
  python3 ccm_agent.py export      same as collect but never sends (prints the file path)
  python3 ccm_agent.py share       collect and write ONE complete file to the Desktop to send by WhatsApp/email
  python3 ccm_agent.py status      show what the agent sees and what is pending

What leaves this device: conversation titles, project paths, token counts, tool and MCP
names, timestamps, and a whitelist of account fields. No prompt/response text (other than
a 60-char fallback label when a conversation has no title), no file contents, no tool
inputs, no credentials.
"""
import argparse
import calendar
import gzip
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

AGENT_VERSION = "1.0.0"
SCHEMA = 1
GAP_ACTIVE_S = 300  # gaps shorter than this count as active time

# >>> CCM-BAKED-CONFIG (rewritten by `ccm_server.py bundle`)
BAKED_CONFIG = {}
# <<< CCM-BAKED-CONFIG

CCM_HOME = Path(os.environ.get("CCM_HOME") or (Path.home() / ".ccm"))

_TS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?")


def ts_epoch(s):
    m = _TS_RE.match(s or "")
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    frac = float("0." + m.group(7)) if m.group(7) else 0.0
    return calendar.timegm((y, mo, d, h, mi, se)) + frac


def utc_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def utc_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- config dirs

def default_claude_dir():
    return Path.home() / ".claude"


def discover_config_dirs(extra):
    home = Path.home()
    cands = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        cands.append(Path(env).expanduser())
    cands.append(home / ".claude")
    try:
        cands.extend(sorted(home.glob(".claude-*")))
    except OSError:
        pass
    cands.append(home / ".config" / "claude")
    cands.extend(Path(x).expanduser() for x in extra or [])
    out, seen = [], set()
    for c in cands:
        try:
            if not (c / "projects").is_dir():
                continue
            rp = os.path.realpath(str(c))
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        out.append(c)
    return out


def claude_json_path(cfg):
    """Where Claude Code keeps .claude.json for this config dir."""
    try:
        if os.path.realpath(str(cfg)) == os.path.realpath(str(default_claude_dir())):
            p = Path.home() / ".claude.json"
            if p.is_file():
                return p
    except OSError:
        pass
    p = cfg / ".claude.json"
    return p if p.is_file() else None


def read_json(path):
    if not path:
        return {}
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


ACCOUNT_KEYS = (
    ("accountUuid", "account_uuid"), ("emailAddress", "email"), ("organizationUuid", "org_uuid"),
    ("organizationName", "org_name"), ("organizationType", "org_type"),
    ("organizationRateLimitTier", "rate_limit_tier"), ("billingType", "billing_type"),
    ("displayName", "display_name"), ("organizationRole", "org_role"),
)


def account_info(cj):
    oa = cj.get("oauthAccount")
    if not isinstance(oa, dict):
        return None
    out = {new: oa.get(old) for old, new in ACCOUNT_KEYS}
    return out if out.get("account_uuid") else None


def compact_snapshot(cj):
    """Whitelisted copy of Claude Code's cached usage-limit snapshot (one overwritten value)."""
    c = cj.get("cachedUsageUtilization")
    if not isinstance(c, dict) or not c.get("fetchedAtMs"):
        return None
    u = c.get("utilization") or {}
    snap = {"fetched_ms": int(c["fetchedAtMs"]), "account_uuid": c.get("accountUuid")}
    for k, v in u.items():
        if k in ("five_hour", "seven_day") or k.startswith("seven_day_"):
            if isinstance(v, dict) and v.get("utilization") is not None:
                snap[k] = {"utilization": v.get("utilization"), "resets_at": v.get("resets_at")}
    bd = u.get("seven_day_breakdown")
    if isinstance(bd, dict):
        snap["breakdown"] = {"as_of": bd.get("as_of"), "window_started_at": bd.get("window_started_at"),
                             "rows": {r.get("key"): r.get("percent") for r in bd.get("rows", [])
                                      if isinstance(r, dict)}}
    ex = u.get("extra_usage")
    if isinstance(ex, dict):
        snap["extra_usage"] = {k: ex.get(k) for k in ("is_enabled", "monthly_limit", "used_credits",
                                                      "utilization", "currency")}
    return snap


# --------------------------------------------------------------------------- archive db

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY, cfg TEXT, cwd TEXT, branch TEXT, entrypoint TEXT, version TEXT,
  first_ts TEXT, last_ts TEXT, custom_title TEXT, ai_title TEXT, first_prompt TEXT,
  cost_json TEXT, account_uuid TEXT, account_conf TEXT, dirty INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS calls(
  sid TEXT, sub TEXT, mid TEXT, ts TEXT, model TEXT, inp INTEGER, out INTEGER, cr INTEGER,
  cw5 INTEGER, cw1 INTEGER, think INTEGER, ctx INTEGER, web INTEGER, speed TEXT, effort TEXT,
  tools TEXT, dirty INTEGER DEFAULT 1, PRIMARY KEY(sid, sub, mid));
CREATE INDEX IF NOT EXISTS calls_dirty ON calls(dirty);
CREATE TABLE IF NOT EXISTS events(
  uuid TEXT PRIMARY KEY, sid TEXT, kind TEXT, ts TEXT, extra TEXT);
CREATE INDEX IF NOT EXISTS events_sid ON events(sid);
CREATE TABLE IF NOT EXISTS util(
  fetched_ms INTEGER, account_uuid TEXT, raw TEXT, dirty INTEGER DEFAULT 1,
  PRIMARY KEY(fetched_ms, account_uuid));
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, off INTEGER, size INTEGER);
"""


def _tools_union(a, b):
    try:
        la = json.loads(a) if a else []
        lb = json.loads(b) if b else []
    except ValueError:
        return b or a
    seen = set()
    out = []
    for t in la + lb:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return json.dumps(out, separators=(",", ":"))


def open_db():
    CCM_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CCM_HOME / "archive.sqlite"))
    conn.create_function("ccm_union", 2, _tools_union)
    conn.executescript(SCHEMA_SQL)
    return conn


# --------------------------------------------------------------------------- transcript parsing

SKIP_PROMPT_PREFIX = ("<local-command-", "<task-notification", "<system-reminder", "[Request interrupted")
INT_FIELDS = ("inp", "out", "cr", "cw5", "cw1", "think", "ctx", "web")


def _text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
    return "\n".join(parts)


def _derive_label(text):
    t = text.strip()
    if t.startswith("<"):
        m = re.search(r"<command-name>\s*(.*?)\s*</command-name>", t, re.S)
        if m:
            args = re.search(r"<command-args>\s*(.*?)\s*</command-args>", t, re.S)
            t = (m.group(1) + " " + (args.group(1) if args else "")).strip()
        else:
            return None
    t = " ".join(t.split())
    return t[:60] or None


def _call_from(r, m, u, sid, sub):
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    cw_total = int(u.get("cache_creation_input_tokens") or 0)
    if cc and (cc.get("ephemeral_5m_input_tokens") is not None or cc.get("ephemeral_1h_input_tokens") is not None):
        cw1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
        cw_total = max(cw_total, cw1 + cw5)
    else:
        cw5, cw1 = cw_total, 0
    inp = int(u.get("input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    det = u.get("output_tokens_details") if isinstance(u.get("output_tokens_details"), dict) else {}
    stu = u.get("server_tool_use") if isinstance(u.get("server_tool_use"), dict) else {}
    tools = [b.get("name") for b in (m.get("content") or [])
             if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")]
    eff = r.get("effort")
    return {
        "sid": sid, "sub": sub, "mid": m.get("id") or r.get("uuid"), "ts": r.get("timestamp"),
        "model": m.get("model"), "inp": inp, "out": int(u.get("output_tokens") or 0), "cr": cr,
        "cw5": cw5, "cw1": cw1, "think": int(det.get("thinking_tokens") or 0), "ctx": inp + cr + cw_total,
        "web": int(stu.get("web_search_requests") or 0) + int(stu.get("web_fetch_requests") or 0),
        "speed": u.get("speed"), "effort": eff if isinstance(eff, str) else None, "tools": tools,
    }


def parse_file(path, start, file_sid, file_sub):
    """Parse complete lines from byte offset `start`. Returns (new_offset, calls, sessions, events)."""
    calls, sessions, events = {}, {}, []
    pos = start

    def sess(sid):
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = {"cwd": None, "branch": None, "entrypoint": None, "version": None,
                                 "first_ts": None, "last_ts": None, "custom_title": None,
                                 "ai_title": None, "first_prompt": None, "cost": None}
        return s

    def touch(s, r, ts, first_wins=True):
        if ts:
            if s["first_ts"] is None or ts < s["first_ts"]:
                s["first_ts"] = ts
            if s["last_ts"] is None or ts > s["last_ts"]:
                s["last_ts"] = ts
        if r.get("cwd") and not s["cwd"]:
            s["cwd"] = r["cwd"]
        if r.get("gitBranch"):
            s["branch"] = r["gitBranch"]
        if r.get("entrypoint") and not s["entrypoint"]:
            s["entrypoint"] = r["entrypoint"]
        if r.get("version"):
            s["version"] = r["version"]

    with open(str(path), "rb") as f:
        f.seek(start)
        for line in f:
            if not line.endswith(b"\n"):
                break  # partial line still being written
            pos += len(line)
            if b'"type":"assistant"' in line:
                kind = "a"
            elif b'"type":"user"' in line:
                if b'"type":"tool_result"' in line or file_sub:
                    continue
                kind = "u"
            elif b'"type":"custom-title"' in line:
                kind = "ct"
            elif b'"type":"ai-title"' in line:
                kind = "at"
            elif b'"type":"cost-state"' in line:
                kind = "cs"
            elif b'"compact_boundary"' in line and b'"type":"system"' in line:
                kind = "cb"
            else:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            sid = r.get("sessionId") or file_sid
            if not sid:
                continue
            if kind == "a" and t == "assistant":
                m = r.get("message")
                u = m.get("usage") if isinstance(m, dict) else None
                model = m.get("model") if isinstance(m, dict) else None
                if not isinstance(u, dict) or not model or model.startswith("<"):
                    continue
                sub = file_sub or (r.get("agentId") or "")
                c = _call_from(r, m, u, sid, sub)
                if not c["mid"] or not c["ts"]:
                    continue
                key = (sid, sub, c["mid"])
                old = calls.get(key)
                if old:
                    for k in INT_FIELDS:
                        old[k] = max(old[k], c[k])
                    old["ts"] = min(old["ts"], c["ts"])
                    old["speed"] = c["speed"] or old["speed"]
                    old["effort"] = c["effort"] or old["effort"]
                    for tn in c["tools"]:
                        if tn not in old["tools"]:
                            old["tools"].append(tn)
                else:
                    calls[key] = c
                touch(sess(sid), r, c["ts"])
            elif kind == "u" and t == "user":
                if r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
                    continue
                m = r.get("message") or {}
                text = _text_of(m.get("content"))
                if isinstance(m.get("content"), list) and not text and not any(
                        isinstance(b, dict) and b.get("type") in ("image", "document") for b in m["content"]):
                    continue
                if text.lstrip().startswith(SKIP_PROMPT_PREFIX):
                    continue
                ts = r.get("timestamp")
                s = sess(sid)
                touch(s, r, ts)
                if r.get("uuid"):
                    events.append((r["uuid"], sid, "prompt", ts, None))
                if not s["first_prompt"]:
                    s["first_prompt"] = _derive_label(text)
            elif kind == "ct":
                if r.get("customTitle"):
                    sess(sid)["custom_title"] = str(r["customTitle"])
            elif kind == "at":
                if r.get("aiTitle"):
                    sess(sid)["ai_title"] = str(r["aiTitle"])
            elif kind == "cs":
                sess(sid)["cost"] = {k: r.get(k) for k in (
                    "totalCostUSD", "totalAPIDuration", "totalDuration", "totalToolDuration",
                    "totalLinesAdded", "totalLinesRemoved", "modelUsage")}
            elif kind == "cb":
                cm = r.get("compactMetadata") if isinstance(r.get("compactMetadata"), dict) else {}
                ts = r.get("timestamp")
                touch(sess(sid), r, ts)
                events.append((r.get("uuid") or "cb-%s-%s" % (sid, ts), sid, "compact", ts, json.dumps({
                    "trigger": cm.get("trigger"), "pre": cm.get("preTokens"), "post": cm.get("postTokens")})))
    return pos, calls, sessions, events


def merge_into_db(conn, cfg_key, calls, sessions, events):
    for c in calls.values():
        vals = (c["sid"], c["sub"], c["mid"], c["ts"], c["model"], c["inp"], c["out"], c["cr"], c["cw5"],
                c["cw1"], c["think"], c["ctx"], c["web"], c["speed"], c["effort"],
                json.dumps(c["tools"], separators=(",", ":")))
        cur = conn.execute("INSERT OR IGNORE INTO calls(sid,sub,mid,ts,model,inp,out,cr,cw5,cw1,think,ctx,web,"
                           "speed,effort,tools,dirty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)", vals)
        if cur.rowcount == 0:
            conn.execute(
                "UPDATE calls SET ts=CASE WHEN ts<=? THEN ts ELSE ? END, model=?, inp=max(inp,?), out=max(out,?),"
                " cr=max(cr,?), cw5=max(cw5,?), cw1=max(cw1,?), think=max(think,?), ctx=max(ctx,?), web=max(web,?),"
                " speed=coalesce(?,speed), effort=coalesce(?,effort), tools=ccm_union(tools,?), dirty=1"
                " WHERE sid=? AND sub=? AND mid=?",
                (c["ts"], c["ts"], c["model"], c["inp"], c["out"], c["cr"], c["cw5"], c["cw1"], c["think"],
                 c["ctx"], c["web"], c["speed"], c["effort"], vals[-1], c["sid"], c["sub"], c["mid"]))
    for ev in events:
        conn.execute("INSERT OR IGNORE INTO events(uuid,sid,kind,ts,extra) VALUES(?,?,?,?,?)", ev)
    for sid, s in sessions.items():
        conn.execute("INSERT OR IGNORE INTO sessions(sid, cfg) VALUES(?,?)", (sid, cfg_key))
        row = conn.execute("SELECT cwd,branch,entrypoint,version,first_ts,last_ts,custom_title,ai_title,"
                           "first_prompt,cost_json FROM sessions WHERE sid=?", (sid,)).fetchone()
        cwd, branch, ep, ver, f_ts, l_ts, ct, at, fp, cj = row
        f_ts = min([x for x in (f_ts, s["first_ts"]) if x] or [None]) if (f_ts or s["first_ts"]) else None
        l_ts = max([x for x in (l_ts, s["last_ts"]) if x] or [None]) if (l_ts or s["last_ts"]) else None
        conn.execute(
            "UPDATE sessions SET cwd=?, branch=?, entrypoint=?, version=?, first_ts=?, last_ts=?, custom_title=?,"
            " ai_title=?, first_prompt=?, cost_json=?, dirty=1 WHERE sid=?",
            (cwd or s["cwd"], s["branch"] or branch, ep or s["entrypoint"], s["version"] or ver, f_ts, l_ts,
             s["custom_title"] or ct, s["ai_title"] or at, fp or s["first_prompt"],
             json.dumps(s["cost"]) if s["cost"] else cj, sid))


def walk_transcripts(cfg):
    base = cfg / "projects"
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames.sort()
        for fn in sorted(filenames):
            if not fn.endswith(".jsonl"):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(base).parts
            sub = None
            if "subagents" in rel:
                sub = p.stem[len("agent-"):] if p.stem.startswith("agent-") else p.stem
                sid = rel[rel.index("subagents") - 1]
            else:
                sid = p.stem
            yield p, sid, sub


def scan_config_dir(conn, cfg, cfg_key, touched, stats, progress):
    files = list(walk_transcripts(cfg))
    known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, off, size FROM files")}
    last_print = time.time()
    for i, (p, sid, sub) in enumerate(files):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        off, _ = known.get(str(p), (0, 0))
        if off > size:
            off = 0  # rewritten/truncated: re-parse (upserts are idempotent)
        if off == size:
            continue
        try:
            new_off, calls, sessions, events = parse_file(p, off, sid, sub)
        except OSError as e:
            log("  skip %s: %s" % (p.name, e))
            continue
        with conn:
            merge_into_db(conn, cfg_key, calls, sessions, events)
            conn.execute("INSERT OR REPLACE INTO files(path,off,size) VALUES(?,?,?)", (str(p), new_off, size))
        touched.update(sessions.keys())
        stats["files"] += 1
        stats["bytes"] += new_off - off
        stats["calls"] += len(calls)
        if progress and time.time() - last_print > 10:
            last_print = time.time()
            log("  ... %d/%d files, %.0f MB read" % (i + 1, len(files), stats["bytes"] / 1e6))
    stats["seen_files"] += len(files)


# --------------------------------------------------------------------------- accounts

def accounts_log_path():
    return CCM_HOME / "accounts_seen.jsonl"


def load_accounts_log():
    last = {}
    try:
        with open(str(accounts_log_path()), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                last[r.get("cfg")] = r
    except OSError:
        pass
    return last


def record_account(cfg_key, acct, prev_log):
    """Append to accounts_seen.jsonl when the logged-in account of a config dir changes.
    Returns the confidence level for sessions touched in this run."""
    prev = prev_log.get(cfg_key)
    uid = acct["account_uuid"] if acct else None
    if prev is None:
        conf = "assumed"
    elif prev.get("account_uuid") == uid:
        conf = "observed"
    else:
        conf = "ambiguous"
    if prev is None or prev.get("account_uuid") != uid:
        rec = {"ts": utc_iso(), "cfg": cfg_key, "account_uuid": uid}
        rec.update(acct or {})
        with open(str(accounts_log_path()), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return conf


def assign_accounts(conn, cfg_key, acct, conf, touched_sids):
    uid = acct["account_uuid"] if acct else None
    for sid in touched_sids:
        row = conn.execute("SELECT cfg, account_uuid FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not row or row[0] != cfg_key:
            continue
        if row[1] is None and uid is None:
            conn.execute("UPDATE sessions SET account_conf='unknown' WHERE sid=?", (sid,))
        elif row[1] is None:
            conn.execute("UPDATE sessions SET account_uuid=?, account_conf=? WHERE sid=?", (uid, conf, sid))
        elif row[1] != uid:
            conn.execute("UPDATE sessions SET account_uuid=?, account_conf='ambiguous' WHERE sid=?", (uid, sid))


# --------------------------------------------------------------------------- device + config

def load_config():
    cfg = dict(BAKED_CONFIG or {})
    cfg.update(read_json(CCM_HOME / "config.json"))
    return cfg


def save_config(cfg):
    CCM_HOME.mkdir(parents=True, exist_ok=True)
    with open(str(CCM_HOME / "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(str(CCM_HOME / "config.json"), 0o600)
    except OSError:
        pass


def device_id():
    p = CCM_HOME / "device_id"
    try:
        v = p.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    CCM_HOME.mkdir(parents=True, exist_ok=True)
    v = str(uuid.uuid4())
    p.write_text(v, encoding="utf-8")
    return v


def os_name():
    sysname = platform.system()
    rel = platform.release().lower()
    if sysname == "Linux" and ("microsoft" in rel or "wsl" in rel):
        return "WSL"
    return {"Darwin": "macOS"}.get(sysname, sysname)


def device_meta(cfg, machine_id):
    return {
        "device_id": device_id(), "machine_id": machine_id, "label": cfg.get("label") or socket.gethostname(),
        "hostname": socket.gethostname(), "os": os_name(), "os_release": platform.release(),
        "tz_offset_min": int((time.localtime().tm_gmtoff or 0) // 60),
        "agent_version": AGENT_VERSION, "python": platform.python_version(),
    }


def tilde(path):
    s = str(path)
    h = str(Path.home())
    return "~" + s[len(h):] if s.startswith(h) else s


# --------------------------------------------------------------------------- export

def project_of(cwd, worktree_marker=re.compile(r"[\\/]\.(?:claude[\\/]worktrees|worktrees)[\\/]")):
    if not cwd:
        return None, False
    m = worktree_marker.search(cwd)
    base = cwd[:m.start()] if m else cwd
    name = re.split(r"[\\/]", base.rstrip("\\/"))[-1] or base
    return name, bool(m)


def build_export(conn, full, meta, accounts, cfg_keys):
    """Yield dicts (NDJSON records). Marks nothing; caller marks dirty=0 afterwards."""
    yield dict(rec="meta", schema=SCHEMA, generated_at=utc_iso(), full=bool(full), device=meta,
               config_dirs=cfg_keys)
    for cfg_key, acct in accounts.items():
        if acct:
            yield dict(rec="account", cfg=cfg_key, **acct)

    where = "" if full else "WHERE dirty=1"
    sids = [r[0] for r in conn.execute("SELECT sid FROM sessions %s ORDER BY first_ts" % where)]
    n_sessions = 0
    for sid in sids:
        row = conn.execute("SELECT cfg,cwd,branch,entrypoint,version,first_ts,last_ts,custom_title,ai_title,"
                           "first_prompt,cost_json,account_uuid,account_conf FROM sessions WHERE sid=?",
                           (sid,)).fetchone()
        cfg_key, cwd, branch, ep, ver, f_ts, l_ts, ct, at, fp, cost_json, acc_uuid, acc_conf = row
        tools, mcp, tokens, subs = {}, {}, {}, {}
        ts_list, ctxs, ncalls, web = [], [], 0, 0
        for sub, ts, model, inp, out, cr, cw5, cw1, think, ctx, w, tl in conn.execute(
                "SELECT sub,ts,model,inp,out,cr,cw5,cw1,think,ctx,web,tools FROM calls WHERE sid=? ORDER BY ts",
                (sid,)):
            ncalls += 1
            web += w or 0
            e = ts_epoch(ts)
            if e is not None:
                ts_list.append(e)
            if not sub:
                ctxs.append(ctx or 0)
            tk = tokens.setdefault(model, dict(inp=0, out=0, cr=0, cw5=0, cw1=0, think=0))
            for k, v in (("inp", inp), ("out", out), ("cr", cr), ("cw5", cw5), ("cw1", cw1), ("think", think)):
                tk[k] += v or 0
            if sub:
                sb = subs.setdefault(sub, dict(calls=0, tokens=0))
                sb["calls"] += 1
                sb["tokens"] += (inp or 0) + (out or 0) + (cr or 0) + (cw5 or 0) + (cw1 or 0)
            for tn in json.loads(tl or "[]"):
                tools[tn] = tools.get(tn, 0) + 1
                if tn.startswith("mcp__"):
                    parts = tn.split("__", 2)
                    srv, tool = parts[1], (parts[2] if len(parts) > 2 else "")
                    mcp.setdefault(srv, {})
                    mcp[srv][tool] = mcp[srv].get(tool, 0) + 1
        prompts, compactions = 0, []
        for kind, ts, extra in conn.execute("SELECT kind,ts,extra FROM events WHERE sid=? ORDER BY ts", (sid,)):
            e = ts_epoch(ts)
            if e is not None:
                ts_list.append(e)
            if kind == "prompt":
                prompts += 1
            else:
                d = json.loads(extra) if extra else {}
                compactions.append({"ts": ts, "pre": d.get("pre"), "post": d.get("post"),
                                    "trigger": d.get("trigger")})
        ts_list.sort()
        active = sum(b - a for a, b in zip(ts_list, ts_list[1:]) if b - a < GAP_ACTIVE_S)
        title = ct or at
        kind = "custom" if ct else ("ai" if at else ("derived" if fp else "none"))
        project, worktree = project_of(cwd)
        cost = json.loads(cost_json) if cost_json else None
        yield dict(
            rec="session", id=sid, cfg=cfg_key, title=title or fp, title_kind=kind, project=project, cwd=cwd,
            branch=branch, worktree=worktree, entrypoint=ep, version=ver, first_ts=f_ts, last_ts=l_ts,
            active_s=int(active), prompts=prompts, calls=ncalls, web_requests=web, tools=tools, mcp=mcp,
            subagents=subs, tokens=tokens, ctx_peak=max(ctxs) if ctxs else 0,
            ctx_avg=int(sum(ctxs) / len(ctxs)) if ctxs else 0, compactions=compactions, cost=cost,
            account_uuid=acc_uuid, account_conf=acc_conf)
        n_sessions += 1

    cols = "sid,sub,mid,ts,model,inp,out,cr,cw5,cw1,think,ctx,web,speed,effort,tools"
    q = "SELECT %s FROM calls %s ORDER BY ts" % (cols, where)
    for r in conn.execute(q):
        yield dict(rec="call", sid=r[0], sub=r[1], mid=r[2], ts=r[3], model=r[4], inp=r[5], out=r[6], cr=r[7],
                   cw5=r[8], cw1=r[9], think=r[10], ctx=r[11], web=r[12], speed=r[13], effort=r[14],
                   tools=json.loads(r[15] or "[]"))
    q = "SELECT raw FROM util %s ORDER BY fetched_ms" % where
    for (raw,) in conn.execute(q):
        d = json.loads(raw)
        d["rec"] = "util"
        yield d


def write_export(records, label, outdir=None, friendly=False):
    outbox = Path(outdir) if outdir else CCM_HOME / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:40] or "device"
    if friendly:  # human-shareable name, e.g. ccm-alice-laptop-20261006-1432.ndjson.gz
        final = outbox / ("ccm-%s-%s.ndjson.gz" % (safe, time.strftime("%Y%m%d-%H%M")))
    else:
        final = outbox / ("ccm-%s-%s-%s.ndjson.gz" % (safe, utc_stamp(), uuid.uuid4().hex[:4]))  # never collide
    tmp = final.with_suffix(".tmp")
    n = 0
    with gzip.open(str(tmp), "wb") as gz:
        for rec in records:
            gz.write((json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
            n += 1
    os.replace(str(tmp), str(final))
    return final, n


# --------------------------------------------------------------------------- send

def post_file(url, token, path):
    data = Path(path).read_bytes()
    req = urlrequest.Request(url.rstrip("/") + "/ingest", data=data, method="POST")
    req.add_header("Content-Type", "application/gzip")
    req.add_header("X-CCM-Filename", Path(path).name)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urlrequest.urlopen(req, timeout=30) as resp:
        return 200 <= resp.status < 300


def send_outbox(cfg):
    outbox = CCM_HOME / "outbox"
    sent_dir = CCM_HOME / "sent"
    servers = cfg.get("servers") or []
    files = sorted(outbox.glob("*.ndjson.gz")) if outbox.is_dir() else []
    if not files:
        return 0, 0, "nothing to send"
    if not servers:
        return 0, len(files), "no server configured (file(s) kept in outbox)"
    delivered, last_err = 0, ""
    for f in files:
        ok = False
        for url in servers:
            try:
                if post_file(url, cfg.get("token"), f):
                    ok = True
                    break
            except (urlerror.URLError, OSError, ValueError) as e:
                last_err = "%s: %s" % (url, getattr(e, "reason", e))
        if not ok:
            break  # server unreachable: keep order, retry next run
        sent_dir.mkdir(parents=True, exist_ok=True)
        os.replace(str(f), str(sent_dir / f.name))
        delivered += 1
    pending = len(files) - delivered
    return delivered, pending, last_err


# --------------------------------------------------------------------------- locking

class Lock:
    def __init__(self):
        self.path = CCM_HOME / "lock"
        self.fd = None

    def __enter__(self):
        CCM_HOME.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 3600:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                raise SystemExit("another ccm_agent run is in progress (remove %s if stale)" % self.path)
        raise SystemExit("could not lock %s" % self.path)

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            self.path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- commands

def share_dir():
    d = Path.home() / "Desktop"
    return d if d.is_dir() else Path.home()


def reveal(path):
    """Show the file in the OS file manager so a non-technical person can find it. Best effort."""
    if os.environ.get("CCM_NO_REVEAL"):
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError:
        pass


def collect(args, send, share=False):
    cfg = load_config()
    if args.label:
        cfg["label"] = args.label
    if getattr(args, "server", None):
        cfg["servers"] = list(args.server)
    if getattr(args, "token", None):
        cfg["token"] = args.token
    dirs = discover_config_dirs(args.config_dir)
    if not dirs:
        log("No Claude Code config directory with a 'projects' folder found.")
        return 1
    with Lock():
        conn = open_db()
        prev_log = load_accounts_log()
        stats = dict(files=0, seen_files=0, bytes=0, calls=0)
        accounts, machine_id = {}, None
        progress = not args.quiet
        t0 = time.time()
        for d in dirs:
            cfg_key = tilde(d)
            cj = read_json(claude_json_path(d))
            machine_id = machine_id or cj.get("machineID")
            acct = account_info(cj)
            accounts[cfg_key] = acct
            conf = record_account(cfg_key, acct, prev_log)
            touched = set()
            if progress:
                log("scanning %s (%s)" % (cfg_key, acct["email"] if acct else "no account info"))
            scan_config_dir(conn, d, cfg_key, touched, stats, progress)
            with conn:
                assign_accounts(conn, cfg_key, acct, conf, touched)
            snap = compact_snapshot(cj)
            if snap:
                with conn:
                    conn.execute("INSERT OR IGNORE INTO util(fetched_ms,account_uuid,raw,dirty) VALUES(?,?,?,1)",
                                 (snap["fetched_ms"], snap.get("account_uuid") or "", json.dumps(snap)))
        meta = device_meta(cfg, machine_id)
        n_dirty = conn.execute("SELECT (SELECT count(*) FROM sessions WHERE dirty=1)"
                               "+(SELECT count(*) FROM calls WHERE dirty=1)"
                               "+(SELECT count(*) FROM util WHERE dirty=1)").fetchone()[0]
        out_file, n_rec, share_file = None, 0, None
        if share:
            # complete snapshot of the archive: idempotent on the server, so sending only the newest file is enough
            share_file, n_rec = write_export(build_export(conn, True, meta, accounts, [tilde(d) for d in dirs]),
                                             meta["label"], outdir=share_dir(), friendly=True)
            with conn:
                conn.execute("UPDATE sessions SET dirty=0")
                conn.execute("UPDATE calls SET dirty=0")
                conn.execute("UPDATE util SET dirty=0")
            if not cfg.get("servers"):  # superseded by the full snapshot
                (CCM_HOME / "sent").mkdir(parents=True, exist_ok=True)
                for old in (CCM_HOME / "outbox").glob("*.ndjson.gz"):
                    os.replace(str(old), str(CCM_HOME / "sent" / old.name))
        elif n_dirty or args.full:
            out_file, n_rec = write_export(build_export(conn, args.full, meta, accounts, [tilde(d) for d in dirs]),
                                           meta["label"])
            with conn:
                conn.execute("UPDATE sessions SET dirty=0")
                conn.execute("UPDATE calls SET dirty=0")
                conn.execute("UPDATE util SET dirty=0")
        tot_s = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        tot_c = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
        conn.close()

    print("CCM collect: %d config dir(s), %d transcript files (%d changed, %.1f MB read, %.1fs)"
          % (len(dirs), stats["seen_files"], stats["files"], stats["bytes"] / 1e6, time.time() - t0))
    for d in dirs:
        a = accounts[tilde(d)]
        print("  %s -> %s" % (tilde(d), ("%s (%s)" % (a["email"], a.get("org_type"))) if a else "account unknown"))
    print("archive: %d conversations, %d API calls" % (tot_s, tot_c))
    if share_file:
        print("")
        print("SHARE FILE: %s (%.1f MB)" % (share_file, share_file.stat().st_size / 1e6))
        print("Send this one file to Heramb (WhatsApp / email). It has titles, project names, token counts, tool "
              "names, times and account email; no prompt text or file contents.")
        reveal(share_file)
        send = False
    if out_file:
        print("export: %s (%d records, %.1f KB)" % (out_file, n_rec, out_file.stat().st_size / 1024))
    else:
        print("export: no new data since the last run")
    if send:
        delivered, pending, err = send_outbox(cfg)
        if delivered:
            print("sent: %d file(s) delivered to the CCM server" % delivered)
        if pending:
            print("pending: %d file(s) in %s (%s)" % (pending, CCM_HOME / "outbox", err or "will retry next run"))
    elif out_file:
        print("not sent (export mode). Import it on the main PC with: ccm_server.py import %s" % out_file)
    return 0


def command_text(python, target, verb="collect"):
    return ('---\ndescription: Collect Claude Code usage stats on this device and send them to the CCM server\n'
            'allowed-tools: Bash\ndisable-model-invocation: true\n---\n'
            'Run this command with the Bash tool and show its output as-is. It only reads local Claude Code '
            'usage metadata (no prompt text leaves this device):\n\n'
            '`"%s" "%s" %s`\n' % (python, target, verb))


def repo_server_urls():
    """Server URLs published in the repo (a SERVER_URL file next to the agent or one level up) or $CCM_SERVER."""
    urls = [u for u in os.environ.get("CCM_SERVER", "").split(",") if u.strip()]
    here = Path(os.path.abspath(__file__)).parent
    for d in (here, here.parent):
        p = d / "SERVER_URL"
        if p.is_file():
            try:
                urls += [l.strip() for l in p.read_text(encoding="utf-8").splitlines()]
            except OSError:
                pass
    return [u for u in urls if u.startswith(("http://", "https://"))]


def check_servers(servers):
    ok = False
    for url in servers:
        try:
            with urlrequest.urlopen(url.rstrip("/") + "/health", timeout=6) as r:
                good = r.status == 200
        except (urlerror.URLError, OSError, ValueError) as e:
            print("  server %s: NOT reachable (%s)" % (url, getattr(e, "reason", e)))
            continue
        print("  server %s: %s" % (url, "reachable" if good else "unexpected response"))
        ok = ok or good
    return ok


def cmd_install(args):
    CCM_HOME.mkdir(parents=True, exist_ok=True)
    target = CCM_HOME / "ccm_agent.py"
    src = Path(os.path.abspath(__file__))
    if not target.exists() or os.path.realpath(str(src)) != os.path.realpath(str(target)):
        shutil.copyfile(str(src), str(target))
    cfg = load_config()
    if args.label:
        cfg["label"] = args.label
    cfg.setdefault("label", socket.gethostname())
    if args.server:
        cfg["servers"] = list(args.server)
    elif not cfg.get("servers"):
        cfg["servers"] = repo_server_urls()
    if args.token:
        cfg["token"] = args.token
    save_config(cfg)
    dirs = discover_config_dirs(args.config_dir)
    for d in dirs:
        cdir = d / "commands"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "ccm-collect.md").write_text(
            command_text(sys.executable, target, "collect" if cfg.get("servers") else "share"), encoding="utf-8")
    print("Installed agent to %s (device %s, label '%s')" % (target, device_id(), cfg["label"]))
    print("Servers: %s" % (", ".join(cfg.get("servers") or []) or "none configured (use export + manual import)"))
    print("Slash command /ccm-collect added to: %s" % (", ".join(tilde(d) for d in dirs) or "no config dir found"))
    if args.schedule:
        install_schedule(target, args.schedule_hours)
    reachable = check_servers(cfg.get("servers") or [])
    if cfg.get("servers") and not reachable:
        print("WARNING: no server reachable. Is Tailscale installed, signed in and connected on this device? "
              "Data will queue in %s until the server is reachable." % (CCM_HOME / "outbox"))
    print('Run now: "%s" "%s" collect' % (sys.executable, target))
    return 0


def install_schedule(target, hours):
    cmd = '"%s" "%s" collect --quiet' % (sys.executable, target)
    if os.name == "nt":
        try:
            r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "HOURLY", "/MO", str(hours), "/TN",
                                "CCM-Collect", "/TR", cmd], capture_output=True, text=True)
            print("Scheduled task CCM-Collect: %s" % ("ok" if r.returncode == 0 else r.stderr.strip()))
        except OSError as e:
            print("could not schedule: %s" % e)
        return
    line = "7 */%d * * * %s >/dev/null 2>&1  # ccm-agent" % (hours, cmd)
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except OSError:
        print("cron not available; schedule '%s' with your system scheduler instead" % cmd)
        return
    existing = [l for l in (cur.stdout if cur.returncode == 0 else "").splitlines() if "# ccm-agent" not in l]
    r = subprocess.run(["crontab", "-"], input="\n".join(existing + [line]) + "\n", text=True,
                       capture_output=True)
    print("cron entry (every %dh): %s" % (hours, "ok" if r.returncode == 0 else r.stderr.strip()))


def cmd_status(args):
    cfg = load_config()
    print("agent %s | home %s | device %s | label %s" % (AGENT_VERSION, CCM_HOME, device_id(), cfg.get("label")))
    print("servers: %s" % (", ".join(cfg.get("servers") or []) or "none"))
    for d in discover_config_dirs(args.config_dir):
        a = account_info(read_json(claude_json_path(d)))
        print("config dir %s -> %s" % (tilde(d), ("%s (%s, %s)" % (a["email"], a.get("org_type"),
                                                                   a.get("rate_limit_tier"))) if a else "no account"))
    if (CCM_HOME / "archive.sqlite").exists():
        conn = open_db()
        print("archive: %d conversations, %d calls, %d util snapshots" % (
            conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
            conn.execute("SELECT count(*) FROM calls").fetchone()[0],
            conn.execute("SELECT count(*) FROM util").fetchone()[0]))
        conn.close()
    for name in ("outbox", "sent"):
        p = CCM_HOME / name
        print("%s: %d file(s)" % (name, len(list(p.glob("*.ndjson.gz"))) if p.is_dir() else 0))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="CCM agent: collect Claude Code usage metadata")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config-dir", action="append", default=[], help="extra Claude config dir (repeatable)")
        p.add_argument("--label", help="friendly device name")

    for name in ("collect", "export"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--full", action="store_true", help="export everything, not just new data")
        p.add_argument("--quiet", action="store_true")
        if name == "collect":
            p.add_argument("--no-send", action="store_true")
            p.add_argument("--server", action="append", help="override server URL(s)")
            p.add_argument("--token")
    p = sub.add_parser("share", help="collect and write ONE file to the Desktop to send by WhatsApp/email")
    common(p)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("install")
    common(p)
    p.add_argument("--server", action="append")
    p.add_argument("--token")
    p.add_argument("--schedule", action="store_true", help="also schedule periodic runs (cron / Task Scheduler)")
    p.add_argument("--schedule-hours", type=int, default=2)
    p = sub.add_parser("status")
    common(p)

    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        argv = ["collect"] + argv  # bare invocation == collect
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd == "install":
        return cmd_install(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "export":
        return collect(args, send=False)
    if cmd == "share":
        return collect(args, send=False, share=True)
    return collect(args, send=not args.no_send)


if __name__ == "__main__":
    sys.exit(main())
