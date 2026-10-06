"""In-memory read model of ccm.db for the dashboard. Python 3.8+, stdlib only.

`Store.build(conn)` loads sessions + calls once (columnar arrays, API-equivalent $ per call, effective account,
5h/weekly windows from ccm_calc) so every filtered aggregate is a cheap pass over a pre-selected index list.
A Store is immutable after build(); the dashboard swaps in a new one when the database changes.
"""
import bisect
import json
import math
import re
import threading
import time
from array import array

import ccm_calc

GAP_ACTIVE_S = 300   # same rule as the agent: gaps shorter than this count as active time
NONE_LABEL = "(none)"


def jload(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def parse_time(v):
    """epoch seconds, or an ISO date/time (UTC). None when empty/invalid."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    v = str(v).strip()
    m = re.match(r"^(\d{4}-\d\d-\d\d)(?:[T ](\d\d:\d\d))?$", v)
    if m:   # date or date+minutes
        v = "%sT%s:00" % (m.group(1), m.group(2) or "00:00")
    return ccm_calc.ts_epoch(v)


class Filt:
    """Filters shared by every endpoint. Time range is [t0, t1)."""

    FIELDS = ("devices", "accounts", "projects", "models", "entrypoints")

    def __init__(self, t0=None, t1=None, devices=None, accounts=None, projects=None, models=None,
                 entrypoints=None, q=""):
        self.t0, self.t1 = t0, t1
        self.devices = frozenset(devices or ())
        self.accounts = frozenset(accounts or ())
        self.projects = frozenset(projects or ())
        self.models = frozenset(models or ())
        self.entrypoints = frozenset(entrypoints or ())
        self.q = (q or "").strip().lower()

    @classmethod
    def from_query(cls, qs):
        """qs: dict name -> list of strings (urllib.parse.parse_qs). Accepts repeated or comma separated values."""
        def many(name):
            out = []
            for key in (name, name + "[]"):
                for v in qs.get(key, []):
                    if name in ("projects", "entrypoints", "models") or "," not in v:
                        out.append(v)       # these may legitimately contain commas only via repetition
                    else:
                        out.extend(x for x in v.split(",") if x)
            return [x for x in out if x != ""]
        one = lambda n: (qs.get(n) or [""])[0]  # noqa: E731
        return cls(parse_time(one("from")), parse_time(one("to")), many("devices"), many("accounts"),
                   many("projects"), many("models"), many("entrypoints"), one("q")[:200])

    def session_key(self):
        return (self.devices, self.accounts, self.projects, self.entrypoints, self.q)

    def key(self):
        return (self.t0, self.t1, self.models) + self.session_key()

    def shifted(self, dt):
        return Filt(self.t0 + dt, self.t1 + dt, self.devices, self.accounts, self.projects, self.models,
                    self.entrypoints, self.q)

    def call_level(self):
        return self.t0 is not None or self.t1 is not None or bool(self.models)

    def active(self):
        return bool(self.devices or self.accounts or self.projects or self.models or self.entrypoints or self.q
                    or self.t0 is not None or self.t1 is not None)


def signature(conn):
    """Cheap fingerprint of everything the read model depends on."""
    q = conn.execute
    parts = [
        q("SELECT count(*),coalesce(max(received_at),'') FROM imports").fetchone(),
        q("SELECT count(*),coalesce(max(updated),'') FROM sessions").fetchone(),
        q("SELECT count(*),coalesce(max(fetched_ms),0) FROM util_snapshots").fetchone(),
        q("SELECT count(*),coalesce(max(id),0) FROM account_overrides").fetchone(),
        tuple(q("SELECT * FROM price_table ORDER BY pattern").fetchall()),
        tuple(q("SELECT * FROM account_capacity ORDER BY account_uuid").fetchall()),
        tuple(q("SELECT account_uuid,nickname,email FROM accounts ORDER BY account_uuid").fetchall()),
        tuple(q("SELECT device_id,label,last_seen,tz_offset_min FROM devices ORDER BY device_id").fetchall()),
        tuple(q("SELECT * FROM account_overrides ORDER BY id").fetchall()),
    ]
    return repr(parts)


class Store:
    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, conn):
        st = cls()
        t_start = time.time()
        st.sig = signature(conn)
        st.built_at = time.time()
        st.lock = threading.Lock()
        st._sel_cache = {}
        st._mask_cache = {}

        # devices / accounts ------------------------------------------------
        st.devices = []
        st.dev_index = {}
        for row in conn.execute("SELECT device_id,machine_id,label,hostname,os,os_release,tz_offset_min,agent_version,"
                                "python,first_seen,last_seen,last_file FROM devices ORDER BY first_seen,device_id"):
            d = dict(zip(("device_id", "machine_id", "label", "hostname", "os", "os_release", "tz_offset_min",
                          "agent_version", "python", "first_seen", "last_seen", "last_file"), row))
            d["name"] = d["label"] or d["hostname"] or d["device_id"][:8]
            st.dev_index[d["device_id"]] = len(st.devices)
            st.devices.append(d)
        st.accounts = []
        st.acct_index = {}
        for row in conn.execute("SELECT account_uuid,email,org_uuid,org_name,org_type,rate_limit_tier,billing_type,"
                                "display_name,org_role,nickname FROM accounts ORDER BY email,account_uuid"):
            a = dict(zip(("account_uuid", "email", "org_uuid", "org_name", "org_type", "rate_limit_tier",
                          "billing_type", "display_name", "org_role", "nickname"), row))
            a["name"] = a["nickname"] or a["email"] or a["account_uuid"][:8]
            a["plan"] = " / ".join(x for x in (a["org_type"], a["rate_limit_tier"]) if x)
            st.acct_index[a["account_uuid"]] = len(st.accounts)
            st.accounts.append(a)
        st.dev_cfgs = {}
        for dev, cfg, acct in conn.execute("SELECT device_id,cfg,account_uuid FROM device_cfgs"):
            st.dev_cfgs.setdefault(acct, []).append((dev, cfg))

        eff = ccm_calc.effective_accounts(conn)
        st.res = res = ccm_calc.compute(conn)
        st.prices = prices = ccm_calc.load_prices(conn)

        def acct_idx(uuid):
            uuid = uuid or "unknown"
            i = st.acct_index.get(uuid)
            if i is None:
                st.acct_index[uuid] = i = len(st.accounts)
                st.accounts.append({"account_uuid": uuid, "email": None, "nickname": None, "name": uuid[:8] if uuid != "unknown" else "unknown",
                                    "plan": "", "org_type": None, "rate_limit_tier": None, "org_name": None,
                                    "billing_type": None, "display_name": None, "org_role": None, "org_uuid": None})
            return i

        def dev_idx(did):
            i = st.dev_index.get(did)
            if i is None:
                st.dev_index[did] = i = len(st.devices)
                st.devices.append({"device_id": did, "name": (did or "?")[:8], "label": None, "hostname": None,
                                   "os": None, "os_release": None, "tz_offset_min": None, "agent_version": None,
                                   "python": None, "first_seen": None, "last_seen": None, "last_file": None,
                                   "machine_id": None, "python": None})
            return i

        # sessions ------------------------------------------------------------
        st.sessions = []
        st.sess_index = {}
        cols = ("device_id,sid,cfg,title,title_kind,project,cwd,branch,worktree,entrypoint,version,first_epoch,"
                "last_epoch,first_ts,last_ts,active_s,prompts,calls,web_requests,tools_json,mcp_json,subagents_json,"
                "tokens_json,ctx_peak,ctx_avg,compactions_json,cost_json,account_uuid,account_conf")
        names = cols.split(",")
        for row in conn.execute("SELECT %s FROM sessions" % cols):
            s = dict(zip(names, row))
            key = (s["device_id"], s["sid"])
            s["account"] = eff.get(key) or s["account_uuid"] or "unknown"
            s["overridden"] = bool(s["account_uuid"]) and s["account"] != s["account_uuid"]
            s["dev_i"] = dev_idx(s["device_id"])
            s["acct_i"] = acct_idx(s["account"])
            s["project_label"] = s["project"] or NONE_LABEL
            s["entry_label"] = s["entrypoint"] or NONE_LABEL
            cj = s["compactions_json"]
            s["n_compact"] = 0 if (not cj or cj == "[]") else len(jload(cj, []))
            sj = s["subagents_json"]
            s["n_sub"] = 0 if (not sj or sj == "{}") else sj.count('"calls"')
            s["_hay"] = " ".join(str(s[k] or "") for k in ("title", "project", "cwd", "branch")).lower()
            st.sess_index[key] = len(st.sessions)
            st.sessions.append(s)
        nsess = len(st.sessions)

        def placeholder(dev, sid):
            s = {k: None for k in names}
            s.update(device_id=dev, sid=sid, title="(conversation not exported)", project=None, entrypoint=None,
                     calls=0, active_s=0, prompts=0, ctx_peak=0, ctx_avg=0, account_conf=None, n_compact=0,
                     n_sub=0, tools_json=None, mcp_json=None, subagents_json=None, tokens_json=None,
                     compactions_json=None, cost_json=None)
            s["account"] = eff.get((dev, sid)) or "unknown"
            s["overridden"] = False
            s["dev_i"], s["acct_i"] = dev_idx(dev), acct_idx(s["account"])
            s["project_label"], s["entry_label"] = NONE_LABEL, NONE_LABEL
            s["_hay"] = ""
            st.sess_index[(dev, sid)] = len(st.sessions)
            st.sessions.append(s)
            return len(st.sessions) - 1

        # calls ---------------------------------------------------------------
        st.models = []
        mod_index = {}
        st.speeds, st.efforts = [], []
        sp_index, ef_index = {}, {}
        tool_cache = {}
        n_calls = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
        c_ts, c_usd, c_gap = array("d"), array("d"), array("f")
        c_s, c_model, c_speed, c_effort = array("i"), array("i"), array("i"), array("i")
        c_inp, c_out, c_cr, c_cw, c_think, c_ctx = (array("q") for _ in range(6))
        c_sub = bytearray()
        c_tools = []
        c_sub_id = []
        c_mid = []
        last_in_sess = {}
        by_session = {}
        a_ts, a_cum = {}, {}
        get_price = prices.get
        gap_cap = GAP_ACTIVE_S
        sess_index = st.sess_index
        sub_ids = {}
        for (dev, sid, ts, model, inp, out, cr, cw5, cw1, think, ctx, speed, effort, tools, sub) in conn.execute(
                "SELECT device_id,sid,ts_epoch,model,inp,out,cr,cw5,cw1,think,ctx,speed,effort,tools_json,sub "
                "FROM calls WHERE ts_epoch IS NOT NULL ORDER BY ts_epoch"):
            si = sess_index.get((dev, sid))
            if si is None:
                si = placeholder(dev, sid)
            inp, out, cr, cw5, cw1 = inp or 0, out or 0, cr or 0, cw5 or 0, cw1 or 0
            p = get_price(model)
            usd = (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[3] + cw1 * p[4]) / 1e6
            mi = mod_index.get(model)
            if mi is None:
                mod_index[model] = mi = len(st.models)
                st.models.append(model or "unknown")
            spi = sp_index.get(speed)
            if spi is None:
                sp_index[speed] = spi = len(st.speeds)
                st.speeds.append(speed or "(n/a)")
            efi = ef_index.get(effort)
            if efi is None:
                ef_index[effort] = efi = len(st.efforts)
                st.efforts.append(effort or "(n/a)")
            tl = tool_cache.get(tools)
            if tl is None:
                tl = tool_cache[tools] = tuple(jload(tools, []))
            prev = last_in_sess.get(si)
            gap = ts - prev if prev is not None and ts - prev < gap_cap else 0.0
            last_in_sess[si] = ts
            i = len(c_ts)
            c_ts.append(ts)
            c_usd.append(usd)
            c_gap.append(gap)
            c_s.append(si)
            c_model.append(mi)
            c_speed.append(spi)
            c_effort.append(efi)
            c_inp.append(inp)
            c_out.append(out)
            c_cr.append(cr)
            c_cw.append(cw5 + cw1)
            c_think.append(think or 0)
            c_ctx.append(ctx or 0)
            c_sub.append(1 if sub else 0)
            c_tools.append(tl)
            if sub:
                sid_i = sub_ids.get(sub)
                if sid_i is None:
                    sub_ids[sub] = sid_i = len(sub_ids)
                c_sub_id.append(sid_i)
            else:
                c_sub_id.append(-1)
            by_session.setdefault(si, []).append(i)
            ai = st.sessions[si]["acct_i"]
            lst = a_ts.get(ai)
            if lst is None:
                a_ts[ai], a_cum[ai] = [], []
                lst = a_ts[ai]
            lst.append(ts)
            a_cum[ai].append((a_cum[ai][-1] if a_cum[ai] else 0.0) + usd)
        st.sub_names = [None] * len(sub_ids)
        for k, v in sub_ids.items():
            st.sub_names[v] = k
        (st.c_ts, st.c_usd, st.c_gap, st.c_s, st.c_model, st.c_speed, st.c_effort, st.c_inp, st.c_out, st.c_cr,
         st.c_cw, st.c_think, st.c_ctx, st.c_sub, st.c_tools, st.c_sub_id) = (
            c_ts, c_usd, c_gap, c_s, c_model, c_speed, c_effort, c_inp, c_out, c_cr, c_cw, c_think, c_ctx, c_sub,
            c_tools, c_sub_id)
        st.by_session = by_session
        st.a_ts, st.a_cum = a_ts, a_cum
        st.n_calls = len(c_ts)
        nsess = len(st.sessions)
        # per-session totals over every call (unfiltered)
        st.s_usd = [0.0] * nsess
        st.s_tok = [0] * nsess
        st.s_calls = [0] * nsess
        for si, idxs in by_session.items():
            u = t = 0
            for i in idxs:
                u += c_usd[i]
                t += c_inp[i] + c_out[i] + c_cr[i] + c_cw[i]
            st.s_usd[si], st.s_tok[si], st.s_calls[si] = u, t, len(idxs)

        # estimated limit share per conversation (from ccm_calc) -------------------
        st.s_est = [None] * nsess
        for (dev, sid), r in res["sessions"].items():
            si = sess_index.get((dev, sid))
            if si is not None:
                st.s_est[si] = r

        # windows ---------------------------------------------------------------------
        st.win5 = st._windows(res["windows5"], acct_idx, 5 * 3600)
        st.win7 = st._windows(res["windows7"], acct_idx, 7 * 86400)

        # util snapshots with the estimate at the same moment ---------------------------
        st.snap_series = {}
        for acct, ss in res["snaps"].items():
            ai = acct_idx(acct)
            cap = res["caps"].get(acct) or {}
            c5, c7 = cap.get("cap_5h"), cap.get("cap_7d")
            ts_l, cum_l = a_ts.get(ai) or [], a_cum.get(ai) or []

            def cum_at(t, ts_l=ts_l, cum_l=cum_l):
                k = bisect.bisect_right(ts_l, t)
                return cum_l[k - 1] if k else 0.0
            series = []
            for s in ss:
                f = s["fetched_s"]
                w5 = ccm_calc._win(s, "five_hour")
                w7 = ccm_calc._win(s, "seven_day")
                e5 = e7 = None
                if w5 and c5:
                    e5 = 100.0 * (cum_at(min(f, w5[1])) - cum_at(w5[1] - ccm_calc.H5)) / c5
                if w7 and c7:
                    e7 = 100.0 * (cum_at(min(f, w7[1])) - cum_at(w7[1] - ccm_calc.W7)) / c7
                rows = (s.get("breakdown") or {}).get("rows") or {}
                series.append({"t": f, "r5": w5[0] if w5 else None, "r5_end": w5[1] if w5 else None,
                               "r7": w7[0] if w7 else None, "r7_end": w7[1] if w7 else None,
                               "e5": e5, "e7": e7, "cc": rows.get("claude_code")})
            st.snap_series[acct] = series

        # overrides / prices / imports (admin) ---------------------------------------------
        st.overrides = [dict(zip(("id", "account_uuid", "device_id", "project_like", "from_ts", "to_ts", "note"), r))
                        for r in conn.execute("SELECT id,account_uuid,device_id,project_like,from_ts,to_ts,note "
                                              "FROM account_overrides ORDER BY id DESC")]
        st.price_rows = [dict(zip(("pattern", "in_p", "out_p", "cr_p", "cw5_p", "cw1_p", "note"), r))
                         for r in conn.execute("SELECT pattern,in_p,out_p,cr_p,cw5_p,cw1_p,note FROM price_table "
                                               "ORDER BY pattern")]
        st.imports = [dict(zip(("filename", "received_at", "records", "device_id"), r))
                      for r in conn.execute("SELECT filename,received_at,records,device_id FROM imports "
                                            "ORDER BY received_at DESC LIMIT 500")]
        st.manual_caps = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT account_uuid,cap_5h,cap_7d,note FROM account_capacity")}
        st.t_min = c_ts[0] if len(c_ts) else None
        st.t_max = c_ts[-1] if len(c_ts) else None
        st.build_seconds = time.time() - t_start
        return st

    def _windows(self, meta, acct_idx, span):
        """Per-window usd, call count and top conversations. Keyed (account_idx, start)."""
        per_acct = {}
        for (acct, (a, b)), usd in meta.items():
            per_acct.setdefault(acct_idx(acct), []).append([a, b, usd, 0, {}])
        for ai in per_acct:
            per_acct[ai].sort(key=lambda w: w[0])
        starts = {ai: [w[0] for w in ws] for ai, ws in per_acct.items()}
        c_ts, c_s, c_usd = self.c_ts, self.c_s, self.c_usd
        sessions = self.sessions
        for i in range(len(c_ts)):
            ts = c_ts[i]
            si = c_s[i]
            ai = sessions[si]["acct_i"]
            ws = per_acct.get(ai)
            if not ws:
                continue
            k = bisect.bisect_right(starts[ai], ts) - 1
            if k < 0 or ts >= ws[k][1]:
                continue
            w = ws[k]
            w[3] += 1
            w[4][si] = w[4].get(si, 0.0) + c_usd[i]
        out = []
        for ai, ws in per_acct.items():
            for a, b, usd, n, sd in ws:
                top = sorted(sd.items(), key=lambda kv: -kv[1])[:5]
                out.append({"acct_i": ai, "start": a, "end": b, "usd": usd, "calls": n, "n_sess": len(sd),
                            "top": top, "devs": sorted({sessions[si]["dev_i"] for si in sd})})
        out.sort(key=lambda w: (w["start"], w["acct_i"]))
        return out

    # ------------------------------------------------------------------ labels
    def dev_name(self, i):
        return self.devices[i]["name"]

    def acct_name(self, i):
        return self.accounts[i]["name"]

    def dev_tz(self, i):
        tz = self.devices[i].get("tz_offset_min")
        return int(tz) if tz is not None else 0

    # ------------------------------------------------------------------ selection
    def session_mask(self, f):
        key = f.session_key()
        m = self._mask_cache.get(key)
        if m is not None:
            return m
        dev_ids, acct_ids = f.devices, f.accounts
        m = bytearray(len(self.sessions))
        q = f.q
        for i, s in enumerate(self.sessions):
            if dev_ids and s["device_id"] not in dev_ids:
                continue
            if acct_ids and s["account"] not in acct_ids:
                continue
            if f.projects and s["project_label"] not in f.projects:
                continue
            if f.entrypoints and s["entry_label"] not in f.entrypoints:
                continue
            if q and q not in s["_hay"]:
                continue
            m[i] = 1
        with self.lock:
            if len(self._mask_cache) > 16:
                self._mask_cache.clear()
            self._mask_cache[key] = m
        return m

    def select(self, f):
        """Indices of calls passing every filter (time order)."""
        key = f.key()
        hit = self._sel_cache.get(key)
        if hit is not None:
            return hit
        ok = self.session_mask(f)
        c_ts, c_s = self.c_ts, self.c_s
        lo = bisect.bisect_left(c_ts, f.t0) if f.t0 is not None else 0
        hi = bisect.bisect_left(c_ts, f.t1) if f.t1 is not None else len(c_ts)
        if f.models:
            mids = {i for i, m in enumerate(self.models) if m in f.models}
            c_m = self.c_model
            idx = [i for i in range(lo, hi) if ok[c_s[i]] and c_m[i] in mids]
        else:
            idx = [i for i in range(lo, hi) if ok[c_s[i]]]
        with self.lock:
            if len(self._sel_cache) > 12:
                self._sel_cache.clear()
            self._sel_cache[key] = idx
        return idx

    # ------------------------------------------------------------------ aggregates
    def totals(self, idx):
        c_usd, c_inp, c_out, c_cr, c_cw, c_think, c_gap, c_s = (
            self.c_usd, self.c_inp, self.c_out, self.c_cr, self.c_cw, self.c_think, self.c_gap, self.c_s)
        sess = self.sessions
        usd = inp = out = cr = cw = think = 0
        act = 0.0
        ss, ds, as_ = set(), set(), set()
        for i in idx:
            usd += c_usd[i]
            inp += c_inp[i]
            out += c_out[i]
            cr += c_cr[i]
            cw += c_cw[i]
            think += c_think[i]
            act += c_gap[i]
            ss.add(c_s[i])
        for si in ss:
            ds.add(sess[si]["dev_i"])
            as_.add(sess[si]["acct_i"])
        return {"calls": len(idx), "usd": usd, "inp": inp, "out": out, "cr": cr, "cw": cw, "think": think,
                "tokens": inp + out + cr + cw, "active_s": act, "conversations": len(ss), "devices": len(ds),
                "accounts": len(as_)}

    def cube(self, idx):
        """Hourly cube: rows [hour, dev, acct, model, calls, usd, inp, out, cr, cw, think, active_s]."""
        sess = self.sessions
        c_ts, c_s, c_model = self.c_ts, self.c_s, self.c_model
        c_usd, c_inp, c_out, c_cr, c_cw, c_think, c_gap = (
            self.c_usd, self.c_inp, self.c_out, self.c_cr, self.c_cw, self.c_think, self.c_gap)
        cells = {}
        for i in idx:
            si = c_s[i]
            s = sess[si]
            key = (int(c_ts[i] // 3600), s["dev_i"], s["acct_i"], c_model[i])
            a = cells.get(key)
            if a is None:
                a = cells[key] = [0, 0.0, 0, 0, 0, 0, 0, 0.0]
            a[0] += 1
            a[1] += c_usd[i]
            a[2] += c_inp[i]
            a[3] += c_out[i]
            a[4] += c_cr[i]
            a[5] += c_cw[i]
            a[6] += c_think[i]
            a[7] += c_gap[i]
        rows = []
        for k in sorted(cells):
            a = cells[k]
            rows.append([k[0], k[1], k[2], k[3], a[0], round(a[1], 5), a[2], a[3], a[4], a[5], a[6], int(a[7])])
        return rows

    def per_session(self, idx):
        """sidx -> [calls, usd, tokens, out, first_ts, last_ts] over the selected calls."""
        c_ts, c_s, c_usd, c_inp, c_out, c_cr, c_cw = (self.c_ts, self.c_s, self.c_usd, self.c_inp, self.c_out,
                                                       self.c_cr, self.c_cw)
        agg = {}
        for i in idx:
            si = c_s[i]
            a = agg.get(si)
            if a is None:
                a = agg[si] = [0, 0.0, 0, 0, c_ts[i], c_ts[i]]
            a[0] += 1
            a[1] += c_usd[i]
            a[2] += c_inp[i] + c_out[i] + c_cr[i] + c_cw[i]
            a[3] += c_out[i]
            a[5] = c_ts[i]
        return agg

    def session_lookup(self, device_id, sid):
        return self.sess_index.get((device_id, sid))


class Holder:
    """Owns the current Store; rebuilds when the database fingerprint changes (checked at most every 2s)
    or when the cache is older than MAX_AGE seconds and the fingerprint differs."""

    CHECK_EVERY = 2.0

    def __init__(self, open_ro):
        self.open_ro = open_ro
        self.store = None
        self.checked = 0.0
        self.lock = threading.Lock()
        self.forced = True

    def invalidate(self):
        self.forced = True

    def get(self):
        now = time.time()
        if self.store is not None and not self.forced and now - self.checked < self.CHECK_EVERY:
            return self.store
        with self.lock:
            now = time.time()
            if self.store is not None and not self.forced and now - self.checked < self.CHECK_EVERY:
                return self.store
            conn = self.open_ro()
            try:
                sig = signature(conn)
                if self.store is None or self.forced or sig != self.store.sig:
                    self.store = Store.build(conn)
                self.forced = False
                self.checked = time.time()
            finally:
                conn.close()
            return self.store


def r2(x, n=2):
    return None if x is None else round(x, n)
