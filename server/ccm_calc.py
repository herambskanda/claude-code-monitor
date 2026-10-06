"""CCM calculations on top of ccm.db: API-equivalent cost per call, 5h/weekly windows,
per-account capacity calibration, per-conversation share of the 5h and weekly limits.

Percentages are ESTIMATES: Anthropic does not publish limits. Capacity is calibrated from the
usage snapshots Claude Code caches (the real utilization %), see `calibrate`.
"""
import bisect
import calendar
import json
import re
import time

H5 = 5 * 3600
W7 = 7 * 24 * 3600

# $/MTok: (input, output, cache read, cache write 5m, cache write 1h). Longest prefix wins, then '*family'.
# Seeded by least-squares fits against Claude Code's own cost-state totals; editable in the price_table.
DEFAULT_PRICES = [
    ("claude-opus-5-5", 4.0, 20.0, 0.2, 5.0, 8.0, "fit vs cost-state"),
    ("claude-opus-5", 5.0, 25.0, 0.5, 6.25, 10.0, "fit vs cost-state"),
    ("claude-sonnet-5", 2.0, 10.0, 0.2, 2.5, 4.0, "fit vs cost-state (sonnet-5 and 5-5)"),
    ("claude-haiku-4-5", 1.0, 5.0, 0.1, 1.25, 2.0, "fit vs cost-state"),
    ("claude-fable-5-1", 10.0, 50.0, 1.0, 12.5, 20.0, "estimate (few samples)"),
    ("*opus", 5.0, 25.0, 0.5, 6.25, 10.0, "family fallback"),
    ("*sonnet", 3.0, 15.0, 0.3, 3.75, 6.0, "family fallback"),
    ("*haiku", 1.0, 5.0, 0.1, 1.25, 2.0, "family fallback"),
    ("*fable", 10.0, 50.0, 1.0, 12.5, 20.0, "family fallback"),
    ("*", 3.0, 15.0, 0.3, 3.75, 6.0, "unknown model fallback"),
]

_TS = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?")


def ts_epoch(s):
    m = _TS.match(s or "")
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    frac = float("0." + m.group(7)) if m.group(7) else 0.0
    return calendar.timegm((y, mo, d, h, mi, se)) + frac


class Prices:
    def __init__(self, rows):
        self.rows = {r[0]: tuple(r[1:6]) for r in rows}
        self.prefixes = sorted((k for k in self.rows if not k.startswith("*")), key=len, reverse=True)
        self.cache = {}

    def get(self, model):
        p = self.cache.get(model)
        if p is None:
            m = re.sub(r"\[.*?\]", "", model or "")
            for k in self.prefixes:
                if m.startswith(k):
                    p = self.rows[k]
                    break
            else:
                for k in self.rows:
                    if k.startswith("*") and k != "*" and k[1:] in m:
                        p = self.rows[k]
                        break
                else:
                    p = self.rows.get("*", (3.0, 15.0, 0.3, 3.75, 6.0))
            self.cache[model] = p
        return p

    def usd(self, model, inp, out, cr, cw5, cw1):
        p = self.get(model)
        return (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[3] + cw1 * p[4]) / 1e6


def load_prices(conn):
    return Prices(conn.execute("SELECT pattern,in_p,out_p,cr_p,cw5_p,cw1_p FROM price_table").fetchall())


def effective_accounts(conn):
    """Map (device_id, sid) -> effective account uuid, applying account_overrides (newest rule wins)."""
    sess = conn.execute("SELECT device_id,sid,account_uuid,project,cwd,first_ts,last_ts FROM sessions").fetchall()
    rules = conn.execute("SELECT account_uuid,device_id,project_like,from_ts,to_ts FROM account_overrides "
                         "ORDER BY id DESC").fetchall()
    out = {}
    for dev, sid, acct, project, cwd, f_ts, l_ts in sess:
        eff = acct
        for r_acct, r_dev, r_proj, r_from, r_to in rules:
            if r_dev and r_dev != dev:
                continue
            if r_proj and r_proj.lower() not in ((project or "") + " " + (cwd or "")).lower():
                continue
            if r_from and (l_ts or "") < r_from:
                continue
            if r_to and (f_ts or "") > r_to:
                continue
            eff = r_acct
            break
        out[(dev, sid)] = eff
    return out


