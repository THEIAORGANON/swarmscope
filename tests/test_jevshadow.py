"""Jev shadow mode: records what Jev would decide, never changes what the rules decide."""
import http.server
import io
import json
import os
import pathlib
import shutil
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
OPT_IN = dict(CFG, include_task=True, include_paths=True, include_error_text=True)

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


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SWARMSCOPE_TASK_SUMMARY", "SWARMSCOPE_JEV_TEST_ENDPOINT", "SWARMSCOPE_JEV_SYNC"):
        monkeypatch.delenv(k, raising=False)


# --- config, endpoint, context --------------------------------------------------------------------

def test_off_unless_enabled_and_project_cannot_set_endpoint():
    assert jevshadow.config(POL) is None
    assert jevshadow.config(dict(POL, jev={"enabled": "yes"})) is None
    cfg = jevshadow.config(dict(POL, jev={"enabled": True, "threshold": 0.6,
                                          "base_url": "https://evil.example", "path": "/steal"}))
    assert cfg["threshold"] == 0.6 and "base_url" not in cfg and "path" not in cfg
    assert jevshadow.endpoint() == "https://api.typesafe.ai/v1/systemone"


def test_test_endpoint_only_loopback(monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", "https://evil.example/v1/systemone")
    assert jevshadow.endpoint() == jevshadow.ENDPOINT
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", "http://127.0.0.1:5555/v1/systemone")
    assert jevshadow.endpoint() == "http://127.0.0.1:5555/v1/systemone"


def test_key_goes_only_to_the_endpoint():
    seen = []

    def op(req, timeout):
        seen.append(req.full_url)
        return FakeResp(200, OK_BODY)
    jevshadow.ask_jev({"model": "jev-1.14.0", "state": {}, "questions": {}}, CFG, KEY, op)
    assert seen == [jevshadow.ENDPOINT]


def test_default_context_is_structured_only():
    ctx = jevshadow.build_context(EVENTS, POL, CHANGE, [], 70.0, True, CFG)
    assert ctx == jevshadow.build_context(EVENTS, POL, CHANGE, [], 70.0, True, CFG)
    ch = ctx["changes"]
    assert ch["production_files"] == 1 and ch["production_lines"] == 44 and ch["test_files"] == 1
    assert ch["critical_files"] == 1 and "paths" not in ch and "critical_paths" not in ch
    assert "task" not in ctx and "last_error" not in ctx["errors"]
    assert ctx["tests"]["last_result"] == "failed" and ctx["tests"]["ran_after_last_change"] is True
    assert ctx["errors"]["repeated_unresolved"] == 2 and ctx["session"]["edit_tool_calls"] == 1
    assert ctx["advisor"] == {"history_available": True, "consultations_so_far": 0,
                              "seconds_since_last_consultation": None, "consulted_after_first_code_change": False}
    blob = json.dumps(ctx)
    assert "supersecretvalue123" not in blob and "SECRET_CODE" not in blob and "cat >" not in blob
    assert "R1" not in blob and "R3" not in blob                       # rules' verdicts are not sent


def test_opt_in_fields_are_redacted():
    ctx = jevshadow.build_context(EVENTS, POL, CHANGE, [], 70.0, True, OPT_IN)
    assert ctx["changes"]["critical_paths"] == ["src/auth/client.py"] and "README.md" not in ctx["changes"]["paths"]
    assert ctx["task"]["source"] == "current_turn_request" and ctx["task"]["text"].startswith("Fix the retry bug")
    assert "supersecretvalue123" not in json.dumps(ctx) and ctx["errors"]["last_error"].startswith("Exit code 1")


def test_task_is_the_current_turn_not_the_first(monkeypatch):
    ev = EVENTS + [{"event": "UserPromptSubmit", "prompt": "<agent-message>[Subagent hand-back] x", "ts": 9.0},
                   {"event": "UserPromptSubmit", "prompt": "Now add logging", "ts": 10.0}]
    assert jevshadow.build_context(ev, POL, CHANGE, [], 70.0, True, OPT_IN)["task"]["text"] == "Now add logging"
    monkeypatch.setenv("SWARMSCOPE_TASK_SUMMARY", "Add retries to the HTTP client")
    t = jevshadow.build_context(ev, POL, CHANGE, [], 70.0, True, CFG)["task"]
    assert t == {"source": "runner_summary", "text": "Add retries to the HTTP client"}


def test_unknown_advisor_history_is_not_zero():
    ctx = jevshadow.build_context(EVENTS, POL, {}, [], 70.0, False, CFG, history_available=False)
    assert ctx["advisor"] == {"history_available": False} and ctx["changes"] == {"known": False}
    assert ctx["tests"]["ran_after_last_change"] is None


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
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return FakeResp(status, body)
    return op


REQ = {"model": "jev-1.14.0", "state": {"x": 1}, "questions": jevshadow.QUESTIONS}


def test_ask_ok_records_probability_cost_and_pinned_model():
    seen = []
    r = jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(200, OK_BODY, seen))
    assert r["status"] == "ok" and r["p_consult"] == 0.7 and r["would_consult"] is True
    assert r["model"] == "jev-1.14.0" and r["cost_usd_est"] == pytest.approx(661 * 0.042 / 1e6)
    assert seen[0] == REQ


