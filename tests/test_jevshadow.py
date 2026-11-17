"""Jev shadow mode: records what Jev would decide, never changes what the rules decide."""
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jevshadow  # noqa: E402
import rules  # noqa: E402

KEY = "apikey_TESTKEY1234567890abcdef_should_never_be_logged"
POL = json.loads(json.dumps(rules.DEFAULTS))
CFG = dict(jevshadow.DEFAULTS, enabled=True)

OK_BODY = {"model": "jev-1.14.0", "answers": {"consult_advisor": {"type": "noul", "noul": 0.7}},
           "usage": {"input_tokens": 661, "output_tokens": 77}}


def files(*specs):
    return [{"path": p, "added": a, "deleted": d, "binary": False} for p, a, d in specs]


EVENTS = [
    {"event": "UserPromptSubmit", "prompt": "Fix the retry bug. token=supersecretvalue123", "ts": 1.0},
    {"event": "PostToolUse", "tool_name": "Edit", "arg": "src/auth/client.py", "ts": 5.0},
    {"event": "PostToolUseFailure", "tool_name": "Bash", "cmd": "pytest -q", "err": "Exit code 1 boom", "ts": 6.0},
    {"event": "PostToolUseFailure", "tool_name": "Bash", "cmd": "pytest -q", "err": "Exit code 1 boom", "ts": 7.0},
    {"event": "PostToolUse", "tool_name": "Bash", "cmd": "cat > src/x.py <<'EOF'\nSECRET_CODE\nEOF", "ts": 8.0},
]
CHANGE = {"files": files(("src/auth/client.py", 40, 4), ("tests/test_client.py", 20, 0), ("README.md", 5, 0)),
          "src_times": [5.0], "prod_times": [5.0], "since_advisor": None}


# --- config & context -----------------------------------------------------------------------------

def test_off_unless_enabled():
    assert jevshadow.config(POL) is None
    assert jevshadow.config(dict(POL, jev={"enabled": False})) is None
    cfg = jevshadow.config(dict(POL, jev={"enabled": True, "threshold": 0.6, "bogus": 1}))
    assert cfg["threshold"] == 0.6 and cfg["model"] == "jev-1.14.0" and "bogus" not in cfg


def test_context_features_minimal_and_deterministic():
    ctx = jevshadow.build_context(EVENTS, POL, CHANGE, [], 70.0, True, CFG)
    assert ctx == jevshadow.build_context(EVENTS, POL, CHANGE, [], 70.0, True, CFG)
    ch = ctx["changes"]
    assert ch["production_files"] == 1 and ch["production_lines"] == 44 and ch["test_files"] == 1
    assert ch["critical_paths"] == ["src/auth/client.py"] and "README.md" not in ch["paths"]
    assert ctx["tests"]["last_result"] == "failed" and ctx["tests"]["ran_after_last_change"] is True
    assert ctx["errors"]["repeated_unresolved"] == 2 and ctx["errors"]["tool_failures"] == 2
    assert ctx["advisor"] == {"reviews_so_far": 0, "seconds_since_last_review": None,
                              "reviewed_after_first_code_change": False}
    assert ctx["session"]["elapsed_s"] == 69
    blob = json.dumps(ctx)
    assert "supersecretvalue123" not in blob and "SECRET_CODE" not in blob and "cat >" not in blob
    assert "R1" not in blob and "R3" not in blob                       # rules' verdicts are not sent
    assert ctx["task"].startswith("Fix the retry bug")


def test_context_without_git_and_without_task():
    ctx = jevshadow.build_context(EVENTS, POL, {}, [3.0], 70.0, False, dict(CFG, include_task_goal=False))
    assert ctx["changes"] == {"known": False} and "task" not in ctx
    assert ctx["tests"]["ran_after_last_change"] is None and ctx["advisor"]["reviews_so_far"] == 1


# --- the call -------------------------------------------------------------------------------------

class FakeResp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(status, body, seen=None):
    def op(req, timeout):
        if seen is not None:
            seen.append(json.loads(req.data))
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(
                json.dumps(body).encode()))
        return FakeResp(status, body)
    return op


def test_ask_ok_records_probability_cost_and_pinned_model():
    seen = []
    r = jevshadow.ask_jev({"x": 1}, CFG, KEY, opener_returning(200, OK_BODY, seen))
    assert r["status"] == "ok" and r["p_consult"] == 0.7 and r["would_consult"] is True
    assert r["model"] == "jev-1.14.0" and r["cost_usd_est"] == pytest.approx(661 * 0.042 / 1e6)
    assert seen[0]["model"] == "jev-1.14.0" and list(seen[0]["questions"]) == ["consult_advisor"]


def test_ask_failures_are_never_a_skip_and_never_leak_the_key():
    r = jevshadow.ask_jev({}, CFG, KEY, opener_returning(401, {"error": f"bad key {KEY}"}))
    assert r["status"] == "error" and r["http_status"] == 401 and KEY not in json.dumps(r)
    assert "would_consult" not in r

    def boom(req, timeout):
        raise TimeoutError("timed out")
    assert jevshadow.ask_jev({}, CFG, KEY, boom)["status"] == "timeout"

    ref = dict(OK_BODY, answers={"consult_advisor": {"type": "refusal"}})
    assert jevshadow.ask_jev({}, CFG, KEY, opener_returning(200, ref))["status"] == "refusal"
    assert jevshadow.ask_jev({}, CFG, KEY, opener_returning(200, dict(OK_BODY, answers={})))["status"] == "error"


