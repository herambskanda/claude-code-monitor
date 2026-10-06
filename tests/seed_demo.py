#!/usr/bin/env python3
"""Build a realistic MULTI-device, 2-account demo database from a single-device ccm.db.

    python3 tests/seed_demo.py SOURCE_ccm.db OUT_DATA_DIR [--seed N]

The source is opened read-only and never modified. OUT_DATA_DIR/ccm.db is (re)created. The result has 4 devices
(different OS / timezone / label), 2 accounts (one on two config dirs), time-shifted and scaled copies of the
source conversations, some 'assumed' / 'ambiguous' account attributions, one override rule, synthetic utilization
snapshots, and injected spikes (see INJECTED) so every dashboard view and every anomaly detector has something to show.
Python 3.8+, standard library only. Titles/projects come from the source data: keep the output out of version control.
"""
import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import ccm_calc  # noqa: E402
import ccm_server  # noqa: E402

NOW = ccm_calc.ts_epoch("2026-10-06T06:30:00Z")
NS = uuid.UUID("12345678-1234-5678-1234-567812345678")

ACCOUNTS = {
    "A": dict(account_uuid="aaaaaaaa-0000-4000-8000-00000000000a", email="alice@example.com", org_name="Alice",
              org_type="claude_max", rate_limit_tier="default_claude_max_20x", display_name="alice", nickname=None,
              week_end="2026-10-10T23:59:59+00:00"),
    "B": dict(account_uuid="bbbbbbbb-0000-4000-8000-00000000000b", email="alice@work.example.org", org_name="Work Org",
              org_type="claude_max", rate_limit_tier="default_claude_max_5x", display_name="alice-work",
              nickname="Work", week_end="2026-10-07T18:00:00+00:00"),
}
DEVICES = [
    dict(key="mac", label="MacBook Pro", host="macbook-pro.local", os="macOS", rel="25.2.0", tz=330, keep=1.0,
         scale=(0.9, 1.1), extra_h=0, days=(0, 0), install="2026-09-20T00:00:00Z",
         cfgs=[("~/.claude", "A", 1.0)]),
    dict(key="win", label="Gaming PC", host="DESKTOP-4F2K", os="Windows", rel="10.0.26100", tz=-300, keep=0.55,
         scale=(0.6, 1.5), extra_h=0, days=(-3, 3), install="2026-09-14T00:00:00Z",
         cfgs=[("~/.claude", "A", 0.93), ("~/.claude-b", "B", 0.07)]),
    dict(key="vm", label="Proxmox dev VM", host="pve-dev-01", os="Linux", rel="6.8.12-pve", tz=0, keep=0.35,
         scale=(0.5, 1.6), extra_h=3, days=(-5, 2), install="2026-09-10T00:00:00Z",
         cfgs=[("~/.claude", "B", 0.5), ("~/.claude-work", "B", 0.5)]),
    dict(key="wsl", label="Work laptop (WSL)", host="LAPTOP-WORK", os="Linux", rel="5.15.167.4-microsoft-standard-WSL2",
         tz=60, keep=0.9, scale=(0.5, 1.3), extra_h=8, days=(-4, 4), hours=(8, 19), install="2026-09-16T00:00:00Z",
         cfgs=[("~/.claude", "B", 1.0)]),
]
INJECTED = [
    "runaway context: 'Debug flaky ETL job (runaway context)' on Proxmox dev VM, 2026-09-29",
    "off-hours burst: 3 'Overnight batch refactor' conversations on Work laptop (WSL) around 03:00 local, 2026-10-02",
    "tool loop: 'Fix failing CI (stuck retry loop)' on MacBook Pro, 180 Bash calls in a row, 2026-09-27",
    "cache thrash: 'Large monorepo refactor (cache thrash)' on Gaming PC, 40 calls with zero cache reads, 2026-09-24",
    "spend spike: a day with a dozen heavy conversations on Gaming PC (account A), 2026-09-25",
]


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".%03dZ" % int((epoch % 1) * 1000)


def sid_for(dev, sid, salt=""):
    return str(uuid.uuid5(NS, "%s|%s|%s" % (dev, sid, salt)))


def scale_json_numbers(obj, f):
    if isinstance(obj, dict):
        return {k: scale_json_numbers(v, f) for k, v in obj.items()}
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return type(obj)(obj * f) if isinstance(obj, int) else obj * f
    return obj


