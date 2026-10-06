"""Dashboard tests: API, filters, anomaly detection on injected spikes, write endpoints, import endpoint, drop folder,
GitHub request/pull endpoints (fake GitHub), XSS escaping, empty/single-device databases.
Run with the rest: python3 -m unittest discover -s tests"""
import gzip
import http.client
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

os.environ["CCM_NO_SLEEP"] = "1"
os.environ["CCM_DASH_QUIET"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccm_agent  # noqa: E402
import ccm_anomaly  # noqa: E402
import ccm_calc  # noqa: E402
import ccm_dashboard  # noqa: E402
import ccm_gh  # noqa: E402
import ccm_server  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
from test_ccm import AgentBase, assistant, claude_json, user, write_jsonl  # noqa: E402

BASE = ccm_calc.ts_epoch("2026-09-01T00:00:00Z")
DAYS = 25
ACCT_A = "aaaaaaaa-0000-4000-8000-00000000000a"
ACCT_B = "bbbbbbbb-0000-4000-8000-00000000000b"
D1, D2 = "d1111111-0000-4000-8000-000000000001", "d2222222-0000-4000-8000-000000000002"


def iso(e):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(e)) + ".000Z"


class Export:
    """Builds the records of one device export (the format ccm_server.ingest_records reads)."""

    def __init__(self, dev_id, label, tz=0, os_name="Linux"):
        self.dev = dict(device_id=dev_id, machine_id="m-" + dev_id, label=label, hostname=label.lower(), os=os_name,
                        os_release="1", tz_offset_min=tz, agent_version="1.0.0", python="3.12")
        self.recs = []
        self.accounts = {}
        self.n = 0

    def account(self, uuid, email, cfg="~/.claude"):
        self.recs.append({"rec": "account", "account_uuid": uuid, "email": email, "cfg": cfg, "org_uuid": "o-" + uuid,
                          "org_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"})

    def session(self, sid, title, project, acct, calls, entry="cli", conf="observed", cfg="~/.claude", sub=""):
        """calls: list of dicts with ts (epoch), model, ctx, out, cr, cw1, inp, tools"""
        toks, tools = {}, {}
        ctxs = []
        for c in calls:
            t = toks.setdefault(c.get("model", "claude-sonnet-5-5"), dict(inp=0, out=0, cr=0, cw5=0, cw1=0, think=0))
            for k in ("inp", "out", "cr", "cw1"):
                t[k] += c.get(k, 0)
            for n in c.get("tools", []):
                tools[n] = tools.get(n, 0) + 1
            ctxs.append(c["ctx"])
        ts = [c["ts"] for c in calls]
        self.recs.append({"rec": "session", "id": sid, "cfg": cfg, "title": title, "title_kind": "ai", "project": project,
                          "cwd": "/w/" + (project or ""), "branch": "main", "worktree": False, "entrypoint": entry,
                          "version": "2.1.0", "first_ts": iso(min(ts)), "last_ts": iso(max(ts)),
                          "active_s": int(sum(b - a for a, b in zip(ts, ts[1:]) if b - a < 300)), "prompts": 3,
                          "calls": len(calls), "web_requests": 0, "tools": tools, "mcp": {}, "subagents": {},
                          "tokens": toks, "ctx_peak": max(ctxs), "ctx_avg": sum(ctxs) // len(ctxs), "compactions": [],
                          "cost": None, "account_uuid": acct, "account_conf": conf})
        for c in calls:
            self.n += 1
            self.recs.append({"rec": "call", "sid": sid, "sub": sub, "mid": "m%d" % self.n, "ts": iso(c["ts"]),
                              "model": c.get("model", "claude-sonnet-5-5"), "inp": c.get("inp", 3),
                              "out": c.get("out", 600), "cr": c.get("cr", 0), "cw5": 0, "cw1": c.get("cw1", 0),
                              "think": 50, "ctx": c["ctx"], "web": 0, "speed": "standard", "effort": "high",
                              "tools": c.get("tools", [])})

    def util(self, acct, ts, five, seven):
        self.recs.append({"rec": "util", "fetched_ms": int(ts * 1000), "account_uuid": acct,
                          "five_hour": {"utilization": five, "resets_at": iso(ts + 3 * 3600).replace(".000Z", "+00:00")},
                          "seven_day": {"utilization": seven, "resets_at": iso(BASE + 28 * 86400).replace(".000Z", "+00:00")},
                          "breakdown": {"rows": {"claude_code": 100}}})

    def bytes(self):
        recs = [{"rec": "meta", "schema": ccm_server.SCHEMA_VERSION, "device": self.dev}] + self.recs
        return gzip.compress("\n".join(json.dumps(r) for r in recs).encode("utf-8"))


def normal_calls(rng, start, n=18, ctx0=20000, tools=("Read",)):
    out, ctx = [], ctx0
    for i in range(n):
        ctx += rng.randint(500, 2000)
        out.append({"ts": start + i * 60 + rng.randint(0, 20), "ctx": ctx, "cr": int(ctx * 0.9 * rng.uniform(0.7, 1.3)),
                    "cw1": int(ctx * 0.1 * rng.uniform(0.7, 1.3)), "out": rng.randint(400, 900), "tools": [tools[i % len(tools)]]})
    return out