def test_worker_without_key_records_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("SWARMSCOPE_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    path = jevshadow.jev_file(tmp_path, "s1")
    jevshadow._append(path, {"kind": "context", "ctx_id": "s1-c1", "features": {}, "jev": {}})
    assert jevshadow.run_worker(str(path), "s1-c1")["status"] == "no_key"


# --- end to end through hook.py ---------------------------------------------------------------------

class FakeJev(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        FakeJev.bodies.append({"auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(n))})
        out = json.dumps(OK_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_jev():
    FakeJev.bodies = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeJev)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("a\n")
    git("add", ".")
    git("commit", "-qm", "init")
    (tmp_path / "tests").mkdir()
    return tmp_path


def hook(payload, env):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    return json.loads(r.stdout) if r.stdout.strip() else None


def session(root, sw, policy, sid, extra_env=None):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "swarmscope-policy.json").write_text(json.dumps(policy))
    env = dict(os.environ, SWARMSCOPE_DIR=str(sw), **(extra_env or {}))
    env.pop("SWARMSCOPE_POLICY", None)
    common = {"session_id": sid, "cwd": str(root)}
    hook(dict(common, hook_event_name="UserPromptSubmit", prompt="add retries"), env)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "big.py").write_text("x = 1\n" * 60)
    hook(dict(common, hook_event_name="PostToolUse", tool_name="Bash",
              tool_input={"command": "python gen.py"}, tool_response={"stdout": "ok"}), env)
    outs = [hook(dict(common, hook_event_name="Stop", stop_hook_active=i > 0), env) for i in range(3)]
    return outs, env


def strip_ids(outs):
    return json.dumps(outs).replace("jev-sess", "x").replace("plain-se", "x")


def test_hook_shadow_never_changes_the_gate(repo, tmp_path_factory, fake_jev):
    import shutil
    other = tmp_path_factory.mktemp("repo2")
    shutil.copytree(repo, other, dirs_exist_ok=True)
    sw1, sw2 = tmp_path_factory.mktemp("sw1"), tmp_path_factory.mktemp("sw2")
    plain, _ = session(other, sw1, {"mode": "enforce"}, "plain-sess")
    shadowed, env = session(repo, sw2, {"mode": "enforce", "jev": {"enabled": True, "base_url": fake_jev}},
                            "jev-sess", {"SWARMSCOPE_JEV_SYNC": "1", "TYPESAFE_API_KEY": KEY})
    assert strip_ids(plain) == strip_ids(shadowed)
    assert shadowed[0]["decision"] == "block"

    recs = jevshadow._load(jevshadow.jev_file(sw2, "jev-sess"))
    ctxs = [r for r in recs if r["kind"] == "context"]
    jevs = [r for r in recs if r["kind"] == "jev"]
    assert len(ctxs) == 3 and len(jevs) == 3 and all(j["status"] == "ok" for j in jevs)
    decisions = rules.load_log(str(sw2 / "decisions" / "jev-sess.jsonl"))
    dec_ids = {r["id"] for r in decisions if r.get("kind") == "decision"}
    assert {r["id"] for c in ctxs for r in c["rules"]} == dec_ids          # linked to every rules decision
    assert ctxs[0]["jev"]["model"] == "jev-1.14.0" and ctxs[0]["jev"]["question_version"] == "q-consult-1"
    assert ctxs[0]["features"]["changes"]["production_lines"] == 60

    sent = FakeJev.bodies
    assert len(sent) == 3 and sent[0]["auth"] == f"Bearer {KEY}"
    assert "x = 1" not in json.dumps(sent) and "R3" not in json.dumps([s["body"] for s in sent])
    for p in pathlib.Path(sw2).rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="replace"), p
    over = rules.load_log(str(sw2 / "overhead" / "jev-sess.jsonl"))
    assert any("jev_shadow_ms" in o for o in over)


def test_hook_shadow_unreachable_jev_still_fails_open(repo, tmp_path_factory):
    sw = tmp_path_factory.mktemp("sw")
    outs, _ = session(repo, sw, {"mode": "enforce", "jev": {"enabled": True, "base_url": "http://127.0.0.1:9",
                                                             "timeout_s": 2}},
                      "dead-sess", {"SWARMSCOPE_JEV_SYNC": "1", "TYPESAFE_API_KEY": KEY})
    assert outs[0]["decision"] == "block" and outs[2] is None
    jevs = [r for r in jevshadow._load(jevshadow.jev_file(sw, "dead-sess")) if r["kind"] == "jev"]
    assert jevs and all(j["status"] in ("error", "timeout") and "would_consult" not in j for j in jevs)


def test_hook_detached_worker_writes_answer(repo, tmp_path_factory, fake_jev):
    sw = tmp_path_factory.mktemp("sw")
    t0 = time.time()
    session(repo, sw, {"mode": "observe", "jev": {"enabled": True, "base_url": fake_jev}}, "bg-sess",
            {"TYPESAFE_API_KEY": KEY})
    path = jevshadow.jev_file(sw, "bg-sess")
    for _ in range(100):
        if sum(1 for r in jevshadow._load(path) if r["kind"] == "jev") == 3:
            break
        time.sleep(0.05)
    jevs = [r for r in jevshadow._load(path) if r["kind"] == "jev"]
    assert len(jevs) == 3 and all(j["status"] == "ok" for j in jevs)
    assert time.time() - t0 < 20
