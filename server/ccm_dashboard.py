"""CCM local web dashboard. Python 3.8+, stdlib only.

  python3 server/ccm_server.py dashboard [--port 8788] [--open]

Read-only JSON API over ccm.db (plus a few small write endpoints, see WRITE_ROUTES) and a static single-page app
from server/static/. Binds to 127.0.0.1 by default because the data is sensitive. No external network calls.
"""
import bisect
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccm_anomaly  # noqa: E402
import ccm_calc  # noqa: E402
import ccm_store  # noqa: E402
from ccm_store import Filt, Holder, jload, r2  # noqa: E402

VERSION = "1.0.0"
STATIC = Path(__file__).resolve().parent / "static"
MAX_JSON_BODY = 64 * 1024
MAX_IMPORT_BODY = 512 * 1024 * 1024
DROP_POLL_S = 5.0
GH_POLL_S = 60.0
CTX_EDGES = [0, 10000, 25000, 50000, 75000, 100000, 150000, 200000, 300000, 400000, 600000, 800000, 1000000]
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".json": "application/json", ".map": "application/json"}


class ApiError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


# --------------------------------------------------------------------------- helpers

def esc_html(s):
    """HTML-escape helper (the UI has its own copy of this; kept here for server-rendered fragments and tests)."""
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def conv_status(s):
    if s["overridden"]:
        return "override"
    return s["account_conf"] or "unknown"


def cost_cc(s):
    c = jload(s.get("cost_json"), {})
    v = c.get("totalCostUSD") if isinstance(c, dict) else None
    return float(v) if isinstance(v, (int, float)) else None


def session_row(st, si, agg=None):
    s = st.sessions[si]
    est = st.s_est[si] or {}
    if agg:
        calls, usd, tok = agg[0], agg[1], agg[2]
    else:
        calls, usd, tok = st.s_calls[si], st.s_usd[si], st.s_tok[si]
    return {"d": s["device_id"], "sid": s["sid"], "title": s["title"] or "", "tk": s["title_kind"],
            "project": s["project"] or "", "dev": s["dev_i"], "acct": s["acct_i"], "conf": conv_status(s),
            "entry": s["entrypoint"] or "", "first": r2(s["first_epoch"], 0), "last": r2(s["last_epoch"], 0),
            "active": s["active_s"] or 0, "prompts": s["prompts"] or 0, "calls": calls, "tokens": tok,
            "usd": r2(usd, 4), "p5": r2(est.get("pct5_peak"), 1), "p5s": r2(est.get("pct5_sum"), 1),
            "p7": r2(est.get("pct7_sum"), 1), "ctx": s["ctx_peak"] or 0, "compact": s["n_compact"],
            "subs": s["n_sub"]}


def dims(st):
    return {"devices": [{"id": d["device_id"], "name": d["name"], "tz": d["tz_offset_min"], "os": d["os"]}
                        for d in st.devices],
            "accounts": [{"id": a["account_uuid"], "name": a["name"], "email": a["email"]} for a in st.accounts],
            "models": list(st.models)}


# --------------------------------------------------------------------------- GET endpoints

def api_meta(ctx, st, f, qs):
    proj = {}
    ent = {}
    for s in st.sessions:
        proj[s["project_label"]] = proj.get(s["project_label"], 0) + 1
        ent[s["entry_label"]] = ent.get(s["entry_label"], 0) + 1
    drop = ctx.drop_info()
    out = dims(st)
    out.update({
        "projects": sorted(([k, v] for k, v in proj.items()), key=lambda kv: -kv[1])[:1000],
        "entrypoints": sorted(([k, v] for k, v in ent.items()), key=lambda kv: -kv[1]),
        "range": {"min": st.t_min, "max": st.t_max}, "sessions": len(st.sessions), "calls": st.n_calls,
        "empty": st.n_calls == 0 and not st.sessions, "version": VERSION, "built_at": st.built_at,
        "build_seconds": round(st.build_seconds, 2), "drop": drop, "now": time.time(),
        "device_tz": [st.dev_tz(i) for i in range(len(st.devices))],
        "account_info": [{"id": a["account_uuid"], "name": a["name"], "email": a["email"], "nickname": a["nickname"],
                          "plan": a["plan"]} for a in st.accounts],
    })
    return out


def api_overview(ctx, st, f, qs):
    idx = st.select(f)
    cur = st.totals(idx)
    prev = None
    if f.t0 is not None and f.t1 is not None and f.t1 > f.t0:
        pidx = st.select(f.shifted(-(f.t1 - f.t0)))
        prev = st.totals(pidx)
    for t in (cur, prev):
        if t:
            t["usd"] = round(t["usd"], 4)
            t["active_s"] = int(t["active_s"])
    proj = {}
    for si, a in st.per_session(idx).items():
        p = proj.setdefault(st.sessions[si]["project_label"], [0.0, 0, 0])
        p[0] += a[1]
        p[1] += a[0]
        p[2] += 1
    top = sorted(proj.items(), key=lambda kv: -kv[1][0])[:12]
    return {"cur": cur, "prev": prev,
            "projects": [{"project": k, "usd": round(v[0], 3), "calls": v[1], "conversations": v[2]} for k, v in top]}


