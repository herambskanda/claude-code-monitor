"""Unit + end-to-end tests. Run: python3 -m unittest discover -s tests -v   (stdlib only)."""
import gzip
import io
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "server"))
import ccm_agent  # noqa: E402
import ccm_calc  # noqa: E402
import ccm_server  # noqa: E402

SECRET_PROMPT = "my-super-secret-prompt-text-XYZZY"
SECRET_FILE = "FILE-CONTENT-SHOULD-NEVER-LEAK"


def ts(minute, hour=10, day=5):
    return "2026-10-%02dT%02d:%02d:00.000Z" % (day, hour, minute)


def usage(inp=2, out=100, cr=1000, cw5=0, cw1=500, think=10):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw5 + cw1,
            "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1},
            "output_tokens_details": {"thinking_tokens": think},
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "speed": "standard"}


def assistant(sid, mid, minute, blocks, u=None, model="claude-sonnet-5-5", cwd="/work/proj", agent=None, **kw):
    r = {"parentUuid": None, "isSidechain": bool(agent), "message": {
        "model": model, "id": mid, "type": "message", "role": "assistant", "content": blocks,
        "usage": u or usage()}, "type": "assistant", "uuid": "u-%s-%s" % (mid, len(blocks)),
         "timestamp": ts(minute), "entrypoint": "cli", "cwd": cwd, "sessionId": sid, "version": "2.1.0",
         "gitBranch": "main", "effort": "high"}
    if agent:
        r["agentId"] = agent
    r.update(kw)
    return r


def user(sid, text, minute, uid=None, **kw):
    r = {"parentUuid": None, "isSidechain": False, "type": "user", "message": {"role": "user", "content": text},
         "uuid": uid or "uu-%s-%s" % (sid, minute), "timestamp": ts(minute), "cwd": "/work/proj", "sessionId": sid,
         "entrypoint": "cli", "version": "2.1.0"}
    r.update(kw)
    return r


def tool_result(sid, minute):
    return {"type": "user", "uuid": "tr-%s" % minute, "timestamp": ts(minute), "sessionId": sid, "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": SECRET_FILE}]}}


def tool_use(name):
    return {"type": "tool_use", "id": "t-" + name, "name": name, "input": {"path": SECRET_FILE}}


