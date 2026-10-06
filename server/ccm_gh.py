"""GitHub transport, main-PC side. Devices push encrypted snapshots to branches `device/<label>-<id>` of a PRIVATE
data repo; this module requests fresh data (requests.json) and pulls + decrypts + imports the snapshots.

Config lives in <data>/github.json (chmod 600): {"repo": "owner/name", "token": "...", "passphrase": "..."}.
All crypto and the GitHub client are shared with the agent (agent/ccm_agent.py), so both sides always agree.
"""
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import ccm_agent as agent  # noqa: E402

GhError = agent.GhError
REQUESTS_PATH = "requests.json"


def config_path(d):
    return Path(d) / "github.json"


def load_config(d):
    p = config_path(d)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if cfg.get("repo") and cfg.get("token") and cfg.get("passphrase") else None


def save_config(d, cfg):
    p = config_path(d)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass


def _get_requests(cfg):
    """Returns (request_dict, file_sha) or ({}, None) when absent."""
    try:
        _, body, _ = agent.gh_api("GET", "/repos/%s/contents/%s" % (cfg["repo"], REQUESTS_PATH), cfg["token"])
    except GhError as e:
        if e.status == 404:
            return {}, None
        raise
    return json.loads(base64.b64decode(body["content"]).decode("utf-8")), body["sha"]


def _put_requests(cfg, req, sha):
    body = {"message": "ccm request %s" % req.get("id"),
            "content": base64.b64encode(json.dumps(req, indent=1).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    agent.gh_api("PUT", "/repos/%s/contents/%s" % (cfg["repo"], REQUESTS_PATH), cfg["token"], body)


def init(d, repo, token, passphrase=None):
    """Validate access, create requests.json if missing, store config. Returns the join code."""
    cfg = {"repo": repo, "token": token, "passphrase": passphrase or secrets.token_urlsafe(18)}
    old = load_config(d)
    if old and old["repo"] == repo and not passphrase:
        cfg["passphrase"] = old["passphrase"]  # keep the existing passphrase so devices stay valid
    info = agent.gh_api("GET", "/repos/%s" % repo, token)[1]
    if info.get("private") is False:
        raise ValueError("repository %s is PUBLIC; the data repo must be private" % repo)
    req, sha = _get_requests(cfg)
    if not req:
        _put_requests(cfg, {"id": 0, "devices": "*", "auto_hours": 0, "requested_at": None}, sha)
    save_config(d, cfg)
    return agent.make_join_code(repo, token, cfg["passphrase"])


def join_code(d):
    cfg = load_config(d)
    return agent.make_join_code(cfg["repo"], cfg["token"], cfg["passphrase"]) if cfg else None


def request_collect(d, devices=None, auto_hours=None, trigger=True):
    cfg = load_config(d)
    if not cfg:
        raise ValueError("GitHub transport is not configured (run: ccm_server.py github-init)")
    req, sha = _get_requests(cfg)
    if trigger:
        req["id"] = max(int(time.time()), int(req.get("id", 0)) + 1)
        req["requested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        req["devices"] = devices or "*"
    if auto_hours is not None:
        req["auto_hours"] = auto_hours
    _put_requests(cfg, req, sha)
    return req


SCHEMA = """CREATE TABLE IF NOT EXISTS gh_pulled(
  branch TEXT PRIMARY KEY, sha TEXT, pulled_at TEXT, label TEXT, device_id TEXT, uploaded_at TEXT,
  handled_request INTEGER, os TEXT, bytes INTEGER)"""


def _branches(cfg):
    out, page = [], 1
    while True:
        _, items, _ = agent.gh_api("GET", "/repos/%s/branches?per_page=100&page=%d" % (cfg["repo"], page),
                                   cfg["token"])
        out += items
        if len(items) < 100:
            return out
        page += 1


def pull_all(d, conn):
    """Import every device branch whose commit changed since the last pull. Returns a list of result dicts."""
    import ccm_server  # lazy: avoid circular import at module load
    cfg = load_config(d)
    if not cfg:
        raise ValueError("GitHub transport is not configured")
    conn.execute(SCHEMA)
    results = []
    for b in _branches(cfg):
        name = b["name"]
        if not name.startswith("device/"):
            continue
        sha = b["commit"]["sha"]
        row = conn.execute("SELECT sha FROM gh_pulled WHERE branch=?", (name,)).fetchone()
        if row and row[0] == sha:
            results.append({"branch": name, "status": "unchanged"})
            continue
        try:
            tree_sha = agent.gh_api("GET", "/repos/%s/git/commits/%s" % (cfg["repo"], sha), cfg["token"])[1]["tree"]["sha"]
            entries = {e["path"]: e["sha"] for e in agent.gh_api(
                "GET", "/repos/%s/git/trees/%s" % (cfg["repo"], tree_sha), cfg["token"])[1]["tree"]}

            def blob(path):
                x = agent.gh_api("GET", "/repos/%s/git/blobs/%s" % (cfg["repo"], entries[path]), cfg["token"])[1]
                return base64.b64decode(x["content"]) if x.get("encoding") == "base64" else x["content"].encode()

            meta = json.loads(blob("meta.json").decode("utf-8")) if "meta.json" in entries else {}
            plain = agent.decrypt_bytes(blob("data.ccm"), cfg["passphrase"])
            res = ccm_server.ingest_bytes(conn, plain, "%s.ndjson.gz" % name.replace("/", "_"), d)
        except (GhError, KeyError, ValueError, OSError) as e:
            results.append({"branch": name, "status": "error", "error": str(e)})
            continue
        with conn:
            conn.execute("INSERT OR REPLACE INTO gh_pulled VALUES(?,?,?,?,?,?,?,?,?)", (
                name, sha, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), meta.get("label"),
                meta.get("device_id"), meta.get("uploaded_at"), meta.get("handled_request"), meta.get("os"),
                meta.get("bytes")))
        results.append({"branch": name, "status": res.get("status"), "label": meta.get("label"),
                        "sessions": res.get("sessions"), "calls": res.get("calls")})
    return results


def status(d, conn):
    cfg = load_config(d)
    if not cfg:
        return {"configured": False}
    conn.execute(SCHEMA)
    out = {"configured": True, "repo": cfg["repo"], "last_request": None, "devices": []}
    try:
        req, _ = _get_requests(cfg)
        out["last_request"] = req
    except (GhError, OSError) as e:
        out["error"] = str(e)
    for r in conn.execute("SELECT branch,label,device_id,uploaded_at,handled_request,os,bytes,pulled_at "
                          "FROM gh_pulled ORDER BY label"):
        out["devices"].append(dict(zip(("branch", "label", "device_id", "uploaded_at", "handled_request", "os",
                                        "bytes", "pulled_at"), r)))
    return out