def build_dataset(two_devices=True, spikes=True):
    """Two devices x 25 days of ordinary work plus injected anomalies. Returns {label: Export}."""
    rng = random.Random(11)
    e1 = Export(D1, "alpha-pc", tz=0, os_name="Linux")
    e1.account(ACCT_A, "a@example.com")
    exports = {"alpha-pc": e1}
    e2 = None
    if two_devices:
        e2 = Export(D2, "beta-mac", tz=330, os_name="macOS")
        e2.account(ACCT_A, "a@example.com")
        e2.account(ACCT_B, "b@example.com", cfg="~/.claude-b")
        exports["beta-mac"] = e2
    for day in range(DAYS):
        for k in range(4):
            e1.session("n1-%d-%d" % (day, k), "alpha work %d.%d" % (day, k), "alpha" if k < 2 else "beta", ACCT_A,
                       normal_calls(rng, BASE + day * 86400 + (9 + 2 * k) * 3600), entry="cli" if k % 2 else "sdk-cli")
            if e2:
                gamma = k == 3
                e2.session("n2-%d-%d" % (day, k), "beta work %d.%d" % (day, k), "gamma" if gamma else "alpha",
                           ACCT_B if gamma else ACCT_A,
                           normal_calls(rng, BASE + day * 86400 + (4 + 2 * k) * 3600, tools=("Read", "Bash")),
                           cfg="~/.claude-b" if gamma else "~/.claude", conf="assumed" if day < 5 else "observed")
    # util snapshots so calibration and the Limits view have something to show
    for d, five, seven in ((6, 12, 8), (12, 20, 14), (18, 9, 20)):
        e1.util(ACCT_A, BASE + d * 86400 + 12 * 3600 + 600, five, seven)
    if not spikes:
        return exports
    # spike 1: runaway context (ctx x8 within 3 calls) on alpha-pc
    t, calls = BASE + 14 * 86400 + 10 * 3600, []
    for i, ctx in enumerate([30000, 31000, 32000, 33000, 34000, 120000, 260000, 520000, 540000, 560000]):
        calls.append({"ts": t + i * 70, "ctx": ctx, "cr": int(ctx * 0.5), "cw1": int(ctx * 0.5), "model": "claude-opus-5-5", "tools": ["Read"]})
    e1.session("spike-runaway", "SPIKE runaway context", "alpha", ACCT_A, calls)
    # spike 2: tool loop (140 identical Bash calls)
    t, calls = BASE + 16 * 86400 + 11 * 3600, []
    for i in range(140):
        calls.append({"ts": t + i * 15, "ctx": 60000, "cr": 54000, "cw1": 500, "out": 200, "tools": ["Bash"]})
    e1.session("spike-loop", "SPIKE tool loop", "alpha", ACCT_A, calls)
    # spike 3: cache thrash (no cache reads, big context re-written every call)
    t, calls = BASE + 18 * 86400 + 13 * 3600, []
    for i in range(30):
        calls.append({"ts": t + i * 40, "ctx": 150000 + i * 300, "cr": 0, "cw1": 150000 + i * 300, "model": "claude-opus-5-5", "tools": ["Edit"]})
    e1.session("spike-cache", "SPIKE cache thrash", "alpha", ACCT_A, calls)
    # spike 4: burst at 03:00 UTC (alpha-pc is only ever active 09-17 UTC)
    t, calls = BASE + 20 * 86400 + 3 * 3600, []
    for i in range(70):
        calls.append({"ts": t + i * 45, "ctx": 40000 + i * 100, "cr": 36000, "cw1": 3000, "tools": ["Edit"]})
    e1.session("spike-night", "SPIKE night burst", "alpha", ACCT_A, calls)
    # spike 5: one very expensive day (heavy opus conversation, 300 calls)
    t, calls = BASE + 22 * 86400 + 12 * 3600, []
    for i in range(300):
        ctx = 200000 + i * 500
        calls.append({"ts": t + i * 20, "ctx": ctx, "cr": ctx * 2, "cw1": 2000, "out": 2500, "model": "claude-opus-5-5", "tools": ["Read", "Edit"][i % 2:i % 2 + 1]})
    e1.session("spike-day", "SPIKE expensive day", "alpha", ACCT_A, calls)
    # a hostile title (must round-trip as data; the UI escapes it)
    e1.session("xss", "XSS <img src=x onerror=alert(1)> \"quoted\" & co", "<script>alert(2)</script>", ACCT_A,
               normal_calls(rng, BASE + 5 * 86400 + 20 * 3600))
    return exports