def api_cube(ctx, st, f, qs):
    out = dims(st)
    out["cols"] = ["hour", "dev", "acct", "model", "calls", "usd", "inp", "out", "cr", "cw", "think", "active_s"]
    out["rows"] = st.cube(st.select(f))
    return out


def sort_val(row, key):
    v = row.get(key)
    return (v is None, v if not isinstance(v, str) else v.lower())


SORT_KEYS = {"title": "title", "project": "project", "device": "dev", "account": "acct", "entry": "entry",
             "start": "first", "active": "active", "prompts": "prompts", "calls": "calls", "tokens": "tokens",
             "usd": "usd", "p5": "p5", "p7": "p7", "ctx": "ctx", "compact": "compact", "subs": "subs"}


def api_sessions(ctx, st, f, qs):
    idx = st.select(f)
    agg = st.per_session(idx)
    mask = st.session_mask(f)
    rows = [session_row(st, si, a) for si, a in agg.items()]
    if not f.call_level():           # conversations without any recorded call are still conversations
        have = set(agg)
        rows.extend(session_row(st, si) for si in range(len(st.sessions)) if mask[si] and si not in have)
    for r in rows:
        r["dev"] = st.devices[r["dev"]]["name"]
        r["acct_id"] = st.accounts[r["acct"]]["account_uuid"]
        r["acct"] = st.accounts[r["acct"]]["name"]
    key = SORT_KEYS.get((qs.get("sort") or ["start"])[0], "first")
    rev = (qs.get("dir") or ["desc"])[0] != "asc"
    nn = [r for r in rows if r.get(key) is not None]
    nulls = [r for r in rows if r.get(key) is None]
    nn.sort(key=lambda r: r[key].lower() if isinstance(r[key], str) else r[key], reverse=rev)
    rows = nn + nulls
    try:
        limit = max(1, min(2000, int((qs.get("limit") or ["300"])[0])))
        offset = max(0, int((qs.get("offset") or ["0"])[0]))
    except ValueError:
        raise ApiError("bad limit/offset")
    return {"total": len(rows), "offset": offset, "rows": rows[offset:offset + limit]}


def api_conversation(ctx, st, f, qs):
    did = (qs.get("device") or [""])[0]
    sid = (qs.get("sid") or [""])[0]
    si = st.session_lookup(did, sid)
    if si is None:
        raise ApiError("conversation not found", 404)
    s = st.sessions[si]
    idxs = st.by_session.get(si, [])
    c_ts, c_ctx, c_inp, c_out, c_cr, c_cw, c_think, c_usd, c_model, c_sub, c_tools = (
        st.c_ts, st.c_ctx, st.c_inp, st.c_out, st.c_cr, st.c_cw, st.c_think, st.c_usd, st.c_model, st.c_sub_id,
        st.c_tools)
    stride = max(1, int(math.ceil(len(idxs) / 6000.0)))
    shown = idxs[::stride]
    calls = [[round(c_ts[i], 2), c_ctx[i], c_inp[i], c_out[i], c_cr[i], c_cw[i], c_think[i], round(c_usd[i], 5),
              c_model[i], c_sub[i], list(c_tools[i])] for i in shown]
    models, subs = {}, {}
    tools = {}
    for i in idxs:
        m = models.setdefault(st.models[c_model[i]], [0, 0.0, 0, 0, 0, 0, 0])
        m[0] += 1
        m[1] += c_usd[i]
        m[2] += c_inp[i]
        m[3] += c_out[i]
        m[4] += c_cr[i]
        m[5] += c_cw[i]
        m[6] += c_think[i]
        if c_sub[i] >= 0:
            sa = subs.setdefault(st.sub_names[c_sub[i]], [0, 0.0, 0, c_ts[i], c_ts[i], 0])
            sa[0] += 1
            sa[1] += c_usd[i]
            sa[2] += c_inp[i] + c_out[i] + c_cr[i] + c_cw[i]
            sa[4] = c_ts[i]
            sa[5] = max(sa[5], c_ctx[i])
        for t in c_tools[i]:
            tools[t] = tools.get(t, 0) + 1
    cost = jload(s.get("cost_json"), {})
    cc_models = {}
    if isinstance(cost, dict):
        for m, v in (cost.get("modelUsage") or {}).items():
            if isinstance(v, dict):
                cc_models[m] = v.get("costUSD")
    est = st.s_est[si] or {}
    res_w5 = []
    caps = st.res["caps"].get(s["account"]) or {}
    for (a, b), usd in sorted((est.get("w5") or {}).items()):
        res_w5.append({"start": a, "end": b, "usd": round(usd, 4),
                       "pct": r2(100 * usd / caps["cap_5h"], 1) if caps.get("cap_5h") else None})
    res_w7 = []
    for (a, b), usd in sorted((est.get("w7") or {}).items()):
        res_w7.append({"start": a, "end": b, "usd": round(usd, 4),
                       "pct": r2(100 * usd / caps["cap_7d"], 1) if caps.get("cap_7d") else None})
    row = session_row(st, si)
    row["dev"] = st.devices[s["dev_i"]]["name"]
    row["acct_id"] = s["account"]
    row["acct"] = st.accounts[s["acct_i"]]["name"]
    return {
        "row": row, "cfg": s["cfg"], "cwd": s["cwd"], "branch": s["branch"], "worktree": bool(s["worktree"]),
        "version": s["version"], "first_ts": s["first_ts"], "last_ts": s["last_ts"],
        "original_account": s["account_uuid"], "overridden": s["overridden"], "conf": s["account_conf"],
        "calls": calls, "stride": stride, "ncalls": len(idxs),
        "models": [{"model": k, "calls": v[0], "usd": round(v[1], 4), "inp": v[2], "out": v[3], "cr": v[4],
                    "cw": v[5], "think": v[6], "cc_usd": cc_models.get(k)} for k, v in
                   sorted(models.items(), key=lambda kv: -kv[1][1])],
        "tools": sorted(([k, v] for k, v in tools.items()), key=lambda kv: -kv[1]),
        "mcp": jload(s.get("mcp_json"), {}),
        "subagents": sorted(({"id": k, "calls": v[0], "usd": round(v[1], 4), "tokens": v[2], "first": v[3],
                              "last": v[4], "ctx_peak": v[5]} for k, v in subs.items()), key=lambda x: -x["usd"]),
        "subagents_meta": jload(s.get("subagents_json"), {}),
        "compactions": jload(s.get("compactions_json"), []),
        "cc_cost": cost_cc(s), "est_usd": round(st.s_usd[si], 4),
        "windows5": res_w5, "windows7": res_w7, "web_requests": s["web_requests"], "ctx_avg": s["ctx_avg"],
        "tokens_json": jload(s.get("tokens_json"), {}),
    }