def write_jsonl(path, records, crlf=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    nl = "\r\n" if crlf else "\n"
    with open(str(path), "wb") as f:
        for r in records:
            f.write((json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
                    if not crlf else (json.dumps(r, separators=(",", ":"), ensure_ascii=False) + nl).encode("utf-8"))


def append_jsonl(path, records, partial=None):
    with open(str(path), "ab") as f:
        for r in records:
            f.write((json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        if partial:
            f.write(partial.encode("utf-8"))


def claude_json(path, uuid, email, util=None):
    d = {"machineID": "mach-1", "oauthAccount": {
        "accountUuid": uuid, "emailAddress": email, "organizationUuid": "org-" + uuid,
        "organizationName": "Org", "organizationType": "claude_max",
        "organizationRateLimitTier": "default_claude_max_20x", "billingType": "stripe", "displayName": "d",
        "secretToken": "SHOULD-NOT-LEAK"}, "oauthToken": "SHOULD-NOT-LEAK"}
    if util:
        d["cachedUsageUtilization"] = util
    path.write_text(json.dumps(d), encoding="utf-8")


class AgentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmtest-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.old_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CCM_HOME")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.old_home = ccm_agent.CCM_HOME
        ccm_agent.CCM_HOME = self.tmp / "ccm"
        self.cfg = self.home / ".claude"

    def tearDown(self):
        ccm_agent.CCM_HOME = self.old_home
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def run_agent(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ccm_agent.main(list(args))
        return rc, buf.getvalue()

    def latest_export(self):
        files = list((ccm_agent.CCM_HOME / "outbox").glob("*.ndjson.gz")) + \
            list((ccm_agent.CCM_HOME / "sent").glob("*.ndjson.gz"))
        return max(files, key=lambda f: f.stat().st_mtime_ns)

    @staticmethod
    def read_export(path):
        with gzip.open(str(path), "rt", encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def session_of(self, recs, sid):
        return next(r for r in recs if r["rec"] == "session" and r["id"] == sid)


class TestParsing(AgentBase):
    def make_session(self):
        sid = "sess-1"
        proj = self.cfg / "projects" / "-work-proj"
        recs = [
            user(sid, SECRET_PROMPT, 0),
            # one API call streamed as 3 lines with growing usage and different blocks
            assistant(sid, "msg_A", 1, [{"type": "thinking", "thinking": ""}], usage(out=10)),
            assistant(sid, "msg_A", 1, [tool_use("Read"), tool_use("mcp__github__create_issue")], usage(out=60)),
            assistant(sid, "msg_A", 1, [tool_use("mcp__github__create_issue"), tool_use("Bash")], usage(out=200)),
            tool_result(sid, 2),
            assistant(sid, "msg_B", 3, [tool_use("mcp__playwright__browser_click")], usage(out=50, cr=3000)),
            user(sid, "summary of the conversation", 4, isCompactSummary=True),
            user(sid, "<local-command-stdout>x</local-command-stdout>", 4, uid="lc1"),
            {"type": "system", "subtype": "compact_boundary", "uuid": "cb1", "timestamp": ts(5), "sessionId": sid,
             "compactMetadata": {"trigger": "manual", "preTokens": 9000, "postTokens": 800}},
            user(sid, "second prompt \u00fc\u4e2d\u6587", 6),
            {"type": "ai-title", "aiTitle": "AI title", "sessionId": sid},
            {"type": "custom-title", "customTitle": "My \u00fcnicode title", "sessionId": sid},
            {"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.5, "modelUsage": {"claude-sonnet-5-5": {
                "inputTokens": 4, "outputTokens": 250}}},
            assistant(sid, "msg_syn", 7, [], usage(), model="<synthetic>"),
        ]
        write_jsonl(proj / (sid + ".jsonl"), recs, crlf=True)
        # subagent transcript
        write_jsonl(proj / sid / "subagents" / "agent-abc123.jsonl", [
            user(sid, "subagent prompt must not count", 2),
            assistant(sid, "msg_S1", 2, [tool_use("Grep")], usage(out=77, cr=500), agent="abc123"),
            assistant(sid, "msg_S1", 2, [tool_use("Grep")], usage(out=77, cr=500), agent="abc123"),
        ])
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com")
        return sid

    def test_dedupe_subagents_prompts_compaction_mcp(self):
        sid = self.make_session()
        rc, out = self.run_agent("export", "--quiet", "--label", "t1")
        self.assertEqual(rc, 0, out)
        recs = self.read_export(self.latest_export())
        s = self.session_of(recs, sid)
        calls = [r for r in recs if r["rec"] == "call"]
        self.assertEqual(len(calls), 3)  # msg_A once, msg_B, subagent msg_S1 once (synthetic skipped)
        a = next(c for c in calls if c["mid"] == "msg_A")
        self.assertEqual(a["out"], 200)
        self.assertEqual(sorted(a["tools"]), ["Bash", "Read", "mcp__github__create_issue"])
        self.assertEqual(a["ctx"], 2 + 1000 + 500)
        self.assertEqual(a["cw1"], 500)
        self.assertEqual(s["prompts"], 2)  # not tool results, compact summary, local-command, subagent prompt
        self.assertEqual(s["title"], "My \u00fcnicode title")
        self.assertEqual(s["title_kind"], "custom")
        self.assertEqual(len(s["compactions"]), 1)
        self.assertEqual(s["compactions"][0]["pre"], 9000)
        self.assertEqual(s["mcp"], {"github": {"create_issue": 1}, "playwright": {"browser_click": 1}})
        self.assertEqual(s["subagents"]["abc123"]["calls"], 1)
        self.assertEqual(s["tokens"]["claude-sonnet-5-5"]["out"], 200 + 50 + 77)
        self.assertEqual(s["ctx_peak"], 3000 + 2 + 500)  # main thread only (subagent excluded)
        self.assertEqual(s["cost"]["totalCostUSD"], 1.5)
        self.assertEqual(s["project"], "proj")
        self.assertEqual(s["account_uuid"], "acct-A")
        self.assertGreater(s["active_s"], 0)

    def test_windows_path_and_worktree(self):
        self.assertEqual(ccm_agent.project_of("C:\\Users\\bob\\code\\my-app"), ("my-app", False))
        self.assertEqual(ccm_agent.project_of("/home/x/app/.claude/worktrees/feat-1"), ("app", True))
        sid = "w1"
        write_jsonl(self.cfg / "projects" / "p" / (sid + ".jsonl"), [
            user(sid, "hi", 0, cwd="C:\\Users\\bob\\code\\my-app"),
            assistant(sid, "m1", 1, [], cwd="C:\\Users\\bob\\code\\my-app")])
        self.run_agent("export", "--quiet")
        s = self.session_of(self.read_export(self.latest_export()), sid)
        self.assertEqual(s["project"], "my-app")
        self.assertEqual(s["title_kind"], "derived")  # no title record: fallback label from first prompt
        self.assertEqual(s["title"], "hi")

    def test_incremental_partial_line_and_idempotence(self):
        sid = self.make_session()
        self.run_agent("export", "--quiet")
        first = self.read_export(self.latest_export())
        n_calls = sum(1 for r in first if r["rec"] == "call")
        rc, out = self.run_agent("export", "--quiet")
        self.assertIn("no new data", out)
        path = self.cfg / "projects" / "-work-proj" / (sid + ".jsonl")
        partial = json.dumps(assistant(sid, "msg_C", 9, [], usage(out=5)), separators=(",", ":"))
        append_jsonl(path, [], partial=partial[:40])  # half-written line must not be consumed
        _, out = self.run_agent("export", "--quiet")
        self.assertIn("no new data", out)
        append_jsonl(path, [], partial=partial[40:] + "\n")
        self.run_agent("export", "--quiet")
        recs = self.read_export(self.latest_export())
        self.assertEqual([r["mid"] for r in recs if r["rec"] == "call"], ["msg_C"])
        s = self.session_of(recs, sid)
        self.assertEqual(s["calls"], n_calls + 1)  # session aggregate covers the whole archive
        # --full re-exports everything and the server dedupes
        self.run_agent("export", "--quiet", "--full")
        self.assertEqual(sum(1 for r in self.read_export(self.latest_export()) if r["rec"] == "call"), n_calls + 1)

    def test_two_config_dirs_two_accounts_and_switch(self):
        cfg2 = self.home / ".claude-work"
        write_jsonl(self.cfg / "projects" / "p" / "s-main.jsonl", [user("s-main", "x", 0), assistant("s-main", "m1", 1, [])])
        write_jsonl(cfg2 / "projects" / "p" / "s-work.jsonl", [user("s-work", "y", 0), assistant("s-work", "m2", 1, [])])
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com")
        claude_json(cfg2 / ".claude.json", "acct-B", "b@example.com")
        self.run_agent("export", "--quiet")
        recs = self.read_export(self.latest_export())
        self.assertEqual(self.session_of(recs, "s-main")["account_uuid"], "acct-A")
        self.assertEqual(self.session_of(recs, "s-work")["account_uuid"], "acct-B")
        self.assertEqual(self.session_of(recs, "s-work")["account_conf"], "assumed")  # first run
        self.assertEqual({r["account_uuid"] for r in recs if r["rec"] == "account"}, {"acct-A", "acct-B"})
        # /login switches the default dir to account C; new activity afterwards is attributed to C (ambiguous)
        claude_json(self.home / ".claude.json", "acct-C", "c@example.com")
        write_jsonl(self.cfg / "projects" / "p" / "s-new.jsonl", [user("s-new", "z", 0), assistant("s-new", "m3", 1, [])])
        self.run_agent("export", "--quiet")
        recs = self.read_export(self.latest_export())
        self.assertEqual(self.session_of(recs, "s-new")["account_uuid"], "acct-C")
        self.assertEqual(self.session_of(recs, "s-new")["account_conf"], "ambiguous")
        # old session untouched since the switch keeps its account
        self.assertFalse([r for r in recs if r["rec"] == "session" and r["id"] == "s-main"])
        log = [json.loads(l) for l in (ccm_agent.CCM_HOME / "accounts_seen.jsonl").read_text().splitlines()]
        self.assertEqual([r["account_uuid"] for r in log if r["cfg"].endswith(".claude")], ["acct-A", "acct-C"])

    def test_privacy(self):
        self.make_session()
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com", util={
            "fetchedAtMs": 1790000000000, "accountUuid": "acct-A", "utilization": {
                "five_hour": {"utilization": 54, "resets_at": "2026-10-05T12:00:00+00:00"},
                "seven_day": {"utilization": 44, "resets_at": "2026-10-10T23:59:59+00:00"},
                "seven_day_breakdown": {"as_of": "x", "window_started_at": "y",
                                        "rows": [{"key": "claude_code", "display_name": "CC", "percent": 99}]},
                "spend": {"disclaimer": "SHOULD-NOT-LEAK"}}})
        self.run_agent("export", "--quiet")
        raw = gzip.open(str(self.latest_export()), "rt", encoding="utf-8").read()
        for needle in (SECRET_PROMPT, SECRET_FILE, "SHOULD-NOT-LEAK", "second prompt", "subagent prompt"):
            self.assertNotIn(needle, raw)
        util = next(r for r in self.read_export(self.latest_export()) if r["rec"] == "util")
        self.assertEqual(util["five_hour"]["utilization"], 54)
        self.assertEqual(util["breakdown"]["rows"]["claude_code"], 99)

    def test_install_creates_slash_command(self):
        self.make_session()
        rc, out = self.run_agent("install", "--label", "box1", "--server", "http://x:1", "--token", "tok")
        self.assertEqual(rc, 0, out)
        cmd = (self.cfg / "commands" / "ccm-collect.md").read_text()
        self.assertIn("ccm_agent.py", cmd)
        self.assertTrue((ccm_agent.CCM_HOME / "ccm_agent.py").exists())
        cfg = json.loads((ccm_agent.CCM_HOME / "config.json").read_text())
        self.assertEqual((cfg["label"], cfg["servers"], cfg["token"]), ("box1", ["http://x:1"], "tok"))

    def test_rewritten_file_reparse_is_idempotent(self):
        sid = self.make_session()
        self.run_agent("export", "--quiet")
        p = self.cfg / "projects" / "-work-proj" / (sid + ".jsonl")
        data = p.read_bytes()
        p.write_bytes(data[:len(data) // 2].rsplit(b"\n", 1)[0] + b"\n")  # truncated below stored offset
        self.run_agent("export", "--quiet")
        conn = ccm_agent.open_db()
        n = conn.execute("SELECT count(*) FROM calls WHERE sid=?", (sid,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 3)  # nothing duplicated or lost from the archive


class TestServer(AgentBase):
    def setUp(self):
        super().setUp()
        self.data = self.tmp / "srv"
        ccm_server.data_dir(str(self.data))
        self.conn = ccm_server.open_db(self.data)
        write_jsonl(self.cfg / "projects" / "p" / "s1.jsonl", [
            user("s1", "x", 0), assistant("s1", "m1", 1, [tool_use("Read")]), assistant("s1", "m2", 2, [])])
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def counts(self):
        return tuple(self.conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                     for t in ("sessions", "calls", "devices", "accounts"))

    def test_import_idempotent_and_overlap(self):
        self.run_agent("export", "--quiet")
        f = self.latest_export()
        res = ccm_server.ingest_bytes(self.conn, f.read_bytes(), f.name, self.data)
        self.assertEqual((res["sessions"], res["calls"]), (1, 2))
        self.assertEqual(ccm_server.ingest_bytes(self.conn, f.read_bytes(), f.name)["status"], "duplicate")
        self.assertEqual(self.counts(), (1, 2, 1, 1))
        self.run_agent("export", "--quiet", "--full")  # overlapping export: different file, same keys
        f2 = self.latest_export()
        ccm_server.ingest_bytes(self.conn, f2.read_bytes(), f2.name)
        self.assertEqual(self.counts(), (1, 2, 1, 1))

    def start_server(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), ccm_server.Handler)
        srv.token, srv.data = "secret-token", self.data
        srv.require_token, srv.allowed_nets = False, ccm_server.parse_nets(ccm_server.DEFAULT_ALLOW)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]

    def test_push_retry_and_auth(self):
        srv, url = self.start_server()
        # server down: file stays in outbox
        dead = "http://127.0.0.1:1"
        rc, out = self.run_agent("collect", "--quiet", "--server", dead, "--token", "secret-token")
        self.assertIn("pending: 1", out)
        self.assertEqual(len(list((ccm_agent.CCM_HOME / "outbox").glob("*.gz"))), 1)
        # wrong token rejected, still pending
        rc, out = self.run_agent("collect", "--quiet", "--server", url, "--token", "nope")
        self.assertIn("pending: 1", out)
        # server reachable (second URL in list is used): delivered, moved to sent/
        rc, out = self.run_agent("collect", "--quiet", "--server", dead, "--server", url, "--token", "secret-token")
        self.assertIn("sent: 1", out)
        self.assertEqual(len(list((ccm_agent.CCM_HOME / "outbox").glob("*.gz"))), 0)
        self.assertEqual(len(list((ccm_agent.CCM_HOME / "sent").glob("*.gz"))), 1)
        conn = ccm_server.open_db(self.data)
        self.assertEqual(conn.execute("SELECT count(*) FROM calls").fetchone()[0], 2)
        conn.close()

    def test_tokenless_allowlist(self):
        srv, url = self.start_server()
        rc, out = self.run_agent("collect", "--quiet", "--server", url)  # loopback is allowed without a token
        self.assertIn("sent: 1", out)
        srv.allowed_nets = []  # now no address is allowed without credentials
        write_jsonl(self.cfg / "projects" / "p" / "s2.jsonl", [user("s2", "q", 0), assistant("s2", "mx", 1, [])])
        rc, out = self.run_agent("collect", "--quiet", "--server", url)
        self.assertIn("pending: 1", out)
        self.assertTrue(ccm_server.ip_allowed("100.101.102.103", ccm_server.parse_nets(ccm_server.DEFAULT_ALLOW)))
        self.assertFalse(ccm_server.ip_allowed("192.168.1.5", ccm_server.parse_nets(ccm_server.DEFAULT_ALLOW)))
        self.assertFalse(ccm_server.ip_allowed("8.8.8.8", ccm_server.parse_nets(ccm_server.DEFAULT_ALLOW)))

    def test_bundle_bakes_config(self):
        out = self.tmp / "bundle" / "ccm_agent.py"
        rc = ccm_server.main(["--data", str(self.data), "bundle", "--server", "http://a:1", "--server", "http://b:2",
                              "--label", "lbl", "--out", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text()
        ns = {"__name__": "baked"}
        exec(compile(text, str(out), "exec"), ns)
        self.assertEqual(ns["BAKED_CONFIG"]["servers"], ["http://a:1", "http://b:2"])
        self.assertEqual(ns["BAKED_CONFIG"]["token"], ccm_server.get_token(self.data))
        self.assertEqual(ns["BAKED_CONFIG"]["label"], "lbl")


class TestCalc(unittest.TestCase):
    def test_calibration_windows_and_session_pct(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccmcalc-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        ccm_server.data_dir(str(self.tmp))
        conn = ccm_server.open_db(self.tmp)
        # sonnet-5: in $2, out $10 per MTok. 1,000,000 output tokens = $10 per call.
        for i, (sid, minute) in enumerate([("s1", 0), ("s1", 30), ("s2", 60), ("s2", 400)]):
            e = ccm_calc.ts_epoch("2026-10-05T%02d:%02d:00Z" % (minute // 60, minute % 60))
            conn.execute("INSERT INTO calls VALUES('d1',?,'',?,?,?,'claude-sonnet-5',0,1000000,0,0,0,0,0,0,NULL,NULL,'[]')",
                         (sid, "m%d" % i, "x", e))
        conn.execute("INSERT INTO sessions(device_id,sid,account_uuid) VALUES('d1','s1','A'),('d1','s2','A')")
        # snapshot at 02:00 for window ending 05:00 (start 00:00): $30 spent -> 60% => capacity $50
        snap = {"fetched_ms": int(ccm_calc.ts_epoch("2026-10-05T02:00:00Z") * 1000), "account_uuid": "A",
                "five_hour": {"utilization": 60, "resets_at": "2026-10-05T05:00:00+00:00"}}
        conn.execute("INSERT INTO util_snapshots VALUES(?,?,?,?)", (snap["fetched_ms"], "A", "d1", json.dumps(snap)))
        res = ccm_calc.compute(conn)
        self.assertAlmostEqual(res["caps"]["A"]["cap_5h"], 50.0, places=3)
        s1, s2 = res["sessions"][("d1", "s1")], res["sessions"][("d1", "s2")]
        self.assertAlmostEqual(s1["pct5_peak"], 40.0, places=3)  # $20 of $50 in the anchored window
        self.assertAlmostEqual(s2["pct5_sum"], 40.0, places=3)  # $10 in the anchored window + $10 in a later one
        self.assertAlmostEqual(s2["pct5_peak"], 20.0, places=3)
        # override rules: project-based account override
        conn.execute("UPDATE sessions SET project='proj-b' WHERE sid='s2'")
        conn.execute("INSERT INTO account_overrides(account_uuid,project_like) VALUES('B','proj-b')")
        self.assertEqual(ccm_calc.effective_accounts(conn)[("d1", "s2")], "B")
        self.assertEqual(ccm_calc.effective_accounts(conn)[("d1", "s1")], "A")
        conn.close()

    def test_prices(self):
        p = ccm_calc.Prices([r[:6] for r in ccm_calc.DEFAULT_PRICES])
        self.assertEqual(p.get("claude-opus-5-5")[1], 20.0)
        self.assertEqual(p.get("claude-opus-5[1m]")[1], 25.0)
        self.assertEqual(p.get("claude-sonnet-5-5")[1], 10.0)
        self.assertEqual(p.get("claude-opus-4-8")[1], 25.0)  # family fallback
        self.assertEqual(p.get("something-new")[1], 15.0)


if __name__ == "__main__":
    unittest.main()