def derive(sess, calls):
    """Recompute aggregate fields of a session dict from its (already final) call tuples."""
    toks, tools, ctxs, ts = {}, {}, [], sorted(c["ts_epoch"] for c in calls)
    for c in calls:
        t = toks.setdefault(c["model"], dict(inp=0, out=0, cr=0, cw5=0, cw1=0, think=0))
        for k in t:
            t[k] += c[k]
        for n in json.loads(c["tools_json"] or "[]"):
            tools[n] = tools.get(n, 0) + 1
        if not c["sub"]:
            ctxs.append(c["ctx"])
    sess["tokens_json"] = json.dumps(toks, separators=(",", ":"))
    sess["tools_json"] = json.dumps(tools, separators=(",", ":"))
    sess["calls"] = len(calls)
    sess["ctx_peak"] = max(ctxs) if ctxs else 0
    sess["ctx_avg"] = int(sum(ctxs) / len(ctxs)) if ctxs else 0
    sess["active_s"] = int(sum(b - a for a, b in zip(ts, ts[1:]) if b - a < 300))
    sess["first_epoch"], sess["last_epoch"] = ts[0], ts[-1]
    sess["first_ts"], sess["last_ts"] = iso(ts[0]), iso(ts[-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = out_dir / ("ccm.db" + suffix)
        if p.exists():
            p.unlink()
    src = sqlite3.connect("file:%s?mode=ro" % Path(a.source).resolve().as_posix(), uri=True)
    src.row_factory = sqlite3.Row
    sessions = [dict(r) for r in src.execute("SELECT * FROM sessions")]
    src_calls = {}
    for r in src.execute("SELECT * FROM calls ORDER BY ts_epoch"):
        src_calls.setdefault((r["device_id"], r["sid"]), []).append(dict(r))
    src_dev = dict(src.execute("SELECT * FROM devices").fetchone())
    src.close()
    orig_tz = src_dev.get("tz_offset_min") or 0

    conn = ccm_server.open_db(out_dir)
    for key, acc in ACCOUNTS.items():
        conn.execute("INSERT INTO accounts(account_uuid,email,org_uuid,org_name,org_type,rate_limit_tier,billing_type,"
                     "display_name,org_role,nickname) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (acc["account_uuid"], acc["email"], "org-" + key, acc["org_name"], acc["org_type"],
                      acc["rate_limit_tier"], "stripe_subscription", acc["display_name"], "admin", acc["nickname"]))
    dev_ids = {}
    for d in DEVICES:
        dev_ids[d["key"]] = str(uuid.uuid5(NS, "device-" + d["key"]))
        conn.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (dev_ids[d["key"]], hashlib.sha256(d["key"].encode()).hexdigest(), d["label"], d["host"], d["os"],
                      d["rel"], d["tz"], "1.0.0", "3.12.4", "2026-09-10T08:00:00Z", "2026-10-06T06:25:16Z",
                      "ccm-%s-20261006.ndjson.gz" % d["key"]))
        for cfg, acct, _ in d["cfgs"]:
            conn.execute("INSERT INTO device_cfgs VALUES(?,?,?,?)",
                         (dev_ids[d["key"]], cfg, ACCOUNTS[acct]["account_uuid"], "2026-10-06T06:25:16Z"))

    sess_rows, call_rows = [], []

    def add(sess, calls):
        sess_rows.append(sess)
        call_rows.extend(calls)

    # ---- clone ------------------------------------------------------------------------------------------------
    for d in DEVICES:
        did = dev_ids[d["key"]]
        install = ccm_calc.ts_epoch(d["install"])
        for s in sessions:
            calls = src_calls.get((s["device_id"], s["sid"]), [])
            if not calls or rng.random() > d["keep"]:
                continue
            f = math.exp(rng.uniform(math.log(d["scale"][0]), math.log(d["scale"][1])))
            shift = (orig_tz - d["tz"]) * 60 + d["extra_h"] * 3600
            if d["key"] != "mac":
                shift += rng.randint(*d["days"]) * 86400
            # never put data in the future: step back whole days until it fits
            while max(c["ts_epoch"] for c in calls) + shift > NOW:
                shift -= 86400
            if d.get("hours"):   # persona: only works inside business hours (local), so a 3am burst is unusual
                lh = int(((min(c["ts_epoch"] for c in calls) + shift) // 3600 + d["tz"] // 60) % 24)
                if not (d["hours"][0] <= lh < d["hours"][1]):
                    continue
            sid = sid_for(d["key"], s["sid"])
            cfg, acct_key = rng.choices([(c, k) for c, k, _ in d["cfgs"]], [w for _, _, w in d["cfgs"]])[0]
            swap = d["key"] in ("vm", "wsl") and rng.random() < 0.5
            new_calls = []
            for c in calls:
                c = dict(c)
                c["device_id"], c["sid"] = did, sid
                c["ts_epoch"] += shift
                c["ts"] = iso(c["ts_epoch"])
                for k in ("inp", "out", "cr", "cw5", "cw1", "think", "ctx"):
                    c[k] = int((c[k] or 0) * f)
                c["ctx"] = min(c["ctx"], 900000)
                if swap and "opus" in (c["model"] or ""):
                    c["model"] = "claude-sonnet-5-5"
                new_calls.append(c)
            n = dict(s)
            n["device_id"], n["sid"], n["cfg"] = did, sid, cfg
            derive(n, new_calls)
            n["cost_json"] = json.dumps(scale_json_numbers(json.loads(s["cost_json"]), f)) if s["cost_json"] else None
            comp = json.loads(s["compactions_json"] or "[]")
            for cc in comp:
                cc["ts"] = iso(ccm_calc.ts_epoch(cc["ts"]) + shift)
                cc["pre"], cc["post"] = int(cc["pre"] * f), int(cc["post"] * f)
            n["compactions_json"] = json.dumps(comp, separators=(",", ":"))
            n["subagents_json"] = json.dumps(scale_json_numbers(json.loads(s["subagents_json"] or "{}"), f))
            n["account_uuid"] = ACCOUNTS[acct_key]["account_uuid"]
            r = rng.random()
            n["account_conf"] = ("ambiguous" if r < 0.03 else "assumed" if n["first_epoch"] < install else "observed")
            n["updated"] = "2026-10-06T06:25:16Z"
            add(n, new_calls)

    # ---- injected spikes -----------------------------------------------------------------------------------------
    def new_session(dev_key, title, project, calls, acct_key=None, entry="cli", cfg=None, conf="observed"):
        d = next(x for x in DEVICES if x["key"] == dev_key)
        did = dev_ids[dev_key]
        sid = sid_for(dev_key, title)
        for c in calls:
            c.update(device_id=did, sid=sid, sub=c.get("sub", ""), web=0, speed="standard", effort="high",
                     ts=iso(c["ts_epoch"]), think=c.get("think", 100), tools_json=c.get("tools_json", "[]"))
        s = dict(device_id=did, sid=sid, cfg=cfg or d["cfgs"][0][0], title=title, title_kind="ai", project=project,
                 cwd="/home/alice/src/" + project, branch="main", worktree=0, entrypoint=entry, version="2.1.260",
                 prompts=max(3, len(calls) // 9), web_requests=0, mcp_json="{}", subagents_json="{}",
                 compactions_json="[]", cost_json=None,
                 account_uuid=ACCOUNTS[acct_key or d["cfgs"][0][1]]["account_uuid"], account_conf=conf,
                 updated="2026-10-06T06:25:16Z")
        derive(s, calls)
        add(s, calls)

    def mk_call(i, t, ctx, model, out=700, cr=None, cw=None, tools=("Bash",)):
        cr = int(ctx * 0.92) if cr is None else cr
        cw = (ctx - cr) if cw is None else cw
        return dict(mid="inj_%s_%d_%d" % (hashlib.md5(str(t).encode()).hexdigest()[:6], i, rng.randint(0, 10 ** 6)),
                    ts_epoch=t, model=model, inp=3, out=out, cr=cr, cw5=0, cw1=cw, ctx=ctx, think=out // 6,
                    tools_json=json.dumps(list(tools)), sub="")

    def day_epoch(ymd, hour_utc):
        return ccm_calc.ts_epoch("%sT%02d:00:00Z" % (ymd, hour_utc))

    # 1. runaway context
    t, calls = day_epoch("2026-09-29", 10), []
    ctx = 25000
    for i in range(80):
        if i < 40:
            ctx += 900
        elif i == 40:
            ctx = 130000
        elif i == 41:
            ctx = 262000
        elif i == 42:
            ctx = 521000
        else:
            ctx = min(720000, ctx + 3500)
        calls.append(mk_call(i, t, ctx, "claude-opus-5-5", cr=int(ctx * 0.3) if i in (40, 41, 42) else None,
                             tools=("Read",) if i % 3 else ("Bash",)))
        t += rng.uniform(40, 130)
    new_session("vm", "Debug flaky ETL job (runaway context)", "etl-pipeline", calls, "B", cfg="~/.claude-work")

    # 2. off-hours burst: pick the rarest local hour of the WSL device around the night
    wsl = next(d for d in DEVICES if d["key"] == "wsl")
    hist = [0] * 24
    for c in call_rows:
        if c["device_id"] == dev_ids["wsl"]:
            hist[int(((c["ts_epoch"] + wsl["tz"] * 60) // 3600) % 24)] += 1
    night = min(range(1, 6), key=lambda h: hist[h])
    base = day_epoch("2026-10-02", 0) + (night - wsl["tz"] // 60) * 3600
    for k in range(3):
        t, calls, ctx = base + k * 1800, [], 30000
        for i in range(140):
            ctx = min(95000, ctx + rng.randint(100, 900))
            calls.append(mk_call(i, t, ctx, "claude-sonnet-5-5", out=500, tools=("Edit",) if i % 2 else ("Read",)))
            t += rng.uniform(15, 40)
        new_session("wsl", "Overnight batch refactor %d" % (k + 1), "backend-api", calls, "B")

    # 3. tool loop
    t, calls = day_epoch("2026-09-27", 9), []
    for i in range(180):
        calls.append(mk_call(i, t, 68000 + (i % 7) * 120, "claude-opus-5", out=250, tools=("Bash",)))
        t += rng.uniform(8, 25)
    new_session("mac", "Fix failing CI (stuck retry loop)", "helm", calls, "A")

    # 4. cache thrash
    t, calls = day_epoch("2026-09-24", 15), []
    for i in range(40):
        ctx = 150000 + i * 400
        calls.append(mk_call(i, t, ctx, "claude-opus-5-5", out=900, cr=0, cw=ctx, tools=("Read",) if i % 2 else ("Edit",)))
        t += rng.uniform(40, 120)
    new_session("win", "Large monorepo refactor (cache thrash)", "monorepo", calls, "A")

    # 5. spend spike day: copy the most expensive conversations onto one day of the Gaming PC
    prices = ccm_calc.Prices(conn.execute("SELECT pattern,in_p,out_p,cr_p,cw5_p,cw1_p FROM price_table").fetchall())
    cost = {}
    for c in call_rows:
        cost[c["sid"]] = cost.get(c["sid"], 0) + prices.usd(c["model"], c["inp"], c["out"], c["cr"], c["cw5"], c["cw1"])
    by_sid = {}
    for c in call_rows:
        by_sid.setdefault(c["sid"], []).append(c)
    heavy = sorted((s for s in sess_rows if s["account_uuid"] == ACCOUNTS["A"]["account_uuid"]),
                   key=lambda s: -cost.get(s["sid"], 0))[:12]
    for k, s in enumerate(heavy):
        calls = [dict(c) for c in by_sid[s["sid"]]]
        start = day_epoch("2026-09-25", 12) + k * 1500
        off = start - min(c["ts_epoch"] for c in calls)
        did = dev_ids["win"]
        sid = sid_for("win", s["sid"], "spike")
        for c in calls:
            c["ts_epoch"] += off
            c["ts"] = iso(c["ts_epoch"])
            c["device_id"], c["sid"] = did, sid
        n = dict(s)
        n["device_id"], n["sid"], n["cfg"] = did, sid, "~/.claude"
        n["title"] = (s["title"] or "") + " (rerun)"
        derive(n, calls)
        n["compactions_json"], n["cost_json"] = "[]", None
        add(n, calls)

    # ---- overrides (one deliberate correction) -----------------------------------------------------------------------
    conn.execute("INSERT INTO account_overrides(account_uuid,device_id,project_like,from_ts,to_ts,note) VALUES(?,?,?,?,?,?)",
                 (ACCOUNTS["A"]["account_uuid"], dev_ids["vm"], "qbank", None, None,
                  "qbank on the VM is billed to my personal account"))

    # ---- write sessions + calls ------------------------------------------------------------------------------------------
    scols = ("device_id,sid,cfg,title,title_kind,project,cwd,branch,worktree,entrypoint,version,first_ts,last_ts,"
             "first_epoch,last_epoch,active_s,prompts,calls,web_requests,tools_json,mcp_json,subagents_json,"
             "tokens_json,ctx_peak,ctx_avg,compactions_json,cost_json,account_uuid,account_conf,updated").split(",")
    conn.executemany("INSERT OR REPLACE INTO sessions(%s) VALUES(%s)" % (",".join(scols), ",".join("?" * len(scols))),
                     [tuple(s.get(c) for c in scols) for s in sess_rows])
    ccols = "device_id,sid,sub,mid,ts,ts_epoch,model,inp,out,cr,cw5,cw1,think,ctx,web,speed,effort,tools_json".split(",")
    conn.executemany("INSERT OR REPLACE INTO calls(%s) VALUES(%s)" % (",".join(ccols), ",".join("?" * len(ccols))),
                     [tuple(c.get(k) for k in ccols) for c in call_rows])
    conn.commit()

    # ---- synthetic utilization snapshots (consistent with the calls + the override) ------------------------------------------
    per = {}
    for ts, usd, acct, dev, sid in ccm_calc.load_calls(conn):
        per.setdefault(acct, []).append((ts, usd, dev))
    for key, acc in ACCOUNTS.items():
        calls = per.get(acc["account_uuid"], [])
        if not calls:
            continue
        # anchored 5h windows (greedy) and their $
        wins, cur = [], None
        for ts, usd, dev in calls:
            if cur is None or ts >= cur[1]:
                cur = [ts, ts + 5 * 3600, 0.0]
                wins.append(cur)
            cur[2] += usd
        tot5 = sorted(w[2] for w in wins)
        cap5 = 1.1 * tot5[int(0.95 * (len(tot5) - 1))]
        wk_end = ccm_calc.ts_epoch(acc["week_end"][:19])
        wk = {}
        for ts, usd, dev in calls:
            b = ccm_calc.weekly_bucket(ts, wk_end)
            wk[b] = wk.get(b, 0.0) + usd
        cap7 = 1.0 * max(wk.values()) / 0.85
        ts_list = [c[0] for c in calls]
        cum, run = [], 0.0
        for c in calls:
            run += c[1]
            cum.append(run)
        import bisect

        def cum_at(t):
            k = bisect.bisect_right(ts_list, t)
            return cum[k - 1] if k else 0.0
        nxt = 0.0
        wi = -1
        for i, (ts, usd, dev) in enumerate(calls):
            if ts < nxt:
                continue
            nxt = ts + rng.expovariate(1.0 / (50 * 60))
            while wi + 1 < len(wins) and wins[wi + 1][0] <= ts:
                wi += 1
            w = wins[wi]
            used5 = cum_at(ts) - cum_at(w[0] - 1e-3)
            wb = ccm_calc.weekly_bucket(ts, wk_end)
            used7 = cum_at(ts) - cum_at(wb[0])
            u5 = min(100, int(round(100 * used5 * (1 + rng.uniform(0, 0.06)) / cap5)))
            u7 = min(100, int(round(100 * used7 * (1 + rng.uniform(0, 0.06)) / cap7)))
            cc = rng.choice((97, 98, 99, 100, 100))
            body = {"fetched_ms": int(ts * 1000), "account_uuid": acc["account_uuid"],
                    "five_hour": {"utilization": u5, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(w[1])) + ".000000+00:00"},
                    "seven_day": {"utilization": u7, "resets_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wb[1])) + ".000000+00:00"},
                    "breakdown": {"as_of": iso(ts), "rows": {"claude_code": cc, "chat": 100 - cc, "cowork": 0, "other": 0}},
                    "extra_usage": {"is_enabled": False}}
            conn.execute("INSERT OR IGNORE INTO util_snapshots VALUES(?,?,?,?)",
                         (body["fetched_ms"], acc["account_uuid"], dev, json.dumps(body, separators=(",", ":"))))
    # ---- import log ------------------------------------------------------------------------------------------------------------------
    for d in DEVICES:
        for k, day in enumerate(("20261003", "20261006")):
            conn.execute("INSERT INTO imports VALUES(?,?,?,?,?)",
                         (hashlib.sha256(("%s%s" % (d["key"], day)).encode()).hexdigest(),
                          "ccm-%s-%s.ndjson.gz" % (d["label"].replace(" ", "-").replace("(", "").replace(")", ""), day),
                          "%s-%s-%sT06:25:1%dZ" % (day[:4], day[4:6], day[6:], k), 1000 + rng.randint(0, 9000), dev_ids[d["key"]]))
    conn.commit()
    n_s = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    n_c = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    n_u = conn.execute("SELECT count(*) FROM util_snapshots").fetchone()[0]
    conn.close()
    print("wrote %s: %d conversations, %d calls, %d utilization snapshots, %d devices, %d accounts"
          % (out_dir / "ccm.db", n_s, n_c, n_u, len(DEVICES), len(ACCOUNTS)))
    print("injected spikes:")
    for line in INJECTED:
        print("  - " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