def api_tools(ctx, st, f, qs):
    idx = st.select(f)
    tools, mcp = {}, {}
    sp, ef, mods = {}, {}, {}
    c_tools, c_speed, c_effort, c_usd, c_model = st.c_tools, st.c_speed, st.c_effort, st.c_usd, st.c_model
    c_inp, c_out, c_cr, c_cw, c_think = st.c_inp, st.c_out, st.c_cr, st.c_cw, st.c_think
    for i in idx:
        for t in c_tools[i]:
            if t.startswith("mcp__"):
                parts = t.split("__", 2)
                server, tn = (parts[1], parts[2]) if len(parts) == 3 else (parts[1], "?")
                m = mcp.setdefault(server, {})
                m[tn] = m.get(tn, 0) + 1
            else:
                tools[t] = tools.get(t, 0) + 1
        a = sp.setdefault(st.speeds[c_speed[i]], [0, 0.0])
        a[0] += 1
        a[1] += c_usd[i]
        a = ef.setdefault(st.efforts[c_effort[i]], [0, 0.0])
        a[0] += 1
        a[1] += c_usd[i]
        m = mods.setdefault(c_model[i], [0, 0.0, 0, 0, 0, 0, 0])
        m[0] += 1
        m[1] += c_usd[i]
        m[2] += c_inp[i]
        m[3] += c_out[i]
        m[4] += c_cr[i]
        m[5] += c_cw[i]
        m[6] += c_think[i]
    return {
        "tools": sorted(([k, v] for k, v in tools.items()), key=lambda kv: -kv[1])[:80],
        "mcp": sorted(({"server": k, "calls": sum(v.values()),
                        "tools": sorted(([a, b] for a, b in v.items()), key=lambda kv: -kv[1])}
                       for k, v in mcp.items()), key=lambda x: -x["calls"]),
        "speeds": sorted(([k, v[0], round(v[1], 3)] for k, v in sp.items()), key=lambda x: -x[1]),
        "efforts": sorted(([k, v[0], round(v[1], 3)] for k, v in ef.items()), key=lambda x: -x[1]),
        "models": sorted(({"model": st.models[k], "calls": v[0], "usd": round(v[1], 3), "inp": v[2], "out": v[3],
                           "cr": v[4], "cw": v[5], "think": v[6]} for k, v in mods.items()),
                         key=lambda x: -x["usd"]),
    }