@pytest.mark.parametrize("answer", [{"type": "noul", "noul": 1.7}, {"type": "noul", "noul": -0.1},
                                    {"type": "noul", "noul": float("nan")}, {"type": "noul", "noul": True},
                                    {"type": "noul", "noul": "0.7"}, {"type": "choice", "noul": 0.7}, None])
def test_invalid_answers_are_rejected(answer):
    body = json.loads(json.dumps(OK_BODY))
    body["answers"] = {"consult_advisor": answer} if answer is not None else {}
    r = jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(200, body))     # json round-trips NaN as NaN
    assert r["status"] == "invalid" and "would_consult" not in r and "p_consult" not in r


def test_model_mismatch_and_unknown_cost():
    body = dict(OK_BODY, model="jev-1.15.0")
    r = jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(200, body))
    assert r["status"] == "model_mismatch" and "would_consult" not in r
    r = jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(200, {k: v for k, v in OK_BODY.items() if k != "usage"}))
    assert r["status"] == "ok" and r["cost_usd_est"] is None


def test_failures_are_never_a_skip_and_never_leak_the_key():
    r = jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(401, {"error": f"bad key {KEY}"}))
    assert r["status"] == "error" and r["http_status"] == 401 and KEY not in json.dumps(r)
    assert "would_consult" not in r

    def boom(req, timeout):
        raise TimeoutError("timed out")
    assert jevshadow.ask_jev(REQ, CFG, KEY, boom)["status"] == "timeout"
    ref = dict(OK_BODY, answers={"consult_advisor": {"type": "refusal"}})
    assert jevshadow.ask_jev(REQ, CFG, KEY, opener_returning(200, ref))["status"] == "refusal"


# --- journal: ids, writes, missing results, replay ------------------------------------------------------

def _shadow(tmp_path, sid="s1", rule_records=()):
    return jevshadow.shadow(sid, str(tmp_path), EVENTS, dict(POL, jev={"enabled": True}), CHANGE, [], 70.0, True,
                            list(rule_records))