def load_calls(conn):
    """List of (epoch, usd, account, device, sid) sorted by time."""
    prices = load_prices(conn)
    acct = effective_accounts(conn)
    rows = []
    for dev, sid, ts, model, inp, out, cr, cw5, cw1 in conn.execute(
            "SELECT device_id,sid,ts_epoch,model,inp,out,cr,cw5,cw1 FROM calls"):
        rows.append((ts, prices.usd(model, inp or 0, out or 0, cr or 0, cw5 or 0, cw1 or 0),
                     acct.get((dev, sid)) or "unknown", dev, sid))
    rows.sort()
    return rows


def load_snapshots(conn):
    snaps = {}
    for fetched_ms, acct, raw in conn.execute("SELECT fetched_ms,account_uuid,raw_json FROM util_snapshots "
                                              "ORDER BY fetched_ms"):
        d = json.loads(raw)
        d["fetched_s"] = fetched_ms / 1000.0
        snaps.setdefault(acct or "unknown", []).append(d)
    return snaps


def _win(snap, key):
    w = snap.get(key)
    if not isinstance(w, dict) or w.get("utilization") is None:
        return None
    end = ts_epoch(w.get("resets_at"))
    return (float(w["utilization"]), end) if end else None


def weekly_bucket(ts, anchor_end):
    """Fixed 7-day periods ending at anchor_end (+/- k weeks). Returns (start, end)."""
    k = (ts - anchor_end) // W7  # floor
    end = anchor_end + (k + 1) * W7
    return end - W7, end


def assign_windows(calls_by_acct, snaps):
    """Return per-call window ids: {(account): [(epoch, usd, dev, sid, w5id, w7id)]} and windows meta."""
    result, meta5, meta7 = {}, {}, {}
    for acct, calls in calls_by_acct.items():
        anchored = []
        week_anchor = None
        for s in snaps.get(acct, []):
            w5 = _win(s, "five_hour")
            if w5:
                anchored.append((w5[1] - H5, w5[1]))
            w7 = _win(s, "seven_day")
            if w7:
                week_anchor = w7[1]
        anchored = sorted(set((round(a), round(b)) for a, b in anchored))
        gstart = None
        out = []
        starts = [a for a, _ in anchored]
        for ts, usd, dev, sid in calls:
            w5 = None
            j = bisect.bisect_right(starts, ts) - 1   # first anchored window containing ts (windows are 5h long)
            while j >= 0 and starts[j] > ts - 2 * H5:
                if anchored[j][1] > ts:
                    w5 = anchored[j]
                j -= 1
            if w5 is None:
                if gstart is None or ts >= gstart + H5:
                    gstart = ts
                w5 = (gstart, gstart + H5)
            w7 = weekly_bucket(ts, week_anchor) if week_anchor is not None else None
            out.append((ts, usd, dev, sid, w5, w7))
            meta5[(acct, w5)] = meta5.get((acct, w5), 0.0) + usd
            if w7:
                meta7[(acct, w7)] = meta7.get((acct, w7), 0.0) + usd
        result[acct] = out
    return result, meta5, meta7