class Http:
    def __init__(self, port):
        self.port = port

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            data = r.read()
            return r.status, dict(r.getheaders()), data
        finally:
            c.close()

    def get(self, path, **params):
        qs = urlencode(params, doseq=True)
        s, h, d = self.req("GET", path + ("?" + qs if qs else ""))
        return s, (json.loads(d.decode("utf-8")) if "json" in h.get("Content-Type", "") else d)

    def ok(self, path, **params):
        s, j = self.get(path, **params)
        assert s == 200, (path, params, s, j)
        return j

    def post(self, path, obj, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        s, hh, d = self.req("POST", path, json.dumps(obj).encode("utf-8"), h)
        return s, json.loads(d.decode("utf-8"))

    def upload(self, path, data, name):
        s, hh, d = self.req("POST", path, data, {"Content-Type": "application/octet-stream", "X-CCM-Filename": quote(name)})
        return s, json.loads(d.decode("utf-8"))


class ServerCase(unittest.TestCase):
    """Starts the dashboard on an ephemeral port against a fresh data dir."""

    def make_server(self, data):
        self.srv = ccm_dashboard.make_server(Path(data), "127.0.0.1", 0)
        self.app = self.srv.app
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.h = Http(self.srv.server_address[1])

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
            self.app.stop.set()
        if getattr(self, "tmp", None):
            shutil.rmtree(str(self.tmp), ignore_errors=True)

    def load(self, exports):
        conn = ccm_server.open_db(self.data)
        for label, e in exports.items():
            res = ccm_server.ingest_bytes(conn, e.bytes(), "ccm-%s.ndjson.gz" % label)
            self.assertEqual(res["status"], "ok")
        conn.close()


class DashBase(ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ccmdash-"))
        cls.data = cls.tmp / "data"
        ccm_server.data_dir(str(cls.data))
        conn = ccm_server.open_db(cls.data)
        cls.exports = build_dataset()
        for label, e in cls.exports.items():
            ccm_server.ingest_bytes(conn, e.bytes(), "ccm-%s.ndjson.gz" % label)
        conn.close()
        cls.srv = ccm_dashboard.make_server(cls.data, "127.0.0.1", 0)
        cls.app = cls.srv.app
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.h = Http(cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.app.stop.set()
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    def tearDown(self):   # class-level server: nothing per test
        pass


class TestApi(DashBase):
    GETS = ["/api/meta", "/api/overview", "/api/cube", "/api/sessions", "/api/tools", "/api/context", "/api/limits",
            "/api/anomalies", "/api/admin", "/api/github/status"]

    def test_static_and_security_headers(self):
        s, h, d = self.h.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"CCM Dashboard", d)
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        for p in ("/app.js", "/views.js", "/views2.js", "/util.js", "/style.css", "/vendor/echarts.min.js"):
            self.assertEqual(self.h.req("GET", p)[0], 200, p)
        # no external script/style at runtime
        html = d.decode()
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", html)

    def test_path_traversal_and_unknown(self):
        for p in ("/../ccm_server.py", "/..%2fccm_server.py", "/%2e%2e/ccm_server.py", "/vendor/../../ccm_server.py",
                  "/nope.txt", "/api/nope", "/static/"):
            self.assertEqual(self.h.req("GET", p)[0], 404, p)

    def test_host_header_must_be_loopback(self):
        s, _, _ = self.h.req("GET", "/api/meta", headers={"Host": "evil.example.com"})
        self.assertEqual(s, 403)

    def test_every_endpoint_with_and_without_filters(self):
        t0 = BASE + 5 * 86400
        filters = [{}, {"from": t0, "to": t0 + 5 * 86400}, {"devices": D1}, {"accounts": ACCT_B},
                   {"projects": "alpha"}, {"models": "claude-opus-5-5"}, {"entrypoints": "cli"}, {"q": "alpha"},
                   {"from": "2026-09-03", "to": "2026-09-09", "devices": [D1, D2], "q": "work"},
                   {"q": "no-such-title-xyz"}]
        for path in self.GETS:
            for f in filters:
                s, j = self.h.get(path, **f)
                self.assertEqual(s, 200, (path, f, j))
                self.assertIsInstance(j, dict)

    def test_meta(self):
        m = self.h.ok("/api/meta")
        self.assertEqual([d["name"] for d in m["devices"]], ["alpha-pc", "beta-mac"])
        self.assertEqual({a["id"] for a in m["accounts"]}, {ACCT_A, ACCT_B})
        self.assertFalse(m["empty"])
        self.assertTrue(m["drop"]["path"].endswith("drop"))
        self.assertIn("alpha", [p[0] for p in m["projects"]])

    def test_filters_partition_totals(self):
        total = self.h.ok("/api/overview")["cur"]
        by_dev = [self.h.ok("/api/overview", devices=d)["cur"] for d in (D1, D2)]
        self.assertEqual(sum(x["calls"] for x in by_dev), total["calls"])
        self.assertAlmostEqual(sum(x["usd"] for x in by_dev), total["usd"], places=2)
        by_acct = [self.h.ok("/api/overview", accounts=a)["cur"] for a in (ACCT_A, ACCT_B)]
        self.assertEqual(sum(x["calls"] for x in by_acct), total["calls"])
        # account B is only the gamma project on beta-mac
        self.assertEqual(self.h.ok("/api/overview", accounts=ACCT_B, devices=D1)["cur"]["calls"], 0)
        gamma = self.h.ok("/api/overview", projects="gamma")["cur"]
        self.assertEqual(gamma["calls"], by_acct[1]["calls"])
        sonnet = self.h.ok("/api/overview", models="claude-sonnet-5-5")["cur"]
        opus = self.h.ok("/api/overview", models="claude-opus-5-5")["cur"]
        self.assertEqual(sonnet["calls"] + opus["calls"], total["calls"])
        q = self.h.ok("/api/sessions", q="SPIKE", limit=50)
        self.assertEqual(q["total"], 5)

    def test_date_range_and_previous_period(self):
        d1 = BASE + 10 * 86400
        r = self.h.ok("/api/overview", **{"from": d1, "to": d1 + 5 * 86400})
        self.assertTrue(r["cur"]["calls"] > 0)
        self.assertTrue(r["prev"]["calls"] > 0)
        self.assertIsNone(self.h.ok("/api/overview")["prev"])
        cube = self.h.ok("/api/cube", **{"from": d1, "to": d1 + 86400})
        hours = {row[0] for row in cube["rows"]}
        self.assertTrue(all(d1 // 3600 <= h < (d1 + 86400) // 3600 for h in hours))
        self.assertEqual(sum(row[4] for row in cube["rows"]), self.h.ok("/api/overview", **{"from": d1, "to": d1 + 86400})["cur"]["calls"])
        # ISO dates are accepted too
        a = self.h.ok("/api/overview", **{"from": "2026-09-11", "to": "2026-09-12"})["cur"]["calls"]
        b = self.h.ok("/api/overview", **{"from": BASE + 10 * 86400, "to": BASE + 11 * 86400})["cur"]["calls"]
        self.assertEqual(a, b)

    def test_sessions_sort_page_and_conversation(self):
        r = self.h.ok("/api/sessions", sort="usd", dir="desc", limit=3)
        self.assertEqual(len(r["rows"]), 3)
        usd = [x["usd"] for x in r["rows"]]
        self.assertEqual(usd, sorted(usd, reverse=True))
        self.assertEqual(r["rows"][0]["title"], "SPIKE expensive day")
        page2 = self.h.ok("/api/sessions", sort="usd", dir="desc", limit=3, offset=3)
        self.assertNotEqual(page2["rows"][0]["sid"], r["rows"][0]["sid"])
        asc = self.h.ok("/api/sessions", sort="title", dir="asc", limit=5)
        self.assertEqual([x["title"].lower() for x in asc["rows"]], sorted(x["title"].lower() for x in asc["rows"]))
        top = r["rows"][0]
        c = self.h.ok("/api/conversation", device=top["d"], sid=top["sid"])
        self.assertEqual(c["ncalls"], 300)
        self.assertEqual(len(c["calls"]), 300)
        self.assertAlmostEqual(c["est_usd"], top["usd"], places=2)
        self.assertEqual(c["models"][0]["model"], "claude-opus-5-5")
        self.assertIn("Read", [t[0] for t in c["tools"]])
        self.assertEqual(self.h.get("/api/conversation", device="x", sid="y")[0], 404)
        self.assertEqual(self.h.get("/api/sessions", limit="abc")[0], 400)

    def test_account_conf_and_estimates(self):
        rows = self.h.ok("/api/sessions", devices=D2, limit=2000)["rows"]
        self.assertEqual({r["conf"] for r in rows}, {"assumed", "observed"})
        self.assertTrue(any(r["p5"] is not None for r in self.h.ok("/api/sessions", accounts=ACCT_A, limit=500)["rows"]))

    def test_tools_and_context(self):
        t = self.h.ok("/api/tools")
        names = dict(t["tools"])
        self.assertGreater(names["Bash"], 140)
        self.assertEqual({m["model"] for m in t["models"]}, {"claude-sonnet-5-5", "claude-opus-5-5"})
        c = self.h.ok("/api/context")
        self.assertEqual(sum(c["hist"]), c["n_calls"])
        self.assertGreaterEqual(c["pct"]["max"], 560000)
        self.assertTrue(any(p["title"] == "SPIKE runaway context" for p in c["top"]))
        self.assertTrue(c["points"])

    def test_limits(self):
        lim = self.h.ok("/api/limits")
        a = next(x for x in lim["accounts"] if x["id"] == ACCT_A)
        self.assertEqual(len(a["snaps"]), 3)
        self.assertGreater(a["caps"]["n_5h"], 0)
        self.assertTrue(a["windows5"])
        self.assertIsNotNone(a["caps"]["cap_5h"])
        self.assertEqual({x["id"] for x in self.h.ok("/api/limits", accounts=ACCT_B)["accounts"]}, {ACCT_B})

    def test_cube_is_consistent_with_overview(self):
        cube = self.h.ok("/api/cube")
        ov = self.h.ok("/api/overview")["cur"]
        self.assertEqual(sum(r[4] for r in cube["rows"]), ov["calls"])
        self.assertAlmostEqual(sum(r[5] for r in cube["rows"]), ov["usd"], places=1)
        self.assertEqual(sum(r[11] for r in cube["rows"]) // 60, int(ov["active_s"]) // 60)

    def test_costs_match_calc(self):
        conn = ccm_server.open_db(self.data)
        try:
            res = ccm_calc.compute(conn)
        finally:
            conn.close()
        self.assertAlmostEqual(self.h.ok("/api/overview")["cur"]["usd"], res["total_usd"], places=2)


class TestAnomalies(DashBase):
    def kinds(self, **kw):
        return self.h.ok("/api/anomalies", **kw)

    def find(self, items, kind, text):
        return [a for a in items if a["kind"] == kind and text in (a["title"] or a["reason"])]

    def test_injected_spikes_found(self):
        an = self.kinds(tz=0)
        items = an["items"]
        self.assertTrue(self.find(items, "ctx_runaway", "SPIKE runaway"), an["counts"])
        self.assertTrue(self.find(items, "tool_loop", "SPIKE tool loop"))
        self.assertTrue(self.find(items, "cache_miss", "SPIKE cache thrash"))
        self.assertTrue([a for a in items if a["kind"] == "off_hours" and a["device_id"] == D1], an["counts"])
        self.assertTrue([a for a in items if a["kind"] == "conv_cost" and a["title"] == "SPIKE expensive day"])
        self.assertTrue([a for a in items if a["kind"] == "daily_spend" and a["scope"] == "dev" and a["device_id"] == D1])
        self.assertTrue([a for a in items if a["kind"] == "daily_spend" and a["scope"] == "acct" and a["account"] == ACCT_A])
        self.assertTrue([a for a in items if a["kind"] == "window_spend"])
        self.assertTrue([a for a in items if a["kind"] == "call_burst" and a["device_id"] == D1])
        # shape of every record
        for a in items:
            for k in ("severity", "kind", "device", "account_name", "sid", "ts", "reason", "label"):
                self.assertIn(k, a)
            self.assertGreaterEqual(a["severity"], 50)
            self.assertLessEqual(a["severity"], 100)
            self.assertTrue(a["reason"])
        self.assertEqual([a["severity"] for a in items], sorted((a["severity"] for a in items), reverse=True))

    def test_calm_data_has_no_noise(self):
        # the ordinary 25 days alone (no spikes) must not look anomalous in the spike detectors
        tmp = Path(tempfile.mkdtemp(prefix="ccmcalm-"))
        try:
            data = tmp / "d"
            ccm_server.data_dir(str(data))
            conn = ccm_server.open_db(data)
            for e in build_dataset(spikes=False).values():
                ccm_server.ingest_bytes(conn, e.bytes(), "x%s.gz" % e.dev["label"])
            conn.close()
            conn = ccm_server.open_db(data)
            import ccm_store
            st = ccm_store.Store.build(conn)
            conn.close()
            found = ccm_anomaly.detect(st, 5.0, 0)
            bad = [a for a in found if a["kind"] in ("daily_spend", "window_spend", "call_burst", "ctx_runaway", "cache_miss", "tool_loop", "conv_cost")]
            self.assertEqual(bad, [], [a["reason"] for a in bad])
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_sensitivity_is_monotonic(self):
        n = [self.kinds(sens=s)["total"] for s in (1, 5, 10)]
        self.assertLessEqual(n[0], n[1])
        self.assertLessEqual(n[1], n[2])
        self.assertLess(n[0], n[2])
        self.assertEqual(self.h.get("/api/anomalies", sens="x")[0], 400)

    def test_filters_apply_to_anomalies(self):
        all_ = self.kinds()["items"]
        self.assertTrue(all(a["device_id"] in (D1, None) or a["device_id"] == D1 for a in self.kinds(devices=D1)["items"]))
        only = self.kinds(devices=D2)["items"]
        self.assertFalse([a for a in only if a["title"] and a["title"].startswith("SPIKE")])
        self.assertLess(len(only), len(all_))
        day = BASE + 14 * 86400
        ranged = self.kinds(**{"from": day, "to": day + 86400})["items"]
        self.assertTrue(self.find(ranged, "ctx_runaway", "SPIKE runaway"))
        self.assertFalse(self.find(ranged, "tool_loop", "SPIKE tool loop"))
        proj = self.kinds(projects="alpha")["items"]
        self.assertTrue(all(a["sid"] for a in proj))       # project filter hides device-level (session-less) items
        self.assertEqual(self.kinds(kinds="tool_loop")["counts"].keys(), {"tool_loop"})

    def test_links_open_the_conversation(self):
        a = self.find(self.kinds()["items"], "tool_loop", "SPIKE tool loop")[0]
        c = self.h.ok("/api/conversation", device=a["device_id"], sid=a["sid"])
        self.assertEqual(c["row"]["title"], "SPIKE tool loop")

    def test_unit_helpers(self):
        med, scale = ccm_anomaly.robust_z([5.0] * 10)
        self.assertGreater(scale, 0)                    # constant series: floor, no division by zero
        med, scale = ccm_anomaly.robust_z([1, 2, 3, 4, 100])
        self.assertEqual(med, 3)
        self.assertEqual(ccm_anomaly.sev(1.0, 2.0), 0)
        self.assertEqual(ccm_anomaly.sev(2.0, 2.0), 50)
        self.assertEqual(ccm_anomaly.sev(2000.0, 2.0), 100)
        self.assertLess(ccm_anomaly.params(1)["z"], 6.0)
        self.assertGreater(ccm_anomaly.params(1)["z"], ccm_anomaly.params(10)["z"])


class TestWrites(DashBase):
    # these tests mutate the shared DB, so each restores what it changed
    def test_nickname_label_and_effective_name(self):
        s, r = self.h.post("/api/account", {"account_uuid": ACCT_B, "nickname": "Work <b>"})
        self.assertEqual((s, r["ok"]), (200, True))
        self.assertEqual(next(a for a in self.h.ok("/api/meta")["account_info"] if a["id"] == ACCT_B)["name"], "Work <b>")
        self.assertEqual(self.h.post("/api/account", {"account_uuid": "nope", "nickname": "x"})[0], 400)
        self.h.post("/api/account", {"account_uuid": ACCT_B, "nickname": ""})
        self.assertEqual(next(a for a in self.h.ok("/api/meta")["account_info"] if a["id"] == ACCT_B)["name"], "b@example.com")
        self.assertEqual(self.h.post("/api/device", {"device_id": D1, "label": "Renamed"})[0], 200)
        self.assertEqual(self.h.ok("/api/meta")["devices"][0]["name"], "Renamed")
        self.assertEqual(self.h.post("/api/device", {"device_id": "zzz", "label": "x"})[0], 400)
        self.h.post("/api/device", {"device_id": D1, "label": "alpha-pc"})

    def test_capacity_changes_estimates(self):
        before = self.h.ok("/api/limits", accounts=ACCT_A)["accounts"][0]
        s, _ = self.h.post("/api/capacity", {"account_uuid": ACCT_A, "cap_5h": 50, "cap_7d": 400, "note": "manual"})
        self.assertEqual(s, 200)
        after = self.h.ok("/api/limits", accounts=ACCT_A)["accounts"][0]
        self.assertEqual(after["caps"]["cap_5h"], 50)
        self.assertEqual(after["caps"]["source"], "manual")
        self.assertEqual(after["caps"]["manual_7d"], 400)
        self.assertNotEqual(before["caps"]["cap_5h"], 50)
        for bad in ({"account_uuid": ACCT_A, "cap_5h": -1}, {"account_uuid": ACCT_A, "cap_5h": "abc"},
                    {"account_uuid": ACCT_A, "cap_5h": "nan"}, {"account_uuid": "nope", "cap_5h": 1}):
            self.assertEqual(self.h.post("/api/capacity", bad)[0], 400, bad)
        self.h.post("/api/capacity", {"account_uuid": ACCT_A, "cap_5h": "", "cap_7d": ""})
        again = self.h.ok("/api/limits", accounts=ACCT_A)["accounts"][0]
        self.assertIsNone(again["caps"]["manual_5h"])
        self.assertAlmostEqual(again["caps"]["cap_5h"], before["caps"]["cap_5h"], places=1)

    def test_price_edit_changes_cost(self):
        usd = self.h.ok("/api/overview", models="claude-sonnet-5-5")["cur"]["usd"]
        row = next(p for p in self.h.ok("/api/admin")["prices"] if p["pattern"] == "claude-sonnet-5")
        new = dict(row, out_p=row["out_p"] * 2, cr_p=row["cr_p"] * 2)
        self.assertEqual(self.h.post("/api/price", new)[0], 200)
        self.assertGreater(self.h.ok("/api/overview", models="claude-sonnet-5-5")["cur"]["usd"], usd * 1.2)
        self.h.post("/api/price", row)
        self.assertAlmostEqual(self.h.ok("/api/overview", models="claude-sonnet-5-5")["cur"]["usd"], usd, places=3)
        self.assertEqual(self.h.post("/api/price", {"pattern": "x", "in_p": -1, "out_p": 1, "cr_p": 1, "cw5_p": 1, "cw1_p": 1})[0], 400)
        self.assertEqual(self.h.post("/api/price", {"pattern": "", "in_p": 1, "out_p": 1, "cr_p": 1, "cw5_p": 1, "cw1_p": 1})[0], 400)
        self.assertEqual(self.h.post("/api/price", {"pattern": "tmp-model", "in_p": 1, "out_p": 2, "cr_p": 0.1, "cw5_p": 1, "cw1_p": 1})[0], 200)
        self.assertIn("tmp-model", [p["pattern"] for p in self.h.ok("/api/admin")["prices"]])
        self.assertEqual(self.h.post("/api/price/delete", {"pattern": "tmp-model"})[0], 200)
        self.assertEqual(self.h.post("/api/price/delete", {"pattern": "*"})[0], 400)

    def test_override_rules_move_cost_between_accounts(self):
        gamma_b = self.h.ok("/api/overview", accounts=ACCT_B)["cur"]["usd"]
        self.assertGreater(gamma_b, 0)
        s, r = self.h.post("/api/override", {"account_uuid": ACCT_A, "project_like": "gamma", "note": "test"})
        self.assertEqual(s, 200)
        self.assertEqual(self.h.ok("/api/overview", accounts=ACCT_B)["cur"]["calls"], 0)
        rows = self.h.ok("/api/sessions", projects="gamma", limit=5)["rows"]
        self.assertEqual({x["conf"] for x in rows}, {"override"})
        self.assertEqual({x["acct_id"] for x in rows}, {ACCT_A})
        self.assertEqual(len(self.h.ok("/api/admin")["overrides"]), 1)
        self.assertEqual(self.h.post("/api/override/delete", {"id": r["id"]})[0], 200)
        self.assertAlmostEqual(self.h.ok("/api/overview", accounts=ACCT_B)["cur"]["usd"], gamma_b, places=3)
        for bad in ({"account_uuid": ACCT_A}, {"account_uuid": "nope", "project_like": "x"},
                    {"account_uuid": ACCT_A, "project_like": "x", "from_ts": "yesterday"}):
            self.assertEqual(self.h.post("/api/override", bad)[0], 400, bad)
        self.assertEqual(self.h.post("/api/override/delete", {"id": "abc"})[0], 400)

    def test_write_endpoint_hardening(self):
        # GET routes are not writable and unknown POSTs 404
        self.assertEqual(self.h.post("/api/meta", {})[0], 404)
        self.assertEqual(self.h.post("/api/sql", {"q": "drop table sessions"})[0], 404)
        # wrong content type
        s, _, d = self.h.req("POST", "/api/account", b'{"account_uuid":"x"}', {"Content-Type": "text/plain"})
        self.assertEqual(s, 415)
        # cross-origin browsers send Origin: refuse when it does not match Host
        s, r = self.h.post("/api/account", {"account_uuid": ACCT_B, "nickname": "x"}, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(s, 403)
        s, r = self.h.post("/api/account", {"account_uuid": ACCT_B, "nickname": ""},
                           headers={"Origin": "http://127.0.0.1:%d" % self.h.port})
        self.assertEqual(s, 200)
        # invalid JSON / non-object / oversized
        self.assertEqual(self.h.req("POST", "/api/account", b"{nope", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.h.req("POST", "/api/account", b"[1]", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.h.req("POST", "/api/account", b"x" * 70000, {"Content-Type": "application/json"})[0], 413)
        # SQL metacharacters are data, not code
        s, _ = self.h.post("/api/device", {"device_id": D1, "label": "x'); DROP TABLE sessions;--"})
        self.assertEqual(s, 200)
        self.assertGreater(self.h.ok("/api/overview")["cur"]["calls"], 1000)
        self.h.post("/api/device", {"device_id": D1, "label": "alpha-pc"})
        # no other table is writable: the ingest data is intact
        conn = ccm_server.open_db(self.data)
        self.assertGreater(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 100)
        conn.close()


class TestEscaping(unittest.TestCase):
    PAYLOADS = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "' onmouseover='alert(1)", "a&b<c>d\"e'f`g",
                "</td><td>", "javascript:alert(1)", None, 5]

    def test_python_helper(self):
        for p in self.PAYLOADS:
            out = ccm_dashboard.esc_html(p)
            for ch in '<>"\'':
                self.assertNotIn(ch, out)
        self.assertEqual(ccm_dashboard.esc_html("a&b"), "a&amp;b")
        self.assertEqual(ccm_dashboard.esc_html(None), "")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_javascript_helper_matches(self):
        util = ROOT / "server" / "static" / "util.js"
        js = ("const u=require(%s); const p=%s; console.log(JSON.stringify(p.map(x=>u.esc(x))));"
              % (json.dumps(str(util)), json.dumps(self.PAYLOADS)))
        out = subprocess.run(["node", "-e", js], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        escaped = json.loads(out.stdout.decode())
        for s in escaped:
            for ch in '<>"\'`':
                self.assertNotIn(ch, s)
        self.assertEqual(escaped[0], "&lt;script&gt;alert(1)&lt;/script&gt;")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_syntax_and_no_unescaped_innerhtml_of_raw_titles(self):
        for f in ("util.js", "app.js", "views.js", "views2.js"):
            r = subprocess.run(["node", "--check", str(ROOT / "server" / "static" / f)], stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
        import re as _re
        for f in ("app.js", "views.js", "views2.js"):
            code = (ROOT / "server" / "static" / f).read_text()
            self.assertIsNone(_re.search(r"\son(submit|click|change|input|load|error)=\\?[\"']", code), f)   # CSP forbids inline handlers
        src = (ROOT / "server" / "static" / "views2.js").read_text() + (ROOT / "server" / "static" / "views.js").read_text()
        # user-derived fields must only appear inside esc(...) in markup
        import re
        for field in ("x.title", "r.title", "a.reason", "p.title", "c.cwd", "x.project", "d.label", "o.note"):
            for m in re.finditer(r"\+\s*" + re.escape(field) + r"\b(?!\s*\))", src):
                ctx = src[max(0, m.start() - 12):m.end() + 2]
                self.assertIn("esc(", src[max(0, m.start() - 60):m.end()], ctx)

    def test_hostile_titles_round_trip_as_data(self):
        pass  # covered in TestApi.test_hostile_title (needs the dataset)


class TestHostileData(DashBase):
    def test_hostile_title_is_returned_verbatim_as_json(self):
        r = self.h.ok("/api/sessions", q="onerror", limit=5)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["rows"][0]["title"], "XSS <img src=x onerror=alert(1)> \"quoted\" & co")
        self.assertEqual(r["rows"][0]["project"], "<script>alert(2)</script>")
        s, h, d = self.h.req("GET", "/api/sessions?q=onerror")
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")


class TestEmptyAndSingleDevice(ServerCase):
    def test_empty_db(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmempty-"))
        self.data = self.tmp / "data"
        self.make_server(self.data)
        m = self.h.ok("/api/meta")
        self.assertTrue(m["empty"])
        self.assertEqual(m["devices"], [])
        self.assertIn("drop", m["drop"]["path"])
        for p in TestApi.GETS:
            s, j = self.h.get(p)
            self.assertEqual(s, 200, (p, j))
        self.assertEqual(self.h.ok("/api/overview")["cur"]["calls"], 0)
        self.assertEqual(self.h.ok("/api/cube")["rows"], [])
        self.assertEqual(self.h.ok("/api/sessions")["rows"], [])
        self.assertEqual(self.h.ok("/api/anomalies")["items"], [])
        self.assertEqual(self.h.ok("/api/limits")["accounts"], [])
        self.assertEqual(self.h.ok("/api/context")["n_calls"], 0)
        self.assertEqual(self.h.ok("/api/tools")["tools"], [])
        self.assertEqual(self.h.ok("/api/admin")["devices"], [])
        self.assertTrue(self.h.ok("/api/admin")["prices"])        # default price table exists
        self.assertEqual(self.h.get("/api/conversation", device="a", sid="b")[0], 404)

    def test_single_device(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmone-"))
        self.data = self.tmp / "data"
        ccm_server.data_dir(str(self.data))
        self.load({"alpha-pc": build_dataset(two_devices=False)["alpha-pc"]})
        self.make_server(self.data)
        m = self.h.ok("/api/meta")
        self.assertEqual(len(m["devices"]), 1)
        self.assertFalse(m["empty"])
        for p in TestApi.GETS:
            self.assertEqual(self.h.get(p)[0], 200, p)
            self.assertEqual(self.h.get(p, devices=D1)[0], 200, p)
        self.assertEqual(self.h.ok("/api/overview")["cur"]["devices"], 1)
        self.assertEqual(self.h.ok("/api/overview", devices=D2)["cur"]["calls"], 0)   # unknown device id: empty, not an error
        self.assertTrue(self.h.ok("/api/anomalies")["items"])

    def test_corrupt_or_missing_numbers_do_not_break(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmodd-"))
        self.data = self.tmp / "data"
        ccm_server.data_dir(str(self.data))
        e = Export(D1, "odd")
        e.account(ACCT_A, "a@example.com")
        e.session("s1", None, None, ACCT_A, [{"ts": BASE, "ctx": 0}, {"ts": BASE + 10, "ctx": 0}], entry=None)
        self.load({"odd": e})
        self.make_server(self.data)
        for p in TestApi.GETS:
            self.assertEqual(self.h.get(p)[0], 200, p)
        self.assertEqual(self.h.ok("/api/sessions")["total"], 1)


class TestImportAndDrop(ServerCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmimp-"))
        self.data = self.tmp / "data"
        self.make_server(self.data)
        self.ex = build_dataset(spikes=False)

    def test_import_endpoint(self):
        self.assertTrue(self.h.ok("/api/meta")["empty"])
        s, r = self.h.upload("/api/import", self.ex["alpha-pc"].bytes(), "ccm-alpha-pc-20260926.ndjson.gz")
        self.assertEqual(s, 200, r)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["device"], "alpha-pc")
        self.assertEqual(r["sessions"], DAYS * 4)
        self.assertGreater(r["calls"], 1000)
        m = self.h.ok("/api/meta")                       # compute cache invalidated: views see the data at once
        self.assertFalse(m["empty"])
        self.assertEqual(self.h.ok("/api/overview")["cur"]["conversations"], DAYS * 4)
        s, r2 = self.h.upload("/api/import", self.ex["alpha-pc"].bytes(), "again.ndjson.gz")
        self.assertEqual((s, r2["status"]), (200, "duplicate"))
        self.assertEqual(r2["device"], "alpha-pc")
        # a newer full snapshot of the same device updates it without double counting
        newer = build_dataset(two_devices=False, spikes=True)["alpha-pc"]
        s, r3 = self.h.upload("/api/import", newer.bytes(), "ccm-alpha-pc-20261001.ndjson.gz")
        self.assertEqual(s, 200)
        calls = self.h.ok("/api/overview")["cur"]["calls"]
        s, r4 = self.h.upload("/api/import", self.ex["beta-mac"].bytes(), "ccm-beta-mac.ndjson.gz")
        self.assertEqual((s, r4["status"]), (200, "ok"))
        self.assertEqual(len(self.h.ok("/api/meta")["devices"]), 2)
        self.assertGreater(self.h.ok("/api/overview")["cur"]["calls"], calls)

    def test_import_errors(self):
        s, r = self.h.upload("/api/import", b"this is not an export", "junk.ndjson.gz")
        self.assertEqual(s, 400)
        self.assertEqual(r["status"], "error")
        self.assertTrue(r["error"])
        s, r = self.h.upload("/api/import", gzip.compress(b'{"rec":"meta","schema":99,"device":{}}\n'), "bad-schema.gz")
        self.assertEqual((s, r["status"]), (400, "error"))
        s, r = self.h.upload("/api/import", gzip.compress(b'{"rec":"meta","schema":1,"device":{"device_id":"x"}}\nnot json\n'), "half.gz")
        self.assertEqual((s, r["status"]), (400, "error"))
        self.assertEqual(self.h.req("POST", "/api/import", b"", {"Content-Length": "0"})[0], 413)
        s, _, _ = self.h.req("POST", "/api/import", b"x", {"Origin": "http://evil.example.com"})
        self.assertEqual(s, 403)
        self.assertTrue(self.h.ok("/api/meta")["empty"])

    def test_filename_cannot_escape(self):
        s, r = self.h.upload("/api/import", self.ex["alpha-pc"].bytes(), "../../evil.ndjson.gz")
        self.assertEqual(s, 200)
        self.assertEqual(r["file"], "evil.ndjson.gz")
        self.assertFalse((self.tmp / "evil.ndjson.gz").exists())

    def test_drop_folder(self):
        drop = self.data / "drop"
        self.assertEqual(Path(self.h.ok("/api/meta")["drop"]["path"]), drop)
        good, dup, bad = drop / "ccm-alpha-pc.ndjson.gz", drop / "ccm-alpha-pc-copy.ndjson.gz", drop / "broken.ndjson.gz"
        good.write_bytes(self.ex["alpha-pc"].bytes())
        dup.write_bytes(self.ex["alpha-pc"].bytes())
        bad.write_bytes(b"garbage")
        (drop / "notes.txt").write_text("ignored")
        fresh = drop / "fresh.ndjson.gz"
        fresh.write_bytes(self.ex["beta-mac"].bytes())          # just written: must wait until it settles
        old = time.time() - 30
        for p in (good, dup, bad):
            os.utime(str(p), (old, old))
        self.assertEqual(self.app.poll_drop(), 3)
        self.assertTrue(fresh.exists())
        self.assertFalse(good.exists())
        self.assertEqual(sorted(p.name for p in (drop / "done").iterdir()), ["ccm-alpha-pc-copy.ndjson.gz", "ccm-alpha-pc.ndjson.gz"])
        self.assertTrue((drop / "failed" / "broken.ndjson.gz").exists())
        err = (drop / "failed" / "broken.ndjson.gz.error.txt").read_text()
        self.assertTrue(err.strip())
        self.assertTrue((drop / "notes.txt").exists())
        self.assertEqual(self.h.ok("/api/overview")["cur"]["conversations"], DAYS * 4)
        info = self.h.ok("/api/meta")["drop"]
        self.assertEqual(info["pending"], 1)
        self.assertEqual({r["status"] for r in info["recent"]}, {"ok", "duplicate", "error"})
        os.utime(str(fresh), (old, old))
        self.assertEqual(self.app.poll_drop(), 1)
        self.assertEqual(len(self.h.ok("/api/meta")["devices"]), 2)
        # same name again goes to done/ with a unique suffix, never overwriting
        again = drop / "ccm-alpha-pc.ndjson.gz"
        again.write_bytes(self.ex["alpha-pc"].bytes())
        os.utime(str(again), (old, old))
        self.assertEqual(self.app.poll_drop(), 1)
        self.assertEqual(len(list((drop / "done").iterdir())), 4)

    def test_drop_loop_thread_runs(self):
        old_poll = ccm_dashboard.DROP_POLL_S
        ccm_dashboard.DROP_POLL_S = 0.2
        try:
            t = threading.Thread(target=self.app.drop_loop, daemon=True)
            t.start()
            p = self.data / "drop" / "auto.ndjson.gz"
            p.write_bytes(self.ex["alpha-pc"].bytes())
            old = time.time() - 30
            os.utime(str(p), (old, old))
            for _ in range(50):
                if not p.exists():
                    break
                time.sleep(0.2)
            self.assertFalse(p.exists())
            self.assertFalse(self.h.ok("/api/meta")["empty"])
        finally:
            ccm_dashboard.DROP_POLL_S = old_poll


class TestGithubIntegration(AgentBase, ServerCase):
    def setUp(self):
        AgentBase.setUp(self)
        os.environ["CCM_NO_REVEAL"] = "1"
        self.fake = FakeGitHub()
        self.url = self.fake.start()
        self.old_api = ccm_agent.GITHUB_API
        ccm_agent.GITHUB_API = self.url
        self.data = self.tmp / "srv"
        ccm_server.data_dir(str(self.data))
        self.make_server(self.data)
        write_jsonl(self.cfg / "projects" / "p" / "s1.jsonl", [
            user("s1", "hello", 0), assistant("s1", "m1", 1, []), assistant("s1", "m2", 2, [])])
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com")

    def tearDown(self):
        ccm_agent.GITHUB_API = self.old_api
        os.environ.pop("CCM_NO_REVEAL", None)
        self.fake.stop()
        ServerCase.tearDown(self)
        AgentBase.tearDown(self)

    def test_not_configured(self):
        st = self.h.ok("/api/github/status")
        self.assertFalse(st["configured"])
        s, r = self.h.post("/api/github/request", {})
        self.assertEqual(s, 400)
        self.assertIn("not configured", r["error"])
        s, r = self.h.post("/api/github/pull", {})
        self.assertEqual(s, 400)
        self.assertEqual(self.h.ok("/api/meta")["empty"], True)      # the dashboard still works without GitHub

    def test_request_pull_cycle(self):
        code = ccm_gh.init(self.data, "o/r", "tok")
        rc, out = self.run_agent("install", "--join", code, "--label", "far-away", "--no-schedule")
        self.assertEqual(rc, 0, out)
        st = self.h.ok("/api/github/status")
        self.assertTrue(st["configured"])
        self.assertEqual(st["repo"], "o/r")
        self.assertEqual(st["devices"], [])                           # nothing pulled yet
        self.assertEqual(st["last_request"]["id"], 0)
        # pull now: imports the device and shows per-branch results
        s, r = self.h.post("/api/github/pull", {})
        self.assertEqual(s, 200, r)
        self.assertEqual([x["status"] for x in r["results"]], ["ok"])
        self.assertEqual(r["results"][0]["label"], "far-away")
        self.assertFalse(self.h.ok("/api/meta")["empty"])             # cache invalidated after the import
        self.assertEqual(self.h.ok("/api/overview")["cur"]["calls"], 2)
        s, r = self.h.post("/api/github/pull", {})
        self.assertEqual([x["status"] for x in r["results"]], ["unchanged"])
        # request data now (new activity first): device has not answered yet
        write_jsonl(self.cfg / "projects" / "p" / "s2.jsonl", [user("s2", "q", 0), assistant("s2", "m3", 1, [])])
        s, r = self.h.post("/api/github/request", {})
        self.assertEqual(s, 200, r)
        rid = r["request"]["id"]
        self.assertGreater(rid, 0)
        st = self.h.ok("/api/github/status", fresh=1)
        self.assertEqual(st["last_request"]["id"], rid)
        self.assertLess(st["devices"][0]["handled_request"] or 0, rid)
        # the device answers on its next poll; after a pull the dashboard sees it
        _, out = self.run_agent("poll")
        self.assertIn("upload: ok", out)
        self.h.post("/api/github/pull", {})
        st = self.h.ok("/api/github/status", fresh=1)
        self.assertGreaterEqual(st["devices"][0]["handled_request"], rid)
        self.assertEqual(self.h.ok("/api/overview")["cur"]["calls"], 3)
        # targeted request and the auto-upload control
        s, r = self.h.post("/api/github/request", {"devices": ["far-away"]})
        self.assertEqual((s, r["request"]["devices"]), (200, ["far-away"]))
        s, r = self.h.post("/api/github/request", {"auto_hours": 6, "trigger": False})
        self.assertEqual((s, r["request"]["auto_hours"]), (200, 6))
        self.assertEqual(self.h.ok("/api/github/status", fresh=1)["last_request"]["auto_hours"], 6)
        self.assertEqual(self.h.post("/api/github/request", {"auto_hours": -1, "trigger": False})[0], 400)
        self.assertEqual(self.h.post("/api/github/request", {"devices": "x"})[0], 400)
        self.assertEqual(self.h.post("/api/github/request", {"trigger": False})[0], 400)

    def test_offline_never_crashes_and_errors_are_visible(self):
        ccm_gh.init(self.data, "o/r", "tok")
        ccm_agent.GITHUB_API = "http://127.0.0.1:1"            # unreachable
        st = self.h.ok("/api/github/status", fresh=1)
        self.assertTrue(st["configured"])
        self.assertTrue(st.get("error"))
        s, r = self.h.post("/api/github/pull", {})
        self.assertEqual(s, 502)
        self.assertIn("GitHub", r["error"])
        st = self.h.ok("/api/github/status")
        self.assertTrue(st["pull"]["error"])
        s, r = self.h.post("/api/github/request", {})
        self.assertEqual(s, 502)
        self.assertEqual(self.h.get("/api/meta")[0], 200)       # everything else keeps working offline
        self.assertEqual(self.h.get("/api/overview")[0], 200)
        # background loop swallows the error as well
        self.app.gh_loop_once = True
        ccm_dashboard.GH_POLL_S, old = 0.05, ccm_dashboard.GH_POLL_S
        try:
            t = threading.Thread(target=self.app.gh_loop, daemon=True)
            t.start()
            time.sleep(0.4)
            self.assertTrue(t.is_alive())
        finally:
            ccm_dashboard.GH_POLL_S = old

    def test_background_pull_loop_imports(self):
        code = ccm_gh.init(self.data, "o/r", "tok")
        self.run_agent("install", "--join", code, "--label", "bg", "--no-schedule")
        old = ccm_dashboard.GH_POLL_S
        ccm_dashboard.GH_POLL_S = 0.1
        try:
            threading.Thread(target=self.app.gh_loop, daemon=True).start()
            for _ in range(60):
                if not self.h.ok("/api/meta")["empty"]:
                    break
                time.sleep(0.1)
            self.assertFalse(self.h.ok("/api/meta")["empty"])
            self.assertEqual(self.h.ok("/api/github/status")["pull"]["error"], None)
        finally:
            ccm_dashboard.GH_POLL_S = old

    def test_wrong_passphrase_reported(self):
        code = ccm_gh.init(self.data, "o/r", "tok")
        self.run_agent("install", "--join", code, "--label", "x", "--no-schedule")
        cfg = ccm_gh.load_config(self.data)
        cfg["passphrase"] = "different"
        ccm_gh.save_config(self.data, cfg)
        s, r = self.h.post("/api/github/pull", {})
        self.assertEqual(s, 200)
        self.assertEqual(r["results"][0]["status"], "error")
        self.assertTrue(self.h.ok("/api/meta")["empty"])


class TestPythonCompat(unittest.TestCase):
    def test_python38_syntax(self):
        import ast
        for p in ("server/ccm_dashboard.py", "server/ccm_store.py", "server/ccm_anomaly.py", "tests/seed_demo.py",
                  "tests/test_dashboard.py"):
            src = (ROOT / p).read_text(encoding="utf-8")
            ast.parse(src, filename=p, feature_version=(3, 8))


class TestSeedDemo(unittest.TestCase):
    def test_seed_demo_builds_multi_device_db(self):
        tmp = Path(tempfile.mkdtemp(prefix="ccmseed-"))
        try:
            src = tmp / "src"
            ccm_server.data_dir(str(src))
            conn = ccm_server.open_db(src)
            rng = random.Random(5)
            e = Export("s-dev", "orig", tz=330)
            e.account(ACCT_A, "a@example.com")
            for d in range(12):
                for k in range(3):
                    e.session("o%d-%d" % (d, k), "t%d" % k, "proj", ACCT_A, normal_calls(rng, BASE + d * 86400 + (3 + 4 * k) * 3600))
            ccm_server.ingest_bytes(conn, e.bytes(), "o.gz")
            conn.close()
            sys.path.insert(0, str(ROOT / "tests"))
            import seed_demo
            out = tmp / "out"
            buf_rc = seed_demo.main([str(src / "ccm.db"), str(out)])
            self.assertEqual(buf_rc, 0)
            c = ccm_server.open_db(out)
            self.assertEqual(c.execute("SELECT count(*) FROM devices").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT count(*) FROM accounts").fetchone()[0], 2)
            self.assertEqual({r[0] for r in c.execute("SELECT DISTINCT account_conf FROM sessions")} & {"assumed", "observed"}, {"assumed", "observed"})
            self.assertGreater(c.execute("SELECT count(*) FROM util_snapshots").fetchone()[0], 10)
            self.assertEqual(c.execute("SELECT count(*) FROM sessions WHERE title LIKE '%runaway context%'").fetchone()[0], 1)
            c.close()
            # the source is untouched
            c = sqlite3_ro(src / "ccm.db")
            self.assertEqual(c.execute("SELECT count(*) FROM sessions").fetchone()[0], 36)
            c.close()
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


def sqlite3_ro(p):
    import sqlite3
    return sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)


if __name__ == "__main__":
    unittest.main()
