"""Spike / anomaly detection for the CCM dashboard. Python 3.8+, stdlib only.

Detectors (all report the same record shape, see `_mk`):
  daily_spend     robust z-score of daily $ per account and per device
  conv_cost       per-conversation $ and tokens vs the same project/device baseline (log scale)
  window_spend    5h-window $ vs the same account's preceding windows
  call_burst      hourly API-call counts per device
  off_hours       activity in hours of the day a device is rarely active (device's own timezone)
  ctx_runaway     context size at least doubling within a few calls
  cache_miss      calls that re-write a large context instead of reading it from cache
  tool_loop       the same tool called many times in a row within a conversation

Robust z-score = (x - median) / (1.4826 * MAD), with a floor on the scale and a minimum-sample guard.
Severity = 50 + 9 * log2(score / threshold), clamped to 50..100 (100 = about 47x the threshold), so every reported item is >= 50.
Detection runs on the whole database (so baselines are stable); the dashboard filters the result afterwards.
"""
import datetime
import json
import math
from statistics import median

KINDS = {
    "daily_spend": "Daily spend spike",
    "conv_cost": "Expensive conversation",
    "window_spend": "Heavy 5h window",
    "call_burst": "Call-rate burst",
    "off_hours": "Unusual hours",
    "ctx_runaway": "Context runaway",
    "cache_miss": "Cache misses",
    "tool_loop": "Tool loop",
}
_EPOCH = datetime.datetime(1970, 1, 1)
MIN_SAMPLES = 7
PER_KIND_CAP = 40


def params(sens):
    """Map the 1..10 sensitivity slider (10 = flag more) to thresholds."""
    s = min(10.0, max(1.0, float(sens)))
    g = 2 ** ((5 - s) / 3.0)          # absolute-size guard factor: 2.5 (strict) .. 0.31 (sensitive)
    return {
        "sens": s,
        "z": 6.0 - 0.4 * s,           # robust z threshold: 5.6 .. 2.0
        "zlog": 4.0 - 0.25 * s,       # threshold on log-scale z (conversation cost): 3.75 .. 1.5
        "zspike": 1.5 * (6.0 - 0.4 * s),  # raw robust z for daily $, 5h windows, hourly bursts: 8.4 .. 3.0
        "ctx_ratio": 2.4 - 0.08 * s,  # 2.32 .. 1.6
        "run": int(round(150 - 12 * s)),  # identical consecutive tool calls: 138 .. 30
        "g": g,
        "min_day_usd": 10.0 * g,
        "min_conv_usd": 3.0 * g,
        "min_win_usd": 20.0 * g,
        "min_burst_calls": 150.0 * g,
        "min_off_calls": 40.0 * g,
        "rare_share": 0.01 * s / 5.0,
        "min_miss_usd": 2.0 * g,
        "min_ctx": 100000,
    }


def robust_z(xs):
    """Return (median, scale) with a sane floor so near-constant series do not explode."""
    med = median(xs)
    mad = median([abs(x - med) for x in xs])
    scale = 1.4826 * mad
    if scale < 1e-9:
        meanad = sum(abs(x - med) for x in xs) / len(xs)
        scale = 1.2533 * meanad
    scale = max(scale, 0.15 * abs(med), 1e-6)
    return med, scale


def log_z_stats(xs):
    """(median of ln x, scale) for heavy-tailed positive series; scale floored so tight series do not explode."""
    lx = [math.log(max(x, 1e-9)) for x in xs]
    med = median(lx)
    mad = median([abs(x - med) for x in lx])
    return med, max(1.4826 * mad, 0.35)


def sev(score, thr):
    if thr <= 0 or score < thr:
        return 0
    return int(round(min(100, 50 + 9 * math.log2(score / thr))))


def _mk(kind, severity, reason, ts, **kw):
    a = {"kind": kind, "label": KINDS[kind], "severity": severity, "reason": reason, "ts": ts,
         "ts_end": kw.pop("ts_end", ts), "device_id": None, "account": None, "sid": None, "title": None,
         "project": None, "value": None, "baseline": None}
    a.update(kw)
    return a