def calibrate(calls_by_acct, snaps, min_util=5.0):
    """capacity (API-equiv $) per account for the 5h and weekly limits, as medians of snapshot samples.
    Weekly samples use the Claude Code share of the weekly meter when the snapshot has a breakdown."""
    caps = {}
    for acct, ss in snaps.items():
        calls = calls_by_acct.get(acct, [])
        ts_l = [c[0] for c in calls]          # calls are time-sorted: prefix sums make each window sum O(log n)
        cum = [0.0]
        for c in calls:
            cum.append(cum[-1] + c[1])

        def usd_between(a, b, ts_l=ts_l, cum=cum):
            return cum[bisect.bisect_right(ts_l, b)] - cum[bisect.bisect_left(ts_l, a)]
        s5, s7 = [], []
        for s in ss:
            f = s["fetched_s"]
            w5 = _win(s, "five_hour")
            if w5 and w5[0] >= min_util and f <= w5[1] + 60:
                a, b = w5[1] - H5, w5[1]
                usd = usd_between(a, min(b, f))
                if usd > 0:
                    s5.append(usd / (w5[0] / 100.0))
            w7 = _win(s, "seven_day")
            if w7 and w7[0] >= min_util and f <= w7[1] + 60:
                a, b = w7[1] - W7, w7[1]
                share = 1.0
                rows = (s.get("breakdown") or {}).get("rows") or {}
                if rows.get("claude_code") is not None:
                    share = max(float(rows["claude_code"]), 1.0) / 100.0
                usd = usd_between(a, min(b, f))
                if usd > 0:
                    s7.append(usd / ((w7[0] / 100.0) * share))
        caps[acct] = {
            "cap_5h": _high_pct(s5), "n_5h": len(s5), "cap_7d": _high_pct(s7), "n_7d": len(s7),
            "samples_5h": s5, "samples_7d": s7, "source": "calibrated",
        }
    return caps


def _high_pct(xs, q=0.9):
    """Upper-tail estimate. Every error source (usage on devices/surfaces we haven't ingested, integer
    rounding of the snapshot %) makes a sample LOWER than the true capacity, so the median is biased low."""
    if not xs:
        return None
    xs = sorted(xs)
    i = q * (len(xs) - 1)
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def compute(conn):
    """Everything the dashboard/report needs. Returns dict with accounts, windows, sessions."""
    calls = load_calls(conn)
    by_acct = {}
    for ts, usd, acct, dev, sid in calls:
        if ts is None:
            continue
        by_acct.setdefault(acct, []).append((ts, usd, dev, sid))
    snaps = load_snapshots(conn)
    caps = calibrate(by_acct, snaps)
    for acct, o5, o7 in conn.execute("SELECT account_uuid,cap_5h,cap_7d FROM account_capacity"):
        c = caps.setdefault(acct, {"cap_5h": None, "n_5h": 0, "cap_7d": None, "n_7d": 0, "source": "manual"})
        if o5:
            c["cap_5h"], c["source"] = o5, "manual"
        if o7:
            c["cap_7d"], c["source"] = o7, "manual"
    assigned, w5, w7 = assign_windows(by_acct, snaps)
    sessions = {}
    for acct, rows in assigned.items():
        cap = caps.get(acct) or {}
        c5, c7 = cap.get("cap_5h"), cap.get("cap_7d")
        for ts, usd, dev, sid, a5, a7 in rows:
            s = sessions.setdefault((dev, sid), {"device_id": dev, "sid": sid, "account": acct, "usd": 0.0,
                                                 "w5": {}, "w7": {}})
            s["usd"] += usd
            s["w5"][a5] = s["w5"].get(a5, 0.0) + usd
            if a7:
                s["w7"][a7] = s["w7"].get(a7, 0.0) + usd
    for (dev, sid), s in sessions.items():
        cap = caps.get(s["account"]) or {}
        c5, c7 = cap.get("cap_5h"), cap.get("cap_7d")
        s["pct5_peak"] = 100.0 * max(s["w5"].values()) / c5 if c5 and s["w5"] else None
        s["pct5_sum"] = 100.0 * sum(s["w5"].values()) / c5 if c5 else None
        s["pct7_sum"] = 100.0 * sum(s["w7"].values()) / c7 if c7 and s["w7"] else None
    return {"caps": caps, "snaps": snaps, "windows5": w5, "windows7": w7, "sessions": sessions,
            "calls": len(calls), "total_usd": sum(c[1] for c in calls), "computed_at": time.time()}
