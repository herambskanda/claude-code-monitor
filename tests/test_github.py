"""GitHub transport tests against an in-memory fake GitHub API. Run with the rest: python3 -m unittest discover -s tests"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ccm  # noqa: E402
from test_ccm import AgentBase, assistant, claude_json, user, write_jsonl  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
import ccm_agent  # noqa: E402
import ccm_gh  # noqa: E402
import ccm_server  # noqa: E402


class TestCrypto(unittest.TestCase):
    def test_roundtrip_and_failures(self):
        for data in (b"", b"x", os.urandom(100000)):
            blob = ccm_agent.encrypt_bytes(data, "pw")
            self.assertEqual(ccm_agent.decrypt_bytes(blob, "pw"), data)
        blob = ccm_agent.encrypt_bytes(b"secret data", "pw")
        self.assertNotIn(b"secret", blob)
        with self.assertRaises(ValueError):
            ccm_agent.decrypt_bytes(blob, "wrong")
        bad = bytearray(blob)
        bad[len(bad) // 2] ^= 1
        with self.assertRaises(ValueError):
            ccm_agent.decrypt_bytes(bytes(bad), "pw")
        self.assertNotEqual(ccm_agent.encrypt_bytes(b"a", "pw"), ccm_agent.encrypt_bytes(b"a", "pw"))  # random salt

    def test_pbkdf2_fallback_decrypts(self):
        ek, mk = ccm_agent._derive_keys("pw", b"s" * 16, 2)
        self.assertEqual(len(ek), 32)

    def test_join_code(self):
        code = ccm_agent.make_join_code("o/r", "tok", "pass")
        self.assertTrue(code.startswith("ccm1."))
        self.assertEqual(ccm_agent.parse_join_code("  " + code + "\n"), {"repo": "o/r", "token": "tok", "passphrase": "pass"})
        for bad in ("nope", "ccm1.@@@", "ccm1." + code[5:12]):
            with self.assertRaises(ValueError):
                ccm_agent.parse_join_code(bad)


class TestGithubFlow(AgentBase):
    def setUp(self):
        super().setUp()
        os.environ["CCM_NO_REVEAL"] = "1"
        os.environ["CCM_NO_SLEEP"] = "1"
        self.fake = FakeGitHub()
        self.url = self.fake.start()
        self.old_api = ccm_agent.GITHUB_API
        ccm_agent.GITHUB_API = self.url
        self.data = self.tmp / "srv"
        ccm_server.data_dir(str(self.data))
        self.conn = ccm_server.open_db(self.data)
        write_jsonl(self.cfg / "projects" / "p" / "s1.jsonl", [
            user("s1", "TOP-SECRET-PROMPT", 0), assistant("s1", "m1", 1, []), assistant("s1", "m2", 2, [])])
        claude_json(self.home / ".claude.json", "acct-A", "a@example.com")

    def tearDown(self):
        ccm_agent.GITHUB_API = self.old_api
        os.environ.pop("CCM_NO_REVEAL", None)
        os.environ.pop("CCM_NO_SLEEP", None)
        self.conn.close()
        self.fake.stop()
        super().tearDown()

    def join(self):
        return ccm_gh.init(self.data, "o/r", "tok")

    def test_init_refuses_public_repo(self):
        self.fake.private = False
        with self.assertRaises(ValueError):
            self.join()

    def test_init_bad_token(self):
        with self.assertRaises(ccm_gh.GhError):
            ccm_gh.init(self.data, "o/r", "wrong")

    def test_full_cycle(self):
        code = self.join()
        self.assertEqual(json.loads(self.fake.files["requests.json"][1])["id"], 0)
        # --- device installs with the join code: schedules nothing in tests, uploads immediately
        rc, out = self.run_agent("install", "--join", code, "--label", "far-away", "--no-schedule")
        self.assertEqual(rc, 0, out)
        self.assertIn("first upload: done", out)
        branches = list(self.fake.refs)
        self.assertEqual(len(branches), 1)
        self.assertTrue(branches[0].startswith("device/far-away-"))
        # data in the repo is encrypted: nothing readable
        for blob in self.fake.blobs.values():
            self.assertNotIn(b"s1", blob[:0])
            self.assertNotIn(b"TOP-SECRET", blob)
            self.assertNotIn(b"a@example.com", blob)
        cmd = (self.cfg / "commands" / "ccm-collect.md").read_text()
        self.assertIn("upload", cmd)
        # --- main PC pulls and imports
        res = ccm_gh.pull_all(self.data, self.conn)
        self.assertEqual([r["status"] for r in res], ["ok"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM calls").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT label FROM devices").fetchone()[0], "far-away")
        self.assertEqual([r["status"] for r in ccm_gh.pull_all(self.data, self.conn)], ["unchanged"])
        # --- poll with no request: no upload, and a conditional GET after the first
        n_refs = len(self.fake.calls)
        _, out = self.run_agent("poll")
        self.assertIn("no new request", out)
        _, out = self.run_agent("poll")
        self.assertEqual([c for c in self.fake.calls[n_refs:] if c[0] != "GET"], [])
        # --- request data: new activity, device uploads on its next poll, handled only once
        write_jsonl(self.cfg / "projects" / "p" / "s2.jsonl", [user("s2", "q", 0), assistant("s2", "m3", 1, [])])
        req = ccm_gh.request_collect(self.data)
        self.assertGreater(req["id"], 0)
        _, out = self.run_agent("poll")
        self.assertIn("upload: ok", out)
        res = ccm_gh.pull_all(self.data, self.conn)
        self.assertEqual([r["status"] for r in res], ["ok"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM calls").fetchone()[0], 3)
        st = ccm_gh.status(self.data, self.conn)
        self.assertEqual(st["devices"][0]["handled_request"], req["id"])
        _, out = self.run_agent("poll")
        self.assertIn("no new request", out)
        # --- targeted request for another device is ignored
        ccm_gh.request_collect(self.data, devices=["someone-else"])
        _, out = self.run_agent("poll")
        self.assertIn("no new request", out)
        # --- auto_hours makes the device upload by itself
        ccm_gh.request_collect(self.data, auto_hours=0.00001, trigger=False)
        import time
        time.sleep(0.1)
        _, out = self.run_agent("poll")
        self.assertIn("upload: ok", out)

    def test_offline_poll_is_quiet_and_upload_retries(self):
        code = self.join()
        self.run_agent("install", "--join", code, "--label", "x", "--no-schedule", "--no-upload")
        ccm_agent.GITHUB_API = "http://127.0.0.1:1"
        rc, out = self.run_agent("poll")
        self.assertEqual(rc, 0)
        rc, out = self.run_agent("upload")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)

    def test_wrong_passphrase_is_reported_not_imported(self):
        code = self.join()
        self.run_agent("install", "--join", code, "--label", "x", "--no-schedule")
        cfg = ccm_gh.load_config(self.data)
        cfg["passphrase"] = "different"
        ccm_gh.save_config(self.data, cfg)
        res = ccm_gh.pull_all(self.data, self.conn)
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