def test_ctx_ids_unique_under_parallel_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_JEV_SYNC", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    ids = []
    ts = [threading.Thread(target=lambda: ids.append(_shadow(tmp_path))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    recs, bad = jevshadow.load(jevshadow.jev_file(tmp_path, "s1"))
    assert bad == 0 and len(set(ids)) == 8
    assert len([r for r in recs if r["kind"] == "context"]) == 8 and len([r for r in recs if r["kind"] == "jev"]) == 8


def test_corrupt_lines_are_reported(tmp_path):
    p = jevshadow.jev_file(tmp_path, "s1")
    jevshadow._append(p, {"kind": "context", "ctx_id": "a"})
    with open(p, "a") as fh:
        fh.write('{"kind": "jev", "ctx_id"\n')
    assert jevshadow.load(p) == ([{"kind": "context", "ctx_id": "a"}], 1)


def test_worker_failures_and_missing_results(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SWARMSCOPE_DIR", str(tmp_path))

    def no_spawn(*a, **k):
        raise OSError("fork failed")
    monkeypatch.setattr(jevshadow.subprocess, "Popen", no_spawn)
    cid = _shadow(tmp_path)
    p = jevshadow.jev_file(tmp_path, "s1")
    assert [r["status"] for r in jevshadow._load(p) if r["kind"] == "jev"] == ["worker_failed"]

    monkeypatch.setattr(jevshadow.subprocess, "Popen", lambda *a, **k: None)   # worker that never writes
    cid2 = _shadow(tmp_path)
    assert jevshadow.pending(p) == [cid2]
    assert jevshadow.finalize(p, now=75.0) == []                               # still within timeout + grace
    out = jevshadow.finalize(p, now=70.0 + 10 + jevshadow.WORKER_GRACE_S)
    assert [(r["ctx_id"], r["status"]) for r in out] == [(cid2, "missing_result")]
    assert jevshadow.finalize(p, now=10 ** 10) == [] and jevshadow.pending(p) == []
    assert cid != cid2

    bad = jevshadow.jev_file(tmp_path, "s2")
    jevshadow._append(bad, {"kind": "context", "ctx_id": "x", "jev": {}})      # no request: worker crashes inside
    assert jevshadow.run_worker(str(bad), "x")["status"] == "worker_failed"


def test_replay_reuses_the_stored_request_and_never_replaces_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_JEV_SYNC", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cid = _shadow(tmp_path)
    p = jevshadow.jev_file(tmp_path, "s1")
    ctx = next(r for r in jevshadow._load(p) if r["kind"] == "context")
    assert ctx["request"]["questions"] == jevshadow.QUESTIONS and ctx["jev"]["question_version"] == "q-consult-2"
    monkeypatch.setattr(jevshadow, "QUESTIONS", {"consult_advisor": {"type": "noul", "instructions": "changed"}})
    r1 = jevshadow.run_worker(str(p), cid, replay=True)
    r2 = jevshadow.run_worker(str(p), cid, replay=True, current_question=True)
    assert r1["same_request"] is True and r2["same_request"] is False
    originals = [r for r in jevshadow._load(p) if r["kind"] == "jev" and not r.get("replay")]
    assert len(originals) == 1 and jevshadow.pending(p) == []


def test_show_separates_fired_applied_and_called(tmp_path, capsys):
    p = jevshadow.jev_file(tmp_path, "abcd1234")
    req = {"model": "jev-1.14.0", "questions": {}, "state": {
        "changes": {"production_files": 2, "production_lines": 64}, "tests": {"last_result": "passed"},
        "advisor": {"history_available": True, "consultations_so_far": 0}}}
    jevshadow._append(p, {"kind": "context", "ctx_id": "c1", "turn": 1, "jev": {"question_version": "q-consult-2"},
                          "request": req, "rules": [
                              {"id": "a-3", "rule": "R3_high_risk", "state": "deferred", "action": "consult_advisor"}]})
    jevshadow._append(p, {"kind": "jev", "ctx_id": "c1", "status": "ok", "p_consult": 0.7, "would_consult": True,
                          "threshold": 0.5, "latency_ms": 235})
    jevshadow._append(p, {"kind": "jev", "ctx_id": "c1", "status": "ok", "p_consult": 0.4, "replay": True,
                          "same_request": True})
    rules.append_log(str(tmp_path / "decisions" / "abcd1234.jsonl"),
                     [{"kind": "followup", "id": "a-3", "outcome": "not_observed"}])
    jevshadow.show("abcd1234", tmp_path)
    out = capsys.readouterr().out
    assert "R3_high_risk fired · deferred (background agents) · advisor not called" in out
    assert "jev:   p=0.70 → consult" in out and "replay (same request): p=0.40" in out


# --- hook --------------------------------------------------------------------------------------------

def test_hook_redaction_fallback_never_logs_plain_text(tmp_path):
    """hook.py with a common.py that has no redact(): the event must not keep the secret."""
    for f in ("hook.py", "rules.py", "repostate.py", "jevshadow.py"):
        shutil.copy(os.path.join(ROOT, f), tmp_path / f)
    src = open(os.path.join(ROOT, "common.py")).read().replace("def redact(", "def _gone_redact(")
    (tmp_path / "common.py").write_text(src)
    sw = tmp_path / "sw"
    payload = {"session_id": "fb", "cwd": str(tmp_path), "hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": "export TOKEN=supersecretvalue123 && run"},
               "tool_response": {"stdout": "password=hunter2hunter2"}}
    r = subprocess.run([sys.executable, str(tmp_path / "hook.py")], input=json.dumps(payload), text=True,
                       capture_output=True, env=dict(os.environ, SWARMSCOPE_DIR=str(sw)), timeout=30)
    assert r.returncode == 0
    logged = (sw / "events" / "fb.jsonl").read_text()
    assert "supersecretvalue123" not in logged and "hunter2" not in logged and "redaction unavailable" in logged


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
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone"
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
    for k in ("SWARMSCOPE_POLICY", "SWARMSCOPE_TASK_SUMMARY"):
        env.pop(k, None)
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
    other = tmp_path_factory.mktemp("repo2")
    shutil.copytree(repo, other, dirs_exist_ok=True)
    sw1, sw2 = tmp_path_factory.mktemp("sw1"), tmp_path_factory.mktemp("sw2")
    plain, _ = session(other, sw1, {"mode": "enforce"}, "plain-sess")
    shadowed, env = session(repo, sw2, {"mode": "enforce", "jev": {"enabled": True}}, "jev-sess",
                            {"SWARMSCOPE_JEV_SYNC": "1", "TYPESAFE_API_KEY": KEY,
                             "SWARMSCOPE_JEV_TEST_ENDPOINT": fake_jev})
    assert strip_ids(plain) == strip_ids(shadowed)
    assert shadowed[0]["decision"] == "block"

    recs = jevshadow._load(jevshadow.jev_file(sw2, "jev-sess"))
    ctxs = [r for r in recs if r["kind"] == "context"]
    jevs = [r for r in recs if r["kind"] == "jev"]
    assert len(ctxs) == 3 and len(jevs) == 3 and all(j["status"] == "ok" for j in jevs)
    decisions = rules.load_log(str(sw2 / "decisions" / "jev-sess.jsonl"))
    dec_ids = {r["id"] for r in decisions if r.get("kind") == "decision"}
    assert {r["id"] for c in ctxs for r in c["rules"]} == dec_ids          # linked to every rules decision
    assert ctxs[0]["jev"]["model"] == "jev-1.14.0" and ctxs[0]["jev"]["question_version"] == "q-consult-2"
    assert ctxs[0]["request"]["state"]["changes"]["production_lines"] == 60
    assert ctxs[0]["request"]["state"]["advisor"] == {"history_available": False}   # no transcript in this test

    sent = FakeJev.bodies
    assert len(sent) == 3 and sent[0]["auth"] == f"Bearer {KEY}"
    assert [s["body"] for s in sent] == [c["request"] for c in ctxs]       # what was sent == what was stored
    assert "x = 1" not in json.dumps(sent) and "R3" not in json.dumps([s["body"] for s in sent])
    for p in pathlib.Path(sw2).rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="replace"), p
    over = rules.load_log(str(sw2 / "overhead" / "jev-sess.jsonl"))
    assert any("jev_shadow_ms" in o for o in over)


def test_hook_project_config_cannot_redirect_the_key(repo, tmp_path_factory, fake_jev):
    sw = tmp_path_factory.mktemp("sw")
    session(repo, sw, {"mode": "observe", "jev": {"enabled": True, "base_url": fake_jev.rsplit("/v1", 1)[0],
                                                   "timeout_s": 1}},
            "redir-sess", {"SWARMSCOPE_JEV_SYNC": "1", "TYPESAFE_API_KEY": KEY})
    assert FakeJev.bodies == []                                             # nothing reached the "attacker"
    ctxs = [r for r in jevshadow._load(jevshadow.jev_file(sw, "redir-sess")) if r["kind"] == "context"]
    assert all(c["jev"]["endpoint"] == jevshadow.ENDPOINT for c in ctxs)


def test_hook_unreachable_jev_still_fails_open(repo, tmp_path_factory):
    sw = tmp_path_factory.mktemp("sw")
    outs, _ = session(repo, sw, {"mode": "enforce", "jev": {"enabled": True, "timeout_s": 2}}, "dead-sess",
                      {"SWARMSCOPE_JEV_SYNC": "1", "TYPESAFE_API_KEY": KEY,
                       "SWARMSCOPE_JEV_TEST_ENDPOINT": "http://127.0.0.1:9/v1/systemone"})
    assert outs[0]["decision"] == "block" and outs[2] is None
    jevs = [r for r in jevshadow._load(jevshadow.jev_file(sw, "dead-sess")) if r["kind"] == "jev"]
    assert len(jevs) == 3 and all(j["status"] in ("error", "timeout") and "would_consult" not in j for j in jevs)


def test_hook_detached_worker_writes_answer(repo, tmp_path_factory, fake_jev):
    sw = tmp_path_factory.mktemp("sw")
    session(repo, sw, {"mode": "observe", "jev": {"enabled": True}}, "bg-sess",
            {"TYPESAFE_API_KEY": KEY, "SWARMSCOPE_JEV_TEST_ENDPOINT": fake_jev})
    path = jevshadow.jev_file(sw, "bg-sess")
    for _ in range(200):
        if not jevshadow.pending(path):
            break
        time.sleep(0.05)
    jevs = [r for r in jevshadow._load(path) if r["kind"] == "jev"]
    assert len(jevs) == 3 and all(j["status"] == "ok" for j in jevs)