def api_context(ctx, st, f, qs):
    idx = st.select(f)
    c_ctx = st.c_ctx
    vals = sorted(c_ctx[i] for i in idx if c_ctx[i] > 0)
    hist = [0] * len(CTX_EDGES)
    for v in vals:
        hist[bisect.bisect_right(CTX_EDGES, v) - 1] += 1
    pct = {}
    if vals:
        for q in (50, 75, 90, 95, 99):
            pct["p%d" % q] = vals[min(len(vals) - 1, int(len(vals) * q / 100.0))]
        pct["max"] = vals[-1]
    agg = st.per_session(idx)
    pts = []
    comp_hist = {}
    triggers = {}
    pre_vals = []
    for si, a in agg.items():
        s = st.sessions[si]
        pts.append({"d": s["device_id"], "sid": s["sid"], "t": r2(s["first_epoch"], 0), "ctx": s["ctx_peak"] or 0,
                    "usd": r2(a[1], 3), "title": s["title"] or "", "dev": s["dev_i"], "acct": s["acct_i"],
                    "nc": s["n_compact"], "calls": a[0]})
        k = min(s["n_compact"], 6)
        comp_hist[k] = comp_hist.get(k, 0) + 1
        if s["n_compact"]:
            for c in jload(s.get("compactions_json"), []):
                tg = c.get("trigger") or "?"
                triggers[tg] = triggers.get(tg, 0) + 1
                if c.get("pre"):
                    pre_vals.append(c["pre"])
    pts.sort(key=lambda p: -p["usd"] if p["usd"] is not None else 0)
    top = sorted(pts, key=lambda p: -p["ctx"])[:25]
    if len(pts) > 3000:
        pts = pts[:1500] + pts[1500::max(1, (len(pts) - 1500) // 1500)]
    peaks = [st.sessions[si]["ctx_peak"] or 0 for si in agg]
    big = {str(th): sum(1 for v in peaks if v >= th) for th in (100000, 200000, 400000, 800000)}
    pre_vals.sort()
    return {"edges": CTX_EDGES, "hist": hist, "pct": pct, "points": pts, "comp_hist": sorted(comp_hist.items()),
            "triggers": triggers, "compactions": len(pre_vals), "pre_median": pre_vals[len(pre_vals) // 2] if pre_vals else None,
            "big": big, "top": top, "n_sessions": len(agg), "n_calls": len(vals)}


def window_json(st, w, cap):
    return {"acct": w["acct_i"], "start": w["start"], "end": w["end"], "usd": round(w["usd"], 4),
            "pct": r2(100 * w["usd"] / cap, 1) if cap else None, "calls": w["calls"], "n_sess": w["n_sess"],
            "devs": w["devs"],
            "top": [{"d": st.sessions[si]["device_id"], "sid": st.sessions[si]["sid"],
                     "title": st.sessions[si]["title"] or "", "usd": round(u, 3)} for si, u in w["top"][:3]]}


def api_limits(ctx, st, f, qs):
    out = []
    t0, t1 = f.t0, f.t1
    for ai, a in enumerate(st.accounts):
        uuid = a["account_uuid"]
        if f.accounts and uuid not in f.accounts:
            continue
        caps = st.res["caps"].get(uuid) or {}
        snaps = st.snap_series.get(uuid, [])
        w5 = [w for w in st.win5 if w["acct_i"] == ai]
        w7 = [w for w in st.win7 if w["acct_i"] == ai]
        if not (caps or snaps or w5):
            continue
        c5, c7 = caps.get("cap_5h"), caps.get("cap_7d")

        def in_range(a_, b_):
            return (t1 is None or a_ < t1) and (t0 is None or b_ > t0)
        s_out = [[round(s["t"]), s["r5"], s["r7"], r2(s["e5"], 1), r2(s["e7"], 1), s["cc"], s["r5_end"], s["r7_end"]]
                 for s in snaps if (t0 is None or s["t"] >= t0) and (t1 is None or s["t"] < t1)]
        devs = sorted({d for d, _ in st.dev_cfgs.get(uuid, [])})
        man = st.manual_caps.get(uuid)
        out.append({
            "id": uuid, "name": a["name"], "email": a["email"], "plan": a["plan"],
            "caps": {"cap_5h": r2(c5, 2), "cap_7d": r2(c7, 2), "n_5h": caps.get("n_5h", 0),
                     "n_7d": caps.get("n_7d", 0), "source": caps.get("source"),
                     "manual_5h": man[0] if man else None, "manual_7d": man[1] if man else None,
                     "note": man[2] if man else None,
                     "samples_5h": [round(x, 1) for x in (caps.get("samples_5h") or [])[:400]],
                     "samples_7d": [round(x, 1) for x in (caps.get("samples_7d") or [])[:400]]},
            "snaps": s_out,
            "windows5": [window_json(st, w, c5) for w in w5 if in_range(w["start"], w["end"])],
            "windows7": [window_json(st, w, c7) for w in w7 if in_range(w["start"], w["end"])],
            "devices": [st.devices[st.dev_index[d]]["name"] for d in devs if d in st.dev_index],
            "devices_total_seen": len({s["device_id"] for s in st.sessions if s["account"] == uuid}),
        })
    return {"accounts": out, "dev_names": [d["name"] for d in st.devices]}


def api_anomalies(ctx, st, f, qs):
    try:
        sens = float((qs.get("sens") or ["5"])[0])
        tz = int(float((qs.get("tz") or ["0"])[0]))
        limit = max(1, min(1000, int((qs.get("limit") or ["300"])[0])))
    except ValueError:
        raise ApiError("bad parameter")
    key = (round(sens, 2), tz)
    cache = st.__dict__.setdefault("_anom_cache", {})
    items = cache.get(key)
    if items is None:
        items = ccm_anomaly.detect(st, sens, tz)
        if len(cache) > 8:
            cache.clear()
        cache[key] = items
    mask = st.session_mask(f)
    session_filters = bool(f.projects or f.entrypoints or f.q)
    kinds = set((qs.get("kinds") or [""])[0].split(",")) - {""}
    out = []
    counts = {}
    for a in items:
        if kinds and a["kind"] not in kinds:
            continue
        if f.t0 is not None and (a["ts_end"] or a["ts"]) < f.t0:
            continue
        if f.t1 is not None and a["ts"] >= f.t1:
            continue
        if a["sid"]:
            si = st.sess_index.get((a["device_id"], a["sid"]))
            if si is None or not mask[si]:
                continue
        else:
            if session_filters:
                continue
            if f.devices and a["device_id"] and a["device_id"] not in f.devices:
                continue
            if f.accounts and a["account"] and a["account"] not in f.accounts:
                continue
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
        out.append(a)
    res = []
    for a in out[:limit]:
        b = dict(a)
        b["device"] = st.devices[st.dev_index[a["device_id"]]]["name"] if a["device_id"] in st.dev_index else None
        ai = st.acct_index.get(a["account"]) if a["account"] else None
        b["account_name"] = st.accounts[ai]["name"] if ai is not None else None
        for k in ("value", "baseline"):
            if isinstance(b.get(k), float):
                b[k] = round(b[k], 4)
        res.append(b)
    return {"items": res, "total": len(out), "counts": counts, "kinds": ccm_anomaly.KINDS,
            "params": {k: round(v, 3) if isinstance(v, float) else v for k, v in ccm_anomaly.params(sens).items()}}


def api_admin(ctx, st, f, qs):
    sess_counts = {}
    for s in st.sessions:
        k = (s["device_id"], s["account"])
        sess_counts[k] = sess_counts.get(k, 0) + 1
    devices = []
    for i, d in enumerate(st.devices):
        row = dict(d)
        row["sessions"] = sum(v for (dv, _), v in sess_counts.items() if dv == d["device_id"])
        devices.append(row)
    accounts = []
    for a in st.accounts:
        row = dict(a)
        row["sessions"] = sum(v for (_, ac), v in sess_counts.items() if ac == a["account_uuid"])
        row["cfgs"] = [{"device": st.devices[st.dev_index[dv]]["name"] if dv in st.dev_index else dv, "cfg": c}
                       for dv, c in st.dev_cfgs.get(a["account_uuid"], [])]
        accounts.append(row)
    return {"devices": devices, "accounts": accounts, "overrides": st.overrides, "prices": st.price_rows,
            "imports": st.imports, "drop": ctx.drop_info(), "data_dir": str(ctx.data_dir),
            "dev_names": {d["device_id"]: d["name"] for d in st.devices}}


def api_github_status(ctx, st, f, qs):
    return ctx.gh_status(fresh=(qs.get("fresh") or [""])[0] == "1")


GET_ROUTES = {
    "/api/github/status": api_github_status,
    "/api/meta": api_meta, "/api/overview": api_overview, "/api/cube": api_cube, "/api/sessions": api_sessions,
    "/api/conversation": api_conversation, "/api/tools": api_tools, "/api/context": api_context,
    "/api/limits": api_limits, "/api/anomalies": api_anomalies, "/api/admin": api_admin,
}


# --------------------------------------------------------------------------- write endpoints

ISO_RE = re.compile(r"^\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d(\.\d+)?)?)?Z?$")


def _s(body, key, maxlen=200, required=False):
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise ApiError("missing field: %s" % key)
        return None
    if not isinstance(v, str) or len(v) > maxlen:
        raise ApiError("bad field: %s" % key)
    return v.strip() or None


def _num(body, key, required=False):
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise ApiError("missing field: %s" % key)
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ApiError("bad number: %s" % key)
    if not math.isfinite(x) or x < 0 or x > 1e9:
        raise ApiError("number out of range: %s" % key)
    return x


def _account_exists(conn, uuid):
    if not conn.execute("SELECT 1 FROM accounts WHERE account_uuid=?", (uuid,)).fetchone():
        raise ApiError("unknown account")


def w_override(conn, body):
    acct = _s(body, "account_uuid", 100, True)
    _account_exists(conn, acct)
    dev = _s(body, "device_id", 100)
    proj = _s(body, "project_like", 200)
    f_ts, t_ts, note = _s(body, "from_ts", 40), _s(body, "to_ts", 40), _s(body, "note", 300)
    for v in (f_ts, t_ts):
        if v and not ISO_RE.match(v):
            raise ApiError("dates must look like 2026-09-30 or 2026-09-30T12:00:00Z")
    if not (dev or proj or f_ts or t_ts):
        raise ApiError("an override needs at least one condition (device, project, from or to)")
    cur = conn.execute("INSERT INTO account_overrides(account_uuid,device_id,project_like,from_ts,to_ts,note) "
                       "VALUES(?,?,?,?,?,?)", (acct, dev, proj, f_ts, t_ts, note))
    return {"id": cur.lastrowid}


def w_override_delete(conn, body):
    try:
        i = int(body.get("id"))
    except (TypeError, ValueError):
        raise ApiError("bad id")
    conn.execute("DELETE FROM account_overrides WHERE id=?", (i,))
    return {}


def w_account(conn, body):
    acct = _s(body, "account_uuid", 100, True)
    _account_exists(conn, acct)
    conn.execute("UPDATE accounts SET nickname=? WHERE account_uuid=?", (_s(body, "nickname", 60), acct))
    return {}


def w_device(conn, body):
    dev = _s(body, "device_id", 100, True)
    if not conn.execute("SELECT 1 FROM devices WHERE device_id=?", (dev,)).fetchone():
        raise ApiError("unknown device")
    conn.execute("UPDATE devices SET label=? WHERE device_id=?", (_s(body, "label", 60), dev))
    return {}


def w_capacity(conn, body):
    acct = _s(body, "account_uuid", 100, True)
    _account_exists(conn, acct)
    c5, c7 = _num(body, "cap_5h"), _num(body, "cap_7d")
    note = _s(body, "note", 200)
    conn.execute("INSERT OR IGNORE INTO account_capacity(account_uuid) VALUES(?)", (acct,))
    conn.execute("UPDATE account_capacity SET cap_5h=?,cap_7d=?,note=? WHERE account_uuid=?",
                 (c5 or None, c7 or None, note, acct))
    conn.execute("DELETE FROM account_capacity WHERE account_uuid=? AND cap_5h IS NULL AND cap_7d IS NULL",
                 (acct,))
    return {}


def w_price(conn, body):
    pat = _s(body, "pattern", 100, True)
    vals = [_num(body, k, True) for k in ("in_p", "out_p", "cr_p", "cw5_p", "cw1_p")]
    conn.execute("INSERT OR REPLACE INTO price_table(pattern,in_p,out_p,cr_p,cw5_p,cw1_p,note) VALUES(?,?,?,?,?,?,?)",
                 [pat] + vals + [_s(body, "note", 200)])
    return {}


def w_price_delete(conn, body):
    pat = _s(body, "pattern", 100, True)
    if pat == "*":
        raise ApiError("the '*' fallback price cannot be deleted")
    conn.execute("DELETE FROM price_table WHERE pattern=?", (pat,))
    return {}


WRITE_ROUTES = {
    "/api/override": w_override, "/api/override/delete": w_override_delete, "/api/account": w_account,
    "/api/device": w_device, "/api/capacity": w_capacity, "/api/price": w_price,
    "/api/price/delete": w_price_delete,
}


def _gh_wrap(fn):
    def inner(app, body):
        try:
            return fn(app, body)
        except ApiError:
            raise
        except ValueError as e:
            raise ApiError(str(e))
        except Exception as e:  # GhError / network: shown in the UI
            raise ApiError("GitHub: %s" % (str(e) or e.__class__.__name__), 502)
    return inner


def post_gh_request(app, body):
    devs = body.get("devices")
    if devs is not None and (not isinstance(devs, list) or not all(isinstance(x, str) and len(x) < 100 for x in devs)):
        raise ApiError("devices must be a list of labels")
    ah = body.get("auto_hours")
    if ah is not None:
        ah = _num({"v": ah}, "v")
    trigger = body.get("trigger", True) is not False
    if not trigger and ah is None:
        raise ApiError("nothing to do")
    return app.gh_request(devs, ah, trigger)


def post_gh_pull(app, body):
    return {"results": app.gh_do_pull()}


GH_POST = {"/api/github/request": _gh_wrap(post_gh_request), "/api/github/pull": _gh_wrap(post_gh_pull)}


# --------------------------------------------------------------------------- app context (db access, import, drop folder)

class App:
    def __init__(self, data_dir):
        import ccm_server
        self.ccm_server = ccm_server
        self.data_dir = Path(data_dir)
        self.drop_dir = self.data_dir / "drop"
        for sub in ("", "done", "failed"):
            (self.drop_dir / sub).mkdir(parents=True, exist_ok=True)
        (self.data_dir / "inbox").mkdir(parents=True, exist_ok=True)
        ccm_server.open_db(self.data_dir).close()      # create schema + default prices on a fresh data dir
        self.db_path = self.data_dir / "ccm.db"
        self.holder = Holder(self.open_ro)
        self.write_lock = threading.Lock()
        self.drop_log = []
        self.stop = threading.Event()
        self.gh_lock = threading.Lock()            # one GitHub pull at a time (loop vs "Pull now")
        self.gh_pull = {"at": None, "results": [], "error": None, "running": False}
        self.gh_cache = (0.0, None)                # (time, remote status) so the UI can poll cheaply

    def open_ro(self):
        conn = sqlite3.connect("file:%s?mode=ro" % self.db_path.as_posix(), uri=True, timeout=15)
        conn.execute("PRAGMA query_only=1")
        return conn

    def write(self, fn, body):
        with self.write_lock:
            conn = self.ccm_server.open_db(self.data_dir)
            try:
                with conn:
                    res = fn(conn, body)
            finally:
                conn.close()
        self.holder.invalidate()
        res = res or {}
        res["ok"] = True
        return res

    def do_import(self, data, filename):
        """Import one export file. Returns a result dict (status ok | duplicate | error)."""
        with self.write_lock:
            conn = self.ccm_server.open_db(self.data_dir)
            try:
                res = self.ccm_server.ingest_bytes(conn, data, filename, self.data_dir)
                did = res.get("device_id")
                if not did:
                    row = conn.execute("SELECT device_id FROM imports WHERE filename=? ORDER BY received_at DESC",
                                       (res.get("file") or filename,)).fetchone()   # duplicate: name it was first imported as
                    if res.get("status") == "duplicate":
                        res["first_imported_as"] = res.get("file")
                    did = row[0] if row else None
                if did:
                    row = conn.execute("SELECT label,hostname FROM devices WHERE device_id=?", (did,)).fetchone()
                    res["device_id"] = did
                    res["device"] = (row[0] or row[1]) if row else None
            except Exception as e:  # malformed file, schema mismatch, db error
                conn.close()
                return {"status": "error", "file": filename, "error": str(e) or e.__class__.__name__}
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        res["file"] = filename
        self.holder.invalidate()
        return res

    # GitHub transport (ccm_gh.py). Everything here may be slow or fail: callers get errors, never exceptions. ---
    def gh_configured(self):
        import ccm_gh
        return ccm_gh.load_config(self.data_dir) is not None

    def gh_status(self, fresh=False):
        import ccm_gh
        if not self.gh_configured():
            return {"configured": False, "pull": self.gh_pull, "data_dir": str(self.data_dir)}
        t, cached = self.gh_cache
        if not fresh and cached is not None and time.time() - t < 8:
            out = dict(cached)
        else:
            conn = self.ccm_server.open_db(self.data_dir)
            try:
                out = ccm_gh.status(self.data_dir, conn)
            except Exception as e:  # network down, bad token, ...
                out = {"configured": True, "error": str(e), "devices": [], "last_request": None}
            finally:
                conn.close()
            self.gh_cache = (time.time(), out)
        out = dict(out)
        out["pull"] = self.gh_pull
        out["pull_interval"] = GH_POLL_S
        return out

    def gh_request(self, devices, auto_hours, trigger):
        import ccm_gh
        req = ccm_gh.request_collect(self.data_dir, devices or None, auto_hours, trigger)
        self.gh_cache = (0.0, None)
        return {"request": req}

    def gh_do_pull(self):
        """Pull + import every changed device branch. Returns the per-branch results (also kept in gh_pull)."""
        import ccm_gh
        if not self.gh_lock.acquire(False):
            raise ApiError("a pull is already running", 409)
        self.gh_pull["running"] = True
        try:
            with self.write_lock:
                conn = self.ccm_server.open_db(self.data_dir)
                try:
                    results = ccm_gh.pull_all(self.data_dir, conn)
                finally:
                    conn.close()
            self.gh_pull.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), results=results, error=None)
            if any(r.get("status") == "ok" for r in results):
                self.holder.invalidate()
            self.gh_cache = (0.0, None)
            return results
        except ValueError as e:
            self.gh_pull.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), results=[], error=str(e))
            raise ApiError(str(e))
        except Exception as e:  # GhError, OSError, sqlite3.Error: report, never crash
            self.gh_pull.update(at=time.strftime("%Y-%m-%dT%H:%M:%S"), results=[], error=str(e) or e.__class__.__name__)
            raise ApiError("GitHub pull failed: %s" % (str(e) or e.__class__.__name__), 502)
        finally:
            self.gh_pull["running"] = False
            self.gh_lock.release()

    def gh_loop(self):
        """Background pull every GH_POLL_S seconds while the transport is configured."""
        while not self.stop.wait(GH_POLL_S):
            try:
                if self.gh_configured():
                    self.gh_do_pull()
            except ApiError:
                pass            # already recorded in gh_pull for the UI
            except Exception:
                traceback.print_exc()

    # drop folder ------------------------------------------------------------
    def drop_info(self):
        pending = self._drop_candidates(age=0)
        return {"path": str(self.drop_dir), "pending": len(pending), "recent": self.drop_log[-20:][::-1],
                "poll_seconds": DROP_POLL_S}

    def _drop_candidates(self, age=1.5):
        out = []
        now = time.time()
        try:
            for p in sorted(self.drop_dir.iterdir()):
                n = p.name.lower()
                if p.is_file() and (n.endswith(".ndjson.gz") or n.endswith(".ndjson")) and not n.startswith("."):
                    if now - p.stat().st_mtime >= age:
                        out.append(p)
        except OSError:
            pass
        return out

    def poll_drop(self):
        """Import every settled export file in drop/ and move it to done/ or failed/. Returns number processed."""
        n = 0
        for p in self._drop_candidates():
            try:
                data = p.read_bytes()
            except OSError:
                continue
            res = self.do_import(data, p.name)
            ok = res.get("status") in ("ok", "duplicate")
            dest_dir = self.drop_dir / ("done" if ok else "failed")
            dest = dest_dir / p.name
            if dest.exists():
                dest = dest_dir / ("%s.%d" % (p.name, int(time.time())))
            try:
                shutil.move(str(p), str(dest))
                if not ok:
                    Path(str(dest) + ".error.txt").write_text(res.get("error", "unknown error") + "\n",
                                                             encoding="utf-8")
            except OSError as e:
                res.setdefault("error", str(e))
            res["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.drop_log.append(res)
            del self.drop_log[:-100]
            n += 1
        return n

    def drop_loop(self):
        while not self.stop.is_set():
            try:
                self.poll_drop()
            except Exception:
                traceback.print_exc()
            self.stop.wait(DROP_POLL_S)


# --------------------------------------------------------------------------- HTTP

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ccm-dashboard/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("CCM_DASH_QUIET"):
            return
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -- plumbing
    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        bind = self.server.bind_host
        if bind in ("0.0.0.0", "::", ""):
            return True
        return name in LOOPBACK or name == bind.lower()

    def _send(self, code, body, ctype, extra=None):
        accept = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept and len(body) > 1200 and (ctype.startswith("application/json") or ctype.startswith("text/")
                                                      or "javascript" in ctype):
            c = zlib.compressobj(5, zlib.DEFLATED, 31)
            body = c.compress(body) + c.flush()
            extra = dict(extra or {}, **{"Content-Encoding": "gzip"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Vary", "Accept-Encoding")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json",
                   {"Cache-Control": "no-store"})

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad Host header"})
        u = urlparse(self.path)
        path = u.path
        if path.startswith("/api/"):
            fn = GET_ROUTES.get(path)
            if not fn:
                return self._json(404, {"error": "not found"})
            try:
                qs = parse_qs(u.query, keep_blank_values=False)
                st = self.server.app.holder.get()
                return self._json(200, fn(self.server.app, st, Filt.from_query(qs), qs))
            except ApiError as e:
                return self._json(e.code, {"error": str(e)})
            except sqlite3.Error as e:
                return self._json(503, {"error": "database busy or unreadable: %s" % e})
            except Exception:
                traceback.print_exc()
                return self._json(500, {"error": "internal error"})
        return self._static(path)

    def _static(self, path):
        path = unquote(path)
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        if "\x00" in rel or ".." in rel.split("/") or "\\" in rel:
            return self._json(404, {"error": "not found"})
        p = (STATIC / rel).resolve()
        try:
            p.relative_to(STATIC.resolve())
        except ValueError:
            return self._json(404, {"error": "not found"})
        if not p.is_file():
            return self._json(404, {"error": "not found"})
        cache = "max-age=86400" if "/vendor/" in path else "no-cache"
        self._send(200, p.read_bytes(), MIME.get(p.suffix.lower(), "application/octet-stream"),
                   {"Cache-Control": cache})

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._json(403, {"error": "cross-origin request refused"})
        path = urlparse(self.path).path
        app = self.server.app
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        try:
            if path == "/api/import":
                if n <= 0 or n > MAX_IMPORT_BODY:
                    return self._json(413, {"error": "bad length"})
                name = unquote(self.headers.get("X-CCM-Filename") or "upload.ndjson.gz")
                name = os.path.basename(name.replace("\\", "/"))[:200] or "upload.ndjson.gz"
                res = app.do_import(self.rfile.read(n), name)
                return self._json(200 if res.get("status") in ("ok", "duplicate") else 400, res)
            fn = WRITE_ROUTES.get(path)
            if not fn and path not in GH_POST:
                return self._json(404, {"error": "not found"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "Content-Type must be application/json"})
            if n <= 0 or n > MAX_JSON_BODY:
                return self._json(413, {"error": "bad length"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                raise ApiError("invalid JSON")
            if not isinstance(body, dict):
                raise ApiError("JSON object expected")
            if path in GH_POST:
                return self._json(200, GH_POST[path](app, body))
            return self._json(200, app.write(fn, body))
        except ApiError as e:
            return self._json(e.code, {"error": str(e)})
        except sqlite3.Error as e:
            return self._json(503, {"error": "database error: %s" % e})
        except Exception:
            traceback.print_exc()
            return self._json(500, {"error": "internal error"})


def make_server(data_dir, host="127.0.0.1", port=8788):
    """Build (but do not start) the HTTP server; used by run() and the tests. port 0 = ephemeral."""
    app = App(data_dir)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app
    srv.bind_host = host
    return srv


def run(data_dir, host="127.0.0.1", port=8788, open_browser=False):
    srv = make_server(Path(data_dir), host, port)
    app = srv.app
    app.poll_drop()
    threading.Thread(target=app.drop_loop, daemon=True).start()
    threading.Thread(target=app.gh_loop, daemon=True).start()
    threading.Thread(target=app.holder.get, daemon=True).start()   # warm the cache
    url = "http://%s:%d/" % (host if host not in ("0.0.0.0", "::") else "127.0.0.1", srv.server_address[1])
    print("CCM dashboard: %s   (data: %s)" % (url, app.data_dir))
    print("Drop export files into %s to import them (checked every %ds)." % (app.drop_dir, int(DROP_POLL_S)))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to %s, the dashboard is reachable from the network and shows sensitive data." % host)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    finally:
        app.stop.set()
    return 0