def _day(ts, tz_min):
    return (_EPOCH + datetime.timedelta(seconds=ts + tz_min * 60)).strftime("%Y-%m-%d")


def _money(x):
    return "$%.2f" % x if x < 100 else "$%d" % round(x)


def detect(store, sens=5.0, tz_min=0):
    p = params(sens)
    out = []
    for fn in (_daily_spend, _conv_cost, _window_spend, _call_burst, _off_hours, _ctx_runaway, _cache_miss,
               _tool_loop):
        items = fn(store, p, tz_min)
        items.sort(key=lambda a: -a["severity"])
        out.extend(items[:PER_KIND_CAP])
    for n, a in enumerate(sorted(out, key=lambda a: (-a["severity"], a["ts"] or 0))):
        a["id"] = "%s-%d" % (a["kind"], n)
    out.sort(key=lambda a: (-a["severity"], -(a["ts"] or 0)))
    return out


# --------------------------------------------------------------------------- (a) daily spend

def _daily_spend(st, p, tz_min):
    items = []
    c_ts, c_usd, c_s = st.c_ts, st.c_usd, st.c_s
    sess = st.sessions
    per = {}   # ("acct"|"dev", index) -> day -> usd
    first_ts = {}
    day_cache = {}
    for i in range(len(c_ts)):
        h = int(c_ts[i] // 3600)
        day = day_cache.get(h)
        if day is None:
            day = day_cache[h] = _day(h * 3600, tz_min)
        s = sess[c_s[i]]
        for key in (("acct", s["acct_i"]), ("dev", s["dev_i"])):
            dd = per.setdefault(key, {})
            dd[day] = dd.get(day, 0.0) + c_usd[i]
            first_ts.setdefault((key, day), c_ts[i])
    for key, days in per.items():
        if len(days) < MIN_SAMPLES:
            continue
        vals = [v for v in days.values() if v > 0]
        if len(vals) < MIN_SAMPLES:
            continue
        med, scale = robust_z(vals)
        for day, v in days.items():
            if v < p["min_day_usd"] or v < 3 * med:
                continue
            z = (v - med) / scale
            sv = sev(z, p["zspike"])
            if not sv:
                continue
            kind, idx = key
            who = st.acct_name(idx) if kind == "acct" else st.dev_name(idx)
            t0 = first_ts[(key, day)]
            a = _mk("daily_spend", sv,
                    "%s spent %s on %s, %.1fx its typical day (%s median, z=%.1f)" % (
                        who, _money(v), day, v / max(med, 1e-9), _money(med), z),
                    t0, ts_end=t0 + 86400, value=v, baseline=med, day=day, scope=kind)
            if kind == "acct":
                a["account"] = st.accounts[idx]["account_uuid"]
            else:
                a["device_id"] = st.devices[idx]["device_id"]
            items.append(a)
    return items


# --------------------------------------------------------------------------- (b) conversations

def _conv_cost(st, p, tz_min):
    items = []
    groups = {}
    for si, s in enumerate(st.sessions):
        if st.s_calls[si] < 3:
            continue
        groups.setdefault(("pd", s["project_label"], s["device_id"]), []).append(si)
        groups.setdefault(("p", s["project_label"]), []).append(si)
        groups.setdefault(("d", s["device_id"]), []).append(si)
    stats = {}
    for gk, members in groups.items():
        if len(members) >= 8:
            lu = [math.log1p(st.s_usd[m] * 100) for m in members]
            lt = [math.log1p(st.s_tok[m] / 1000.0) for m in members]
            stats[gk] = (median([st.s_usd[m] for m in members]), robust_z(lu), robust_z(lt), len(members))
    best = {}
    for si, s in enumerate(st.sessions):
        if st.s_calls[si] < 3:
            continue
        for gk in (("pd", s["project_label"], s["device_id"]), ("p", s["project_label"]), ("d", s["device_id"])):
            if gk in stats:
                best[si] = gk
                break
    for si, gk in best.items():
        s = st.sessions[si]
        usd, tok = st.s_usd[si], st.s_tok[si]
        med_usd, (mu, su), (mt, sk), n = stats[gk]
        if usd < p["min_conv_usd"]:
            continue
        zu = (math.log1p(usd * 100) - mu) / su
        zt = (math.log1p(tok / 1000.0) - mt) / sk
        z = max(zu, zt)
        sv = sev(z, p["zlog"])
        if not sv or usd < 2.5 * med_usd:
            continue
        where = {"pd": "same project and device", "p": "same project", "d": "same device"}[gk[0]]
        items.append(_mk(
            "conv_cost", sv,
            "%s cost %s and %s tokens, %.1fx the median conversation in the %s (%s, n=%d, z=%.1f)" % (
                (s["title"] or s["sid"])[:60], _money(usd), _short(tok), usd / max(med_usd, 1e-9), where,
                _money(med_usd), n, z),
            s["first_epoch"] or (st.c_ts[st.by_session[si][0]] if si in st.by_session else 0),
            ts_end=s["last_epoch"], device_id=s["device_id"], account=s["account"], sid=s["sid"], title=s["title"],
            project=s["project"], value=usd, baseline=med_usd))
    return items


def _short(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return "%.1f%s" % (n / div, suf)
    return str(int(n))


# --------------------------------------------------------------------------- (c) 5h windows

def _window_spend(st, p, tz_min):
    items = []
    per = {}
    for w in st.win5:
        per.setdefault(w["acct_i"], []).append(w)
    for ai, ws in per.items():
        ws.sort(key=lambda w: w["start"])
        cap = (st.res["caps"].get(st.accounts[ai]["account_uuid"]) or {}).get("cap_5h")
        for k, w in enumerate(ws):
            prev = [x["usd"] for x in ws[max(0, k - 30):k]]
            if len(prev) < MIN_SAMPLES:
                # not enough history before this window: use all other windows of the account instead
                prev = [x["usd"] for j, x in enumerate(ws) if j != k]
                if len(prev) < MIN_SAMPLES:
                    continue
            med, scale = robust_z(prev)
            v = w["usd"]
            if v < p["min_win_usd"] or v < 2.5 * med:
                continue
            z = (v - med) / scale
            sv = sev(z, p["zspike"])
            if not sv:
                continue
            pct = " (est. %d%% of the 5h limit)" % round(100 * v / cap) if cap else ""
            top = w["top"][0] if w["top"] else None
            a = _mk("window_spend", sv,
                    "%s used %s in one 5h window, %.1fx its recent windows (%s median, z=%.1f)%s" % (
                        st.acct_name(ai), _money(v), v / max(med, 1e-9), _money(med), z, pct),
                    w["start"], ts_end=w["end"], account=st.accounts[ai]["account_uuid"], value=v, baseline=med)
            if top:
                s = st.sessions[top[0]]
                a.update(device_id=s["device_id"], sid=s["sid"], title=s["title"], project=s["project"])
            items.append(a)
    return items


# --------------------------------------------------------------------------- (d) call bursts

def _call_burst(st, p, tz_min):
    items = []
    per = {}
    c_ts, c_s = st.c_ts, st.c_s
    for i in range(len(c_ts)):
        dev = st.sessions[c_s[i]]["dev_i"]
        h = int(c_ts[i] // 3600)
        d = per.setdefault(dev, {})
        d[h] = d.get(h, 0) + 1
    for dev, hours in per.items():
        if len(hours) < 24:
            continue
        med, scale = robust_z(list(hours.values()))
        best_per_day = {}
        for h, n in hours.items():
            if n < p["min_burst_calls"] or n < 4 * med:
                continue
            z = (n - med) / scale
            sv = sev(z, p["zspike"])
            if not sv:
                continue
            day = _day(h * 3600, tz_min)
            if day not in best_per_day or n > best_per_day[day][0]:
                best_per_day[day] = (n, h, z, sv)
        for day, (n, h, z, sv) in best_per_day.items():
            items.append(_mk("call_burst", sv,
                             "%s made %d API calls in one hour (%s), %.1fx its typical active hour (%d, z=%.1f)" % (
                                 st.dev_name(dev), n, _fmt_hour(h * 3600, st.dev_tz(dev)), n / max(med, 1), med, z),
                             h * 3600, ts_end=h * 3600 + 3600, device_id=st.devices[dev]["device_id"],
                             value=n, baseline=med))
    return items


def _fmt_hour(ts, tz_min):
    return (_EPOCH + datetime.timedelta(seconds=ts + tz_min * 60)).strftime("%Y-%m-%d %H:00 local")


# --------------------------------------------------------------------------- (e) unusual hours

def _off_hours(st, p, tz_min):
    """Hours of the day (device-local) that carry only a sliver of the device's calls on its other days."""
    items = []
    per = {}
    c_ts, c_s = st.c_ts, st.c_s
    for i in range(len(c_ts)):
        dev = st.sessions[c_s[i]]["dev_i"]
        h = int(c_ts[i] // 3600)
        d = per.setdefault(dev, {})
        d[h] = d.get(h, 0) + 1
    for dev, hours in per.items():
        total = sum(hours.values())
        tz = st.dev_tz(dev)
        loc = {}      # utc hour -> (local date, local hour)
        calls_by_hr = [0] * 24
        calls_by_day_hr = {}
        all_days = set()
        for h, n in hours.items():
            day, hr = _day(h * 3600, tz), int(((h * 3600 + tz * 60) // 3600) % 24)
            loc[h] = (day, hr)
            all_days.add(day)
            calls_by_hr[hr] += n
            calls_by_day_hr[(day, hr)] = n
        if total < 200 or len(all_days) < 5:
            continue
        cand = []
        for h, n in sorted(hours.items()):
            day, hr = loc[h]
            if n < p["min_off_calls"]:
                continue
            others = total - n
            share = (calls_by_hr[hr] - calls_by_day_hr[(day, hr)]) / float(max(others, 1))   # candidate day left out
            if share < p["rare_share"]:
                cand.append((h, hr, n, share))
        k = 0
        while k < len(cand):       # merge consecutive hours into one burst
            j = k
            while j + 1 < len(cand) and cand[j + 1][0] - cand[j][0] <= 1:
                j += 1
            grp = cand[k:j + 1]
            calls = sum(g[2] for g in grp)
            share = min(g[3] for g in grp)
            intensity = math.sqrt(calls / p["min_off_calls"]) * math.sqrt(p["rare_share"] / max(share, 0.002))
            sv = sev(intensity, 1.0)
            if sv:
                h0, h1 = grp[0][0], grp[-1][0]
                items.append(_mk(
                    "off_hours", sv,
                    "%s made %d calls around %02d:00 local on %s; on its other days only %.1f%% of its calls fall in that hour" % (
                        st.dev_name(dev), calls, grp[0][1], loc[h0][0], 100 * share),
                    h0 * 3600, ts_end=h1 * 3600 + 3600, device_id=st.devices[dev]["device_id"], value=calls,
                    baseline=share))
            k = j + 1
    return items


# --------------------------------------------------------------------------- (f) context runaways

def _ctx_runaway(st, p, tz_min):
    items = []
    c_ctx, c_ts, c_sub = st.c_ctx, st.c_ts, st.c_sub
    for si, idxs in st.by_session.items():
        main = [i for i in idxs if not c_sub[i]]
        if len(main) < 4:
            continue
        best = None
        for k in range(1, len(main)):
            cur = c_ctx[main[k]]
            if cur < p["min_ctx"]:
                continue
            base = min(c_ctx[main[j]] for j in range(max(0, k - 4), k))
            if base <= 0:
                base = 1
            ratio = cur / base
            if ratio >= p["ctx_ratio"] and cur - base >= 60000:
                if best is None or ratio > best[0]:
                    best = (ratio, k, base, cur)
        if not best:
            continue
        ratio, k, base, cur = best
        sv = sev(ratio, p["ctx_ratio"])
        s = st.sessions[si]
        items.append(_mk(
            "ctx_runaway", sv,
            "context grew %s -> %s (%.1fx) within %d calls in \"%s\"" % (
                _short(base), _short(cur), ratio, min(4, k), (s["title"] or s["sid"])[:60]),
            c_ts[main[k]], device_id=s["device_id"], account=s["account"], sid=s["sid"], title=s["title"],
            project=s["project"], value=cur, baseline=base))
    return items


# --------------------------------------------------------------------------- (g) cache misses / tool loops

def _cache_miss(st, p, tz_min):
    items = []
    c_cr, c_cw, c_inp, c_ctx, c_usd, c_ts = st.c_cr, st.c_cw, st.c_inp, st.c_ctx, st.c_usd, st.c_ts
    c_sub, c_gap = st.c_sub, st.c_gap
    for si, idxs in st.by_session.items():
        if len(idxs) < 6:
            continue
        # a miss on the main thread while the previous call was < 4 min ago (the cache TTL is 5 min)
        bad = [i for i in idxs[2:] if c_cr[i] == 0 and c_ctx[i] >= 50000 and not c_sub[i] and 0 < c_gap[i] < 240]
        if len(bad) < 3:
            continue
        extra = sum(c_usd[i] for i in bad)
        if extra < p["min_miss_usd"]:
            continue
        cr = sum(c_cr[i] for i in idxs)
        tot = sum(c_inp[i] + c_cr[i] + c_cw[i] for i in idxs)
        hit = cr / tot if tot else 0.0
        intensity = (extra / p["min_miss_usd"]) * (len(bad) / 3.0) ** 0.5
        sv = sev(intensity, 1.0)
        if not sv:
            continue
        s = st.sessions[si]
        items.append(_mk(
            "cache_miss", sv,
            "%d calls re-sent a context over 50k tokens with no cache read (%s), cache hit ratio %d%% in \"%s\"" % (
                len(bad), _money(extra), round(100 * hit), (s["title"] or s["sid"])[:60]),
            c_ts[bad[0]], ts_end=c_ts[bad[-1]], device_id=s["device_id"], account=s["account"], sid=s["sid"],
            title=s["title"], project=s["project"], value=extra, baseline=hit))
    return items


def _tool_loop(st, p, tz_min):
    items = []
    c_tools, c_ts, c_sub = st.c_tools, st.c_ts, st.c_sub
    thr = max(5, p["run"])
    for si, idxs in st.by_session.items():
        if len(idxs) < thr:
            continue
        best = (0, None, 0)
        run, cur, start = 0, None, 0
        for i in idxs:
            if c_sub[i]:
                continue
            t = c_tools[i]
            key = tuple(sorted(t)) if t else None
            if key is not None and key == cur:
                run += 1
            else:
                cur, run, start = key, (1 if key is not None else 0), i
            if run > best[0]:
                best = (run, cur, start)
        if best[0] < thr:
            continue
        s = st.sessions[si]
        tools = jload_counts(s.get("tools_json"))
        name = "+".join(best[1])
        total = sum(tools.get(t, 0) for t in best[1]) if tools else None
        items.append(_mk(
            "tool_loop", sev(best[0], thr),
            "%s called %d times in a row%s in \"%s\"" % (
                name, best[0], (" (%d in the whole conversation)" % total) if total else "",
                (s["title"] or s["sid"])[:60]),
            c_ts[best[2]], device_id=s["device_id"], account=s["account"], sid=s["sid"], title=s["title"],
            project=s["project"], value=best[0], baseline=thr))
    return items


def jload_counts(raw):
    try:
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}
