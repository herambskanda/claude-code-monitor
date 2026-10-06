"""Tiny in-memory fake of the GitHub REST endpoints CCM uses (tests only)."""
import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitHub:
    def __init__(self, token="tok", repo="o/r", private=True):
        self.token, self.repo, self.private = token, repo, private
        self.files, self.blobs, self.trees, self.commits, self.refs = {}, {}, {}, {}, {}
        self.calls = []
        self.srv = None

    def sha(self, data):
        return hashlib.sha1(data if isinstance(data, bytes) else data.encode()).hexdigest()

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None, headers=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                path = self.path.split("?")[0]
                fake.calls.append((method, path))
                if self.headers.get("Authorization") != "Bearer " + fake.token:
                    return self.send(401, {"message": "Bad credentials"})
                pre = "/repos/%s" % fake.repo
                if not path.startswith(pre):
                    return self.send(404, {"message": "Not Found"})
                rest = path[len(pre):]
                if rest == "" and method == "GET":
                    return self.send(200, {"full_name": fake.repo, "private": fake.private})
                m = re.match(r"/contents/(.+)$", rest)
                if m:
                    name = m.group(1)
                    if method == "GET":
                        if name not in fake.files:
                            return self.send(404, {"message": "Not Found"})
                        sha, data = fake.files[name]
                        if self.headers.get("If-None-Match") == '"%s"' % sha:
                            return self.send(304)
                        return self.send(200, {"sha": sha, "encoding": "base64",
                                               "content": base64.b64encode(data).decode()}, {"ETag": '"%s"' % sha})
                    if method == "PUT":
                        old = fake.files.get(name)
                        if old and body.get("sha") != old[0]:
                            return self.send(409, {"message": "sha mismatch"})
                        data = base64.b64decode(body["content"])
                        fake.files[name] = (fake.sha(data), data)
                        return self.send(200, {"content": {"sha": fake.files[name][0]}})
                if rest == "/git/blobs" and method == "POST":
                    data = base64.b64decode(body["content"]) if body["encoding"] == "base64" else body["content"].encode()
                    s = fake.sha(data)
                    fake.blobs[s] = data
                    return self.send(201, {"sha": s})
                if rest == "/git/trees" and method == "POST":
                    s = fake.sha(json.dumps(body["tree"], sort_keys=True))
                    fake.trees[s] = body["tree"]
                    return self.send(201, {"sha": s})
                if rest == "/git/commits" and method == "POST":
                    s = fake.sha(json.dumps(body, sort_keys=True) + str(len(fake.commits)))
                    fake.commits[s] = {"tree": {"sha": body["tree"]}, "committer": {"date": "2026-10-06T10:00:00Z"},
                                       "parents": body["parents"]}
                    return self.send(201, {"sha": s})
                m = re.match(r"/git/refs/heads/(.+)$", rest)
                if m and method == "PATCH":
                    if m.group(1) not in fake.refs:
                        return self.send(404, {"message": "Reference does not exist"})
                    fake.refs[m.group(1)] = body["sha"]
                    return self.send(200, {})
                if rest == "/git/refs" and method == "POST":
                    fake.refs[body["ref"][len("refs/heads/"):]] = body["sha"]
                    return self.send(201, {})
                if rest == "/branches" and method == "GET":
                    return self.send(200, [{"name": k, "commit": {"sha": v}} for k, v in sorted(fake.refs.items())])
                m = re.match(r"/git/commits/(.+)$", rest)
                if m and method == "GET":
                    return self.send(200, fake.commits[m.group(1)])
                m = re.match(r"/git/trees/(.+)$", rest)
                if m and method == "GET":
                    return self.send(200, {"tree": fake.trees[m.group(1)]})
                m = re.match(r"/git/blobs/(.+)$", rest)
                if m and method == "GET":
                    return self.send(200, {"encoding": "base64", "content": base64.b64encode(
                        fake.blobs[m.group(1)]).decode()})
                return self.send(404, {"message": "unhandled %s %s" % (method, rest)})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PUT(self):
                self.handle_any("PUT")

            def do_PATCH(self):
                self.handle_any("PATCH")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
