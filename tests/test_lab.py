"""Lab runner end to end with a scripted fake Claude Code that calls the real hooks and a local Jev stub
(no network, no model). Each test pins one failure that would make a run look valid when it is not."""
import argparse
import http.server
import importlib.util
from pathlib import Path
import json
import shlex
import re
import time
import os
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lab"))
import lab  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")


def fake_launcher(tmp_path):
    """The fake claude, started through the test's own interpreter: the fixture's executable bit is not relied on
    (a copy of the repo written with mode 0644 made 45 tests fail with PermissionError on the lab Mac, 18.11)."""
    launcher = tmp_path / "bin" / "claude"
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    launcher.chmod(0o755)
    return str(launcher)
KEY = "apikey_LABTEST_never_in_the_agent_env"


@pytest.fixture(autouse=True)
def _no_seatbelt_on_linux(monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_LAB_UNSANDBOXED_TESTS", "1")        # tests that need the opposite unset it


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=dict(os.environ, **lab.GIT_ENV))


@pytest.fixture
def jev_stub(monkeypatch):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
            out = json.dumps({"model": body["model"], "answers": {"consult_advisor": {"type": "noul", "noul": float(os.environ.get("JEV_STUB_P", "0.3"))}},
                              "usage": {"input_tokens": 600, "output_tokens": 20}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone")
    yield seen
    srv.shutdown()


@pytest.fixture
def task(tmp_path, monkeypatch, jev_stub):
    src = tmp_path / "src1"
    (src / "src" / "inventory").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / ".claude").mkdir()
    (src / "src" / "inventory" / "__init__.py").write_text("from .stock import Inventory\n")
    (src / "src" / "inventory" / "stock.py").write_text(
        "class Inventory:\n    def __init__(self):\n        self._s = {}\n\n    def available(self, k):\n"
        "        return self._s.get(k, 0)\n")
    (src / "tests" / "__init__.py").write_text("")
    (src / "tests" / "test_stock.py").write_text(
        "import unittest\nfrom inventory import Inventory\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(Inventory().available('a'), 0)\n")
    (src / ".claude" / "swarmscope-policy.json").write_text('{"mode": "enforce"}')
    (src / "PROMPT.txt").write_text("consult the advisor")
    (src / ".gitignore").write_text("__pycache__/\n")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "base")
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text(
        "import unittest\nfrom inventory import Inventory\nclass H(unittest.TestCase):\n"
        "    def test_h(self):\n        self.assertEqual(Inventory().available('zzz-hidden-marker'), 0)\nunittest.main()\n")
    t = {"task_id": "t1", "source": {"repo": str(src), "commit": "HEAD", "exclude": [".claude", "PROMPT.txt"]},
         "prompt": "Fix it.", "task_summary": "Fix the inventory; done when unit tests pass.",
         "public_tests": "PYTHONPATH=src python3 -m unittest discover -s tests -t .",
         "hidden_tests": {"dir": str(hidden), "cmd": "PYTHONPATH=src python3 {hidden}/test_hidden.py",
                          "expected_tests": 1}}
    p = tmp_path / "task.json"
    p.write_text(json.dumps(t))
    monkeypatch.setenv("SWARMSCOPE_LAB", str(tmp_path / "home" / "lab"))
    monkeypatch.setenv("SWARMSCOPE_WORK_ROOT", str(tmp_path / "w"))
    monkeypatch.setenv("SWARMSCOPE_CLAUDE_BIN", fake_launcher(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setenv("SWARMSCOPE_LAB_UNSANDBOXED_TESTS", "1")        # no Seatbelt on Linux
    monkeypatch.setenv("SWARMSCOPE_LAB_PASS_ENV", "FAKE_SKILLS,FAKE_NO_STOP,FAKE_SLEEP,FAKE_MODEL,FAKE_LEAK,FAKE_NET,FAKE_SKIP,FAKE_EVIL,FAKE_DENY,FAKE_RATE_LIMIT,FAKE_VOLUNTARY_ADVISOR,FAKE_PROFILE_ADVISOR,FAKE_WEB,FAKE_FLAKY_FILE,FAKE_SUSPEND,FAKE_ADVISOR_NO_RESULT,FAKE_ADVISOR_HANDOFF,FAKE_TAIL_AFTER_RESULT,FAKE_TAIL_SUB,FAKE_TAIL_SUB_MODEL,FAKE_HOOK_KILLS,FAKE_READ_TRANSCRIPT,FAKE_READ_OUTSIDE,FAKE_SPILL_READ,FAKE_NO_TOOL,FAKE_ABSENT_TOOLS,FAKE_EXTRA_TOOLS,FAKE_GUARDED_READ,FAKE_GUARDED_LOG,FAKE_TASK_READ,FAKE_NO_REPLACE")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    # Claude Code's per-uid temp folder (background task outputs): a test's own, never the machine's /tmp/claude-<uid>
    ctmp = tmp_path / "ctmp" / "claude-test"
    monkeypatch.setattr(lab, "claude_tmp_root", lambda env=None: str(ctmp))
    lab.cmd_setup(None)
    return lab.load_task(str(p))


def modes_with(monkeypatch, **common):
    m, sha = lab.load_modes()
    m = json.loads(json.dumps(m))
    m["common"].update(common)
    monkeypatch.setattr(lab, "load_modes", lambda: (m, sha))


# --- A and B end to end -------------------------------------------------------------------------------

def test_a_and_b_valid_same_base_same_infrastructure(task, jev_stub):
    a = lab.attempt(task, "A")
    b = lab.attempt(task, "B")
    assert a["integrity"] == [] and b["integrity"] == [], (a["integrity"], b["integrity"])
    assert a["valid"] and b["valid"] and a["success"] and b["success"]
    assert a["base"]["work_tree"] == b["base"]["work_tree"]
    assert a["base"]["excluded"] == [".claude", ".claude/swarmscope-policy.json", "PROMPT.txt"]
    assert a["decisions"]["by_rule"]["R3_high_risk"] == {"triggered": 1} and a["advisor"]["called"] == 0
    assert b["decisions"]["by_rule"]["R3_high_risk"] == {"requested": 1, "pass": 1}
    adv = b["advisor"]
    assert (adv["requested"], adv["requested_answered"], adv["called"], adv["returned"], adv["voluntary"]) == (1, 1, 1, 1, 0)
    assert adv["blocks"][0]["outcome"] == "returned" and adv["blocks"][0]["rules"] == ["R3_high_risk"]
    for r in (a, b):
        assert r["jev"]["statuses"] == {"ok": r["decisions"]["stops"]} and r["jev"]["ok_share"] == 1.0
        assert r["jev"]["task_source"] == "runner_summary"
        assert all(x["stop_ts"] is not None for x in r["jev"]["answers"])
        assert r["decisions"]["policy_sha256_seen"] == [r["config"]["policy_sha256"]]
        assert r["tests"]["hidden"]["tests"] == 1 and r["tests"]["hidden"]["ok"]
        assert "TYPESAFE_API_KEY" not in r["config"]["env_passed"]
        assert not (lab.lab_root() / "runs" / r["run_id"] / "sw" / "typesafe_key").exists()
        assert not (lab.work_root(lab.load_modes()[0]) / r["run_id"]).exists()        # working copy removed
    assert {s["auth"] for s in jev_stub} == {f"Bearer {KEY}"}


def test_settings_sandbox_and_deny_rules(task):
    r = lab.attempt(task, "A")
    s = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())
    sb = s["sandbox"]
    assert sb["enabled"] and sb["allowUnsandboxedCommands"] is False
    assert sb["network"]["allowedDomains"] == [lab.NO_NETWORK]          # [] left the network open
    assert "~/" in sb["filesystem"]["denyRead"]
    for p in (str(lab.lab_root().resolve()), lab.expand(task["hidden_tests"]["dir"]), lab.expand(task["source"]["repo"])):
        assert p in sb["filesystem"]["denyRead"]
        assert f"Edit(/{p}/**)" in s["permissions"]["deny"]
    for p in (lab.expand(task["hidden_tests"]["dir"]), lab.expand(task["source"]["repo"])):
        assert f"Read(/{p}/**)" in s["permissions"]["deny"]
    assert "Edit(~/**)" in s["permissions"]["deny"]
    assert s["permissions"]["allow"] == ["Bash"]          # interpreters must run without an approval nobody can give


def _rule_re(rule, tool):
    """A Read/Edit path rule as a regex over absolute paths (gitignore-style: ** any depth, * one level, \\x)."""
    m = re.fullmatch(tool + r"\((.*)\)", rule)
    if not m:
        return None
    pat = m.group(1)
    pat = str(Path.home()) + pat[1:] if pat.startswith("~/") else pat[1:] if pat.startswith("//") else pat
    out, i = "", 0
    while i < len(pat):
        if pat[i] == "\\":
            out += re.escape(pat[i + 1]); i += 2
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out += "(/.*)?"; i += 3
        elif pat.startswith("**", i):
            out += ".*"; i += 2
        elif pat[i] == "*":
            out += "[^/]*"; i += 1
        elif pat[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pat[i]); i += 1
    return re.compile(out + "$")


def _denied(rules, path, tool):
    return any(r and r.match(str(path)) for r in (_rule_re(x, tool) for x in rules))


def test_read_denies_leave_open_only_the_sessions_own_spilled_tool_outputs(task):
    # review 18.11: the agent may Read the tool output Claude Code spilled for it (run d985a62293 was invalid
    # for it); other runs' outputs, transcripts, the config and everything else in home stay denied, writes too
    home, cfg = Path.home().resolve(), (lab.lab_root() / "config").resolve()
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "id_x").write_text("k")
    (home / "Downloads" / "x").mkdir(parents=True, exist_ok=True)
    other = cfg / "projects" / "-private-tmp-swarmscope-w-old" / "s1" / "tool-results"
    other.mkdir(parents=True, exist_ok=True)
    r = lab.attempt(task, "A")
    deny = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())["permissions"]["deny"]
    own = cfg / "projects" / "-private-tmp-swarmscope-w-new" / "sess"
    assert not _denied(deny, own / "tool-results" / "b3euojfv4.txt", "Read")
    for p in (own / "tool-results" / "b3euojfv4.txt", own.parent / "sess.jsonl"):
        assert _denied(deny, p, "Edit")
    for p in (own.parent / "sess.jsonl", own / "subagents" / "agent-1.jsonl", own.parent / "memory" / "m.md",
              other / "a.txt", cfg / "settings.json", cfg / ".claude.json", cfg / ".credentials.json",
              cfg / "agents" / "docs.md", lab.lab_root().resolve() / "runs" / r["run_id"] / "run.json",
              home / ".ssh" / "id_x", home / "Downloads" / "x" / "y", lab.expand(task["hidden_tests"]["dir"]) + "/t.py"):
        assert _denied(deny, p, "Read"), p
    # a name with glob characters is matched literally
    odd = home / "we[i]rd*name"
    odd.mkdir()
    r = lab.attempt(task, "A")
    deny = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())["permissions"]["deny"]
    assert _denied(deny, odd / "f", "Read") and not _denied(deny, home / "weXrdyname" / "f", "Read")


def _guarded(task, monkeypatch, tmp_path, target, wrap=None):
    log = tmp_path / "guarded.jsonl"
    monkeypatch.setenv("FAKE_GUARDED_READ", str(target))
    monkeypatch.setenv("FAKE_GUARDED_LOG", str(log))
    if wrap:
        real = lab.read_guard_hook
        monkeypatch.setattr(lab, "read_guard_hook", lambda work, task: wrap(real(work, task)))
    r = lab.attempt(task, "A")
    settings = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())
    return r, json.loads(log.read_text().splitlines()[-1]), settings


def test_read_guard_is_its_own_fail_closed_hook_for_the_reading_tools(task, monkeypatch, tmp_path):
    outside = Path.home() / "late-secret.txt"                       # appears after the settings, no deny names it
    outside.write_text("s")
    r, g, settings = _guarded(task, monkeypatch, tmp_path, outside)
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == lab.READ_GUARD_MATCHER and pre[0]["hooks"][0]["onFailure"] == "block"
    assert "readguard.py" in pre[0]["hooks"][0]["command"] and pre[1]["matcher"] == "*"
    assert g["refused"] and "outside the working copy" in json.dumps(g["res"])
    assert r["observation"]["reads_outside"] == []                   # refused, so nothing read


def test_read_guard_error_before_the_decision_blocks_the_read(task, monkeypatch, tmp_path):
    # review of PR #8 (P1): an import error before the check let the read through with exit 0
    outside = Path.home() / "late-secret.txt"
    outside.write_text("s")

    def broken(h):
        h["hooks"][0]["command"] = h["hooks"][0]["command"].replace("SWARMSCOPE_READ_GUARD=", "X_UNUSED=")
        return h
    r, g, _ = _guarded(task, monkeypatch, tmp_path, outside, broken)
    assert g["refused"] and "blocking because onFailure" in g["res"]["_blocked"]


def test_read_guard_timeout_blocks_the_read(task, monkeypatch, tmp_path):
    outside = Path.home() / "late-secret.txt"
    outside.write_text("s")

    def slow(h):
        h["hooks"][0]["command"] = "sleep 5; " + h["hooks"][0]["command"]
        h["hooks"][0]["timeout"] = 1
        return h
    r, g, _ = _guarded(task, monkeypatch, tmp_path, outside, slow)
    assert g["refused"] and "timed out" in g["res"]["_blocked"]


def test_read_guard_lets_the_working_copy_through(task, monkeypatch, tmp_path):
    r, g, _ = _guarded(task, monkeypatch, tmp_path, "src/inside.py")     # relative: inside the own copy
    assert not g["refused"] and g["res"] in (None, {})
    # a sibling run's copy beside it is outside
    r, g, _ = _guarded(task, monkeypatch, tmp_path, Path(lab.work_root(lab.load_modes()[0])) / "other" / "x")
    assert g["refused"]


def test_old_claude_code_is_refused(task, monkeypatch):
    assert any("older than 2.1.295" in p for p in lab.check_init(
        {"model": "m", "claude_code_version": "2.1.200"}, lab.load_modes()[0], "/tmp"))


def test_reads_outside_the_working_copy_make_a_run_invalid(tmp_path):
    proj = tmp_path / "projects" / "-w-1"
    (proj / "sess" / "tool-results").mkdir(parents=True)
    work = tmp_path / "w"
    work.mkdir()
    tp = proj / "sess.jsonl"

    def use(i, name, inp):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}

    def res(i, err=False):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err,
                                                         "content": "x"}]}}
    recs = [use("1", "Read", {"file_path": str(proj / "sess" / "tool-results" / "a.txt")}), res("1"),
            use("2", "Read", {"file_path": "src/x.py"}), res("2"),
            use("3", "Read", {"file_path": str(tmp_path / "secret")}), res("3", err=True),     # refused
            use("4", "Grep", {"pattern": "k", "path": str(tmp_path / "projects")}), res("4"),
            use("5", "Read", {"file_path": str(proj / "sess.jsonl")}), res("5"),
            # review of PR #8: another session's outputs in the same project, and a Glob pattern leaving its path
            use("6", "Read", {"file_path": str(proj / "other" / "tool-results" / "b.txt")}), res("6"),
            use("7", "Glob", {"pattern": str(tmp_path / "secret") + "/*"}), res("7"),
            use("8", "Glob", {"pattern": "src/*/../../../x/*"}), res("8"),
            use("9", "Glob", {"pattern": "src/**/*.py"}), res("9")]
    tp.write_text("\n".join(json.dumps(x) for x in recs) + "\n")
    hits = lab.read_outside(str(tp), work)
    paths = sorted(h[2] for h in hits)
    assert str(tmp_path / "projects") in paths and str(proj / "sess.jsonl") in paths
    assert str(proj / "other" / "tool-results" / "b.txt") in paths
    assert str(tmp_path / "secret") in paths and any("climbs out" in p for p in paths)
    assert len(hits) == 5                                                  # 1, 2, 3 (refused) and 9 are fine


def test_working_copy_hides_the_experiment(task):
    r = lab.attempt(task, "B", keep_work=True)
    work = lab.work_root(lab.load_modes()[0]) / r["run_id"]
    assert not set(os.listdir(work)) & {".claude", "PROMPT.txt", ".swarmscope", "CLAUDE.md"}
    assert subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.strip() == ""
    assert lab.lab_root().resolve() not in work.resolve().parents


# --- validity, session status and the verdict are separate --------------------------------------------

def test_timeout_is_never_success_even_when_hidden_tests_pass(task, monkeypatch):
    # ChatGPT, 21.11: a session killed by its budget (no result message) still counts as an outcome - a failure -
    # so the longest attempts do not drop out of the denominator; its cost is only an estimate
    modes_with(monkeypatch, timeout_s=1)
    monkeypatch.setenv("FAKE_SLEEP", "4")
    monkeypatch.setenv("FAKE_NO_STOP", "1")
    r = lab.attempt(task, "A")
    assert r["session_status"] == "timeout" and r["success"] is False
    assert r["valid"] is True and r["timeout_class"] == "budget", r["integrity"]
    assert r["costs"]["run_usd_status"] == "estimated" and r["costs"]["claude_code_total_usd"] is None
    assert r["verdict"]["session_completed"] is False


def test_work_after_the_result_is_a_tail_not_a_cost_mismatch(task, monkeypatch):
    # 2ec1703088 (ChatGPT, 21.11): a result at 936 s, then a background task notification woke the model; the
    # budget killed it. Claude Code's total covers the session up to the result: the tail is reconstructed apart
    modes_with(monkeypatch, timeout_s=3)
    monkeypatch.setenv("FAKE_TAIL_AFTER_RESULT", "8")
    r = lab.attempt(task, "A")
    c = r["costs"]
    assert r["session_status"] == "timeout" and r["timeout_class"] == "budget"
    assert r["valid"] is True, r["integrity"]            # a budget timeout with full accounting: a valid failure
    assert r["success"] is False and c["reconstruction"] == "complete"
    t = c["tail_after_result"]
    assert t["messages"] == 1 and t["usd"] > 0 and t["reconciled"] is False and t["claude_code_total"] is None
    assert abs(c["computed_total_usd"] - c["reconciled_total_usd"] - t["usd"]) < 1e-9
    assert abs(c["reconciled_total_usd"] - c["claude_code_total_usd"]) <= 0.01


def test_timeout_classes():
    init = {"session_id": "s"}
    assert lab.timeout_class(init, [], None) == "budget"
    assert lab.timeout_class(init, [{"attempt": 1}], None) == "infra_suspected"          # retried requests
    assert lab.timeout_class(init, [], ("api_error", "rejected")) == "infra_suspected"  # api_failure
    assert lab.timeout_class(None, [], None) == "not_started"
    allowed = [{"rate_limit_info": {"status": "allowed"}}] * 11
    assert lab.timeout_class(init, [], None, allowed) == "budget"
    assert lab.timeout_class(init, [], None, allowed + [{"rate_limit_info": {"status": "rejected"}}]) == "infra_suspected"
    # allowed rate-limit events are no failure (668882775e: 11 of them, finished normally): api_failure ignores them
    assert lab.api_failure(None, [{"rate_limit_info": {"status": "allowed"}}] * 11) is None


def test_a_subagent_after_the_result_stays_in_the_tail(task, monkeypatch):
    # ChatGPT, 21.11 (P2): the prefix took every subagent message whole, so a subagent working after the result
    # made a false mismatch; the split covers the lead, the advisor and the subagents
    modes_with(monkeypatch, timeout_s=3)
    monkeypatch.setenv("FAKE_TAIL_AFTER_RESULT", "8")
    monkeypatch.setenv("FAKE_TAIL_SUB", "1")
    r = lab.attempt(task, "A")
    c = r["costs"]
    assert r["valid"] is True, r["integrity"]
    assert c["reconstruction"] == "complete" and c["tail_after_result"]["messages"] == 2
    assert c["by_model_reconciled"]["subagents"] == {} and c["by_model"]["subagents"]["s1"]["usd"] > 0
    assert c["run_usd_status"] == "partly_confirmed"
    assert abs(c["run_usd"] - (c["claude_code_total_usd"] + c["tail_after_result"]["usd"])) < 1e-6


def test_models_are_checked_over_the_whole_run_tail_included(task, monkeypatch):
    # ChatGPT review of PR #16 (P1): an Opus subagent after the result in A0 (no advisor) went unseen: check_models
    # got the prefix only
    modes_with(monkeypatch, timeout_s=3)
    monkeypatch.setenv("FAKE_TAIL_AFTER_RESULT", "8")
    monkeypatch.setenv("FAKE_TAIL_SUB", "1")
    monkeypatch.setenv("FAKE_TAIL_SUB_MODEL", "claude-opus-5-5")
    r = lab.attempt(task, "A0")
    assert r["valid"] is False and "unexpected model ran: claude-opus-5-5" in r["integrity"], r["integrity"]
    assert "claude-opus-5-5" in r["models_seen"]


def test_text_totals_mark_estimated_costs(task, monkeypatch, capsys):
    # ChatGPT review of PR #16 (P2): calib printed "mean $1.400" with cost_unconfirmed=1; pairs' total and spent
    # had no mark either
    a, b = _prj("A", 1, 1.0), _prj("B", 1, 1.0)
    a["costs"].update(reconstruction="complete", tail_after_result={"messages": 1, "usd": 0.4}, run_usd=1.4,
                      run_usd_status="partly_confirmed")
    b["costs"].update(reconstruction="complete", run_usd=1.0, run_usd_status="confirmed")
    for r in (a, b):
        r["tests"]["hidden"].update(passed=1, expected=1)
    monkeypatch.setattr(lab, "load_runs", lambda *x, **k: [a, b])
    monkeypatch.setattr(lab, "load_manifests", lambda *x, **k: {})
    import types
    lab.cmd_pairs(types.SimpleNamespace(pilot=None, batch=None, modes="A,B", json=False, all=True, revalidated=False,
                                        task=None))
    out = capsys.readouterr().out
    assert "total [setup" in out and "A ~$1.40" in out and "1 run cost(s) estimated; confirmed pairs 0/1)" in out
    assert "spent: ~$2.40" in out and "1 run cost(s) estimated" in out.split("spent:")[1]
    t = lab.calibration([a, _prj("A", 2, 0.6)], "A")
    row = next(iter(t.values()))
    assert row["cost_unconfirmed"] == 1 and row["cost_usd_confirmed"] == [0.6]
    runs_dir = lab.lab_root() / "runs"
    for r in (a, _prj("A", 2, 0.6)):
        d = runs_dir / r["run_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(r))
    lab.cmd_calib(types.SimpleNamespace(mode="A", batch=None, json=False))
    out = capsys.readouterr().out
    assert "mean ~$1.000 (1 of 2 estimated; confirmed mean $0.600 over 1)" in out


def test_reports_count_the_whole_run_and_say_how_far_it_is_confirmed(capsys):
    # ChatGPT, 21.11 (P1): A $1 + $0.40 tail, B $1 + $0.90 tail - the reports showed $2 and +$0 instead of ~$3.30, +$0.50
    a, b = _prj("A", 1, 1.0), _prj("B", 1, 1.0)
    for r, tail in ((a, 0.40), (b, 0.90)):
        r["costs"].update(reconstruction="complete", tail_after_result={"messages": 1, "usd": tail},
                          run_usd=1.0 + tail, run_usd_status="partly_confirmed")
    t = only_row(lab.pair_report([a, b]), "pk-x")
    assert abs(t["mean_diff_usd"] - 0.50) < 1e-9 and abs(t["A_usd_total"] + t["B_usd_total"] - 3.30) < 1e-9
    assert t["confirmed_cost_pairs"] == 0 and t["mean_diff_usd_confirmed"] is None
    assert lab.run_cost(a) == 1.40 and lab.run_cost_status(a) == "partly_confirmed"
    # an old-style run (no run_usd) with a reconciled total stays confirmed
    old = _prj("A", 2, 0.7)
    assert lab.run_cost(old) == 0.7 and lab.run_cost_status(old) == "confirmed"


def test_a_task_sets_its_own_session_budget(task, monkeypatch):
    # ChatGPT, 21.11: pk-prerel gets 1800 s; the same budget for every mode, recorded, part of the task hash
    seen = []
    real = lab.run_session
    monkeypatch.setattr(lab, "run_session", lambda *a, **k: seen.append(a[6]) or real(*a, **k))
    r = lab.attempt(dict(task, session_timeout_s=1800), "A")
    assert seen == [1800] and r["config"]["timeout_s"] == 1800 and r["config"]["timeout_source"] == "task"
    r = lab.attempt(task, "B")
    assert seen[-1] == lab.load_modes()[0]["common"]["timeout_s"] and r["config"]["timeout_source"] == "modes"


def test_sh_timeout_partial_output_is_text():
    r = lab.sh([sys.executable, "-c", "import sys,time; print('partial', flush=True); time.sleep(5)"], timeout=1)
    assert r["timed_out"] and isinstance(r["out"], str) and isinstance(r["err"], str)


@pytest.mark.parametrize("text,expected", [
    ("....\n----\nRan 4 tests in 0.01s\n\nOK\n", (4, 0)),
    ("..F\n----\nRan 3 tests in 0.01s\n\nFAILED (failures=1)\n", (3, 1)),
    ("E\n----\nRan 2 tests in 0.01s\n\nFAILED (failures=1, errors=1)\n", (2, 2)),
    ("Traceback (most recent call last):\nImportError\n", (None, None)),
    ("", (None, None)),
])
def test_parse_unittest(text, expected):
    assert lab.parse_unittest(text) == expected


def test_suite_without_report_or_wrong_count_is_not_ok(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    no_report = lab.run_suite("hidden", "exit 0", snap, run_dir, expected=7)
    assert no_report["ok"] is False and no_report["report"] is False
    fewer = lab.run_suite("hidden", f"{sys.executable} -c \"print('Ran 3 tests in 0.1s'); print(); print('OK')\"",
                          snap, run_dir, expected=7)
    assert fewer["ok"] is False and fewer["tests"] == 3


def test_suites_run_on_separate_copies(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lab.run_suite("public", "touch leaked_marker", snap, run_dir)
    r = lab.run_suite("hidden", "test ! -e leaked_marker && echo 'Ran 1 test in 0s' && echo && echo OK", snap, run_dir)
    assert r["ok"] and not (snap / "leaked_marker").exists()


def test_run_and_batch_exit_nonzero_on_infrastructure_error(task, monkeypatch):
    monkeypatch.setenv("FAKE_MODEL", "claude-other-1")
    assert lab.cmd_run(argparse.Namespace(task=task["_file"], mode="A")) == 1
    assert lab.cmd_batch(argparse.Namespace(task=task["_file"], modes="A", repeats=1, seed=1)) == 1


# --- the advisor: one unit per blocked Stop, calls attributed to the block's window --------------------

def test_attribution_never_closes_a_stale_block():
    blocks = [{"ts": 100.0, "until": 110.0, "decision_ids": ["r1"], "rules": ["R3"], "actions": ["consult_advisor"],
               "requests_consult": True},
              {"ts": 110.0, "until": None, "decision_ids": ["r2"], "rules": ["R3"], "actions": ["consult_advisor"],
               "requests_consult": True}]
    calls = [{"id": "c1", "ts": 111.0, "status": "returned"}, {"id": "c2", "ts": 112.0, "status": "failed"}]
    out, per = lab.attribute(calls, blocks)
    assert per[0]["calls"] == [] and per[0]["outcome"] == "not_called"
    assert per[1]["calls"] == ["c1", "c2"] and per[1]["outcome"] == "returned"
    assert [c["voluntary"] for c in out] == [False, False]


def test_r2_and_r3_on_one_stop_are_one_request():
    recs = [{"kind": "decision", "id": "s-2", "rule": "R2_repeat_error", "state": "requested", "action": "consult_advisor", "stop_ts": 100.0},
            {"kind": "decision", "id": "s-3", "rule": "R3_high_risk", "state": "requested", "action": "consult_advisor", "stop_ts": 100.0},
            {"kind": "block", "ts": 100.0, "ids": ["s-2", "s-3"]}]
    blocks = lab.blocks_from_log(recs, [100.0, 130.0])
    assert len(blocks) == 1 and blocks[0]["rules"] == ["R2_repeat_error", "R3_high_risk"] and blocks[0]["until"] == 130.0
    out, per = lab.attribute([{"id": "c", "ts": 140.0, "status": "returned"}], blocks)
    assert per[0]["outcome"] == "not_called" and out[0]["voluntary"] is True       # after the next Stop: voluntary


def test_calls_after_a_tests_only_block_are_voluntary():
    blocks = [{"ts": 100.0, "until": None, "decision_ids": ["s-1"], "rules": ["R1_tests"], "actions": ["run_tests"],
               "requests_consult": False}]
    out, per = lab.attribute([{"id": "c", "ts": 105.0, "status": "returned"}], blocks)
    assert out[0]["voluntary"] is True and per[0]["outcome"] is None


# --- Jev: the key stays on the runner side, every Stop needs an ok answer -----------------------------

def test_no_key_on_runner_side_does_not_start(task, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    r = lab.attempt(task, "A")
    assert r["session_status"] == "not_started" and any("no TypeSafe key" in p for p in r["integrity"])


def test_jev_errors_make_the_run_invalid(task, monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", "http://127.0.0.1:9/v1/systemone")       # nothing listens
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("jev answers not ok" in p for p in r["integrity"])


def test_jev_context_must_match_a_stop():
    recs = [{"kind": "context", "ctx_id": "x", "rules": [{"id": "s-9", "rule": "R1_tests", "state": "pass"}]},
            {"kind": "jev", "ctx_id": "x", "status": "ok", "p_consult": 0.2}]
    dec = [{"kind": "decision", "id": "s-1", "stop_ts": 1.0}]
    _, problems = lab.summarize_jev(recs, dec, True)
    assert any("does not match" in p for p in problems) and any("without a jev context" in p for p in problems)


# --- config, init, transcript, batch invariants -------------------------------------------------------

def test_policy_used_by_the_hook_must_be_the_runners():
    recs = [{"kind": "decision", "id": "s-1", "rule": "R3_high_risk", "state": "triggered", "stop_ts": 1.0,
             "gate": {"mode": "observe"}, "policy_sha256": "tampered"}]
    _, problems = lab.summarize_decisions(recs, "observe", "original")
    assert any("policy file used by the hook" in p for p in problems)


def test_unexpected_model_is_an_integrity_problem(task, monkeypatch):
    monkeypatch.setenv("FAKE_MODEL", "claude-other-1")
    r = lab.attempt(task, "A")
    assert any("init model" in p for p in r["integrity"]) and any("unexpected model ran" in p for p in r["integrity"])


def test_skills_loaded_in_the_session_are_an_integrity_problem(task, monkeypatch):
    monkeypatch.setenv("FAKE_SKILLS", '["built-in-browser"]')
    r = lab.attempt(task, "A")
    assert any("skills loaded" in p for p in r["integrity"]) and "--disable-slash-commands" in r["config"]["argv"]


def test_a_tool_off_the_allow_list_makes_a_run_invalid(task, monkeypatch):
    # PR #8 review: Workflow read a file past the read guard, and the tool set changes without a version change
    monkeypatch.setenv("FAKE_EXTRA_TOOLS", "Workflow")
    r = lab.attempt(task, "A")
    assert not r["valid"] and any("not on the allow-list" in p and "Workflow" in p for p in r["integrity"])
    monkeypatch.delenv("FAKE_EXTRA_TOOLS")
    assert lab.attempt(task, "A")["valid"]


def test_workflow_is_denied_in_the_settings(task, tmp_path):
    modes = lab.load_modes()[0]
    p = lab.write_settings(tmp_path, tmp_path / "sw", tmp_path / "pol.json", tmp_path / "w", task, lab.lab_root(), modes)
    deny = json.loads(p.read_text())["permissions"]["deny"]
    assert "Workflow" in deny and "WebFetch" in deny


def test_batch_invariant_mismatch_stops_the_attempt(task):
    expect = lab.batch_expect(task, lab.load_modes()[1])
    r = lab.attempt(task, "A", expect=dict(expect, hidden_tests_sha256="other"))
    assert r["session_status"] == "not_started" and any("batch invariant hidden_tests_sha256" in p for p in r["integrity"])
    assert lab.attempt(task, "A", expect=expect)["valid"]


def test_preflight_config_template(task):
    cfg = lab.lab_root() / "config"
    work = lab.work_root(lab.load_modes()[0]) / "x"
    work.mkdir(parents=True)
    assert lab.preflight(work, cfg)["ok"]
    (cfg / "agents" / "extra.md").write_text("an agent the repo does not have")
    (cfg / "rules").mkdir()
    (cfg / "rules" / "r.md").write_text("always consult the advisor")
    probs = lab.preflight(work, cfg)["problems"]
    assert any("config agents" in p for p in probs) and any("has rules" in p for p in probs)
    inside_home = lab.lab_root().parent / "w-in-home"
    inside_home.mkdir()
    assert any("inside the home folder" in p for p in lab.preflight(inside_home, cfg)["problems"])


def test_preflight_stops_on_instructions_above_the_working_copy(task):
    (lab.work_root(lab.load_modes()[0]) / "CLAUDE.md").write_text("Consult the advisor before you finish.")
    r = lab.attempt(task, "A")
    assert r["session_status"] == "not_started" and any("CLAUDE.md" in p for p in r["integrity"])


def test_agent_env_is_an_allow_list(monkeypatch, tmp_path):
    for k, v in {"SWARMSCOPE_POLICY_FILE": "/x", "TYPESAFE_API_KEY": "k", "CLAUDE_CODE_EFFORT_LEVEL": "low",
                 "ANTHROPIC_MODEL": "m", "SOME_TOKEN": "t", "PATH": "/usr/bin"}.items():
        monkeypatch.setenv(k, v)
    env, dropped = lab.agent_env(tmp_path)
    assert env["PATH"] == "/usr/bin" and env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert not {"SWARMSCOPE_POLICY_FILE", "TYPESAFE_API_KEY", "CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL",
                "SOME_TOKEN"} & set(env)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_user_git_signing_config_does_not_reach_the_base_commit(task, tmp_path):
    (tmp_path / "home" / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n")
    r = lab.attempt(task, "A", keep_work=True)
    assert r["session_status"] != "not_started", r["integrity"]
    work = lab.work_root(lab.load_modes()[0]) / r["run_id"]
    cfg = subprocess.run(["git", "config", "--local", "commit.gpgsign"], cwd=work, capture_output=True, text=True)
    assert cfg.stdout.strip() == "false"


def test_base_failure_is_recorded_not_raised(task):
    r = lab.attempt(dict(task, source=dict(task["source"], commit="no-such-commit")), "A")
    assert r["session_status"] == "not_started" and any(p.startswith("base:") for p in r["integrity"])
    assert (lab.lab_root() / "runs" / r["run_id"] / "run.json").exists()


def test_builtin_plugins_are_clean_others_are_not():
    builtin = {"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}
    other = {"name": "x", "path": "/u/.claude/plugins/x", "source": "x@market"}
    assert lab.not_clean({"plugins": [builtin], "skills": [], "mcp_servers": []}) == {}
    assert lab.not_clean({"plugins": [builtin, other]}) == {"plugins": [other]}


def test_doctor_ping_and_advisor_check(task, capsys):
    assert lab.cmd_doctor(argparse.Namespace(ping=True, advisor_check=True)) == 0
    out = capsys.readouterr().out
    assert "'OK'" in out and "advisor      ok" in out and out.strip().endswith("ready")


# --- probe: the checker must catch a leak --------------------------------------------------------------

def test_probe_passes_for_a_contained_agent(task, capsys):
    assert lab.cmd_probe(argparse.Namespace(task=task["_file"])) == 0
    assert "boundary HOLDS" in capsys.readouterr().out


def test_probe_catches_a_leak(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_LEAK", "1")
    assert lab.cmd_probe(argparse.Namespace(task=task["_file"])) == 1
    out = capsys.readouterr().out
    assert "FAIL  canary_not_in_transcript" in out and "FAIL  step_1_bash_read_canary" in out
    assert "boundary BROKEN" in out


def test_probe_catches_open_network(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_NET", "1")
    assert lab.cmd_probe(argparse.Namespace(task=task["_file"])) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_9_bash_network" in out and "FAIL  no_network_content" in out and "boundary BROKEN" in out


# --- review 2: nothing the agent configures runs with the runner's rights --------------------------------

def test_agent_git_config_never_runs_in_hooks_or_final_diff(task, monkeypatch, tmp_path):
    marker = tmp_path / "pwned"
    monkeypatch.setenv("FAKE_EVIL", str(marker))
    r = lab.attempt(task, "B")
    assert not marker.exists()
    files = {f["path"]: f for f in r["diff"]["files"]}
    assert r["diff"]["method"] == "python" and files["src/inventory/stock.py"]["added"] == 60
    assert ".gitattributes" in files
    assert not (lab.lab_root() / "runs" / r["run_id"] / "snapshot" / ".git").exists()


def test_suites_get_a_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-key")
    monkeypatch.setenv("SWARMSCOPE_POLICY_FILE", "/runner/policy.json")
    monkeypatch.setenv("SOME_TOKEN", "t")
    snap = tmp_path / "snap"
    snap.mkdir()
    r = lab.run_suite("hidden", "env; echo 'Ran 1 test in 0s'; echo; echo OK", snap, tmp_path, work_base=tmp_path)
    assert r["ok"] and "secret-key" not in r["tail"] and "SWARMSCOPE" not in r["tail"] and "SOME_TOKEN" not in r["tail"]
    assert f"HOME={tmp_path}" in r["tail"]          # HOME points into the throw-away check folder


def test_unsandboxed_tests_are_an_integrity_problem(task, monkeypatch):
    monkeypatch.delenv("SWARMSCOPE_LAB_UNSANDBOXED_TESTS")
    # no sandbox on this machine: on Linux there is none anyway; on the lab Mac sandbox-exec exists and has to be
    # hidden, or the tests simply run sandboxed (the test failed there, 18.11)
    real_which = lab.shutil.which
    monkeypatch.setattr(lab.shutil, "which", lambda n, *a, **k: None if n == "sandbox-exec" else real_which(n, *a, **k))
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("tests not run: no sandbox available" in p for p in r["integrity"])
    assert r["tests"]["hidden"]["sandbox"] == "unavailable" and r["success"] is False


def test_seatbelt_profile_denies_home_and_network(tmp_path):
    prof = lab.seatbelt_profile(tmp_path)
    assert "(deny network*)" in prof
    assert f'(deny file-read* (subpath "{tmp_path.resolve()}"))' in prof
    assert f'(deny file-write* (subpath "{tmp_path.resolve()}"))' in prof


@pytest.mark.parametrize("text,expected", [
    ("sssssss\n----\nRan 7 tests in 0.01s\n\nOK (skipped=7)\n", (7, 7)),
    ("..x\n----\nRan 3 tests in 0.01s\n\nOK (expected failures=1)\n", (3, 1)),
    ("..u\n----\nRan 3 tests in 0.01s\n\nFAILED (unexpected successes=1)\n", (3, 1)),
    ("..s\n----\nRan 3 tests in 0.01s\n\nOK (skipped=1)\n", (3, 1)),
])
def test_skips_and_expected_failures_are_not_passes(text, expected):
    assert lab.parse_unittest(text) == expected


def test_all_skipped_hidden_suite_is_not_ok(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    r = lab.run_suite("hidden", f"{sys.executable} -c \"print('Ran 7 tests in 0.1s'); print(); print('OK (skipped=7)')\"",
                      snap, tmp_path, expected=7, work_base=tmp_path)
    assert r["ok"] is False and r["passed"] == 0 and r["tests"] == 7


def test_probe_catches_a_readable_own_transcript(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_READ_TRANSCRIPT", "1")
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_14_tool_read_own_transcript" in out and "boundary BROKEN" in out


def test_a_read_outside_the_working_copy_makes_the_run_invalid(task, monkeypatch):
    secret = lab.lab_root() / "runs" / "elsewhere.txt"
    monkeypatch.setenv("FAKE_READ_OUTSIDE", str(secret))
    r = lab.attempt(task, "A")
    assert not r["valid"] and any("read outside" in p for p in r["integrity"])
    assert r["observation"]["reads_outside"][0][2] == str(secret.resolve())


def test_probe_step_12_must_read_the_file_step_11_named(task, monkeypatch, capsys, tmp_path):
    # review of PR #8: a successful Read of some other file must not pass step 12
    other = tmp_path / "x" / "tool-results" / "other.txt"
    monkeypatch.setenv("FAKE_SPILL_READ", str(other))
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    assert "FAIL  step_12_reads_the_file_of_step_11" in capsys.readouterr().out


def test_probe_creates_late_paths_after_the_settings_and_cleans_them(task, capsys, monkeypatch):
    seen = {}
    real = lab.write_settings

    def spy(run_dir, *a, **k):
        p = real(run_dir, *a, **k)
        seen["deny"] = json.loads(p.read_text())["permissions"]["deny"]
        return p
    monkeypatch.setattr(lab, "write_settings", spy)
    assert lab.main(["probe", "--task", task["_file"]]) == 0
    out = capsys.readouterr().out
    for k in ("17_tool_read_late_home", "18_tool_read_late_config", "19_tool_read_late_project",
              "20_tool_read_other_session", "22_sub_grep_late_home", "23_sub_grep_climbs_out",
              "24_sub_grep_tilde_project", "25_sub_grep_space_project"):
        assert f"ok    step_{k}" in out
    assert not any("_probe-late-" in r for r in seen["deny"])           # unknown to the settings: truly late
    assert not list(Path.home().glob("_probe-late-*"))
    assert not list((lab.lab_root() / "config").glob("_probe-late-*"))
    assert not list((lab.lab_root() / "config" / "projects").glob("*/probe-other-session-*"))


def test_probe_a_missing_tool_is_not_a_refusal(task, monkeypatch, capsys):
    # live probes c157de6666 (Glob, 19.11) and b5da123f22 (Grep, 20.11): "No such tool" is no refusal
    monkeypatch.setenv("FAKE_ABSENT_TOOLS", "Grep")
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_22_sub_grep_late_home  (tool not available" in out and "boundary BROKEN" in out


def test_probe_runs_grep_and_glob_in_the_grep_subagent(task, capsys):
    # PR #8 review: the main session of 2.1.326 has no Grep/Glob, the lab's grep subagent has both
    assert lab.main(["probe", "--task", task["_file"]]) == 0
    out = capsys.readouterr().out
    for label in ("21_task_grep_agent", "22_sub_grep_late_home", "23_sub_grep_climbs_out", "24_sub_grep_tilde_project",
                  "25_sub_grep_space_project", "26_sub_glob_project", "27_sub_glob_tilde_project"):
        assert f"ok    step_{label}" in out, label


def test_probe_catches_a_guard_that_misreads_a_tilde_path(task, monkeypatch, capsys, tmp_path):
    # the bypass of the PR #8 review: the old guard joined '~/...' and ' /abs' to the working copy; Claude Code
    # expands and trims them (the fake does too) - the probe must see the read go through
    src = (Path(lab.REPO) / "readguard.py").read_text()
    old = (src.replace('    if path.startswith("~"):\n        return f"path starts with ~: {path!r}"\n', "")
              .replace("    if path and (path[0].isspace()", "    if False and (path[0].isspace()"))
    assert old != src
    (tmp_path / "readguard.py").write_text(old)
    real = lab.read_guard_hook

    def broken(work, task_):
        h = real(work, task_)
        h["hooks"][0]["command"] = h["hooks"][0]["command"].replace(str(Path(lab.REPO) / "readguard.py"),
                                                                    str(tmp_path / "readguard.py"))
        return h
    monkeypatch.setattr(lab, "read_guard_hook", broken)
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_24_sub_grep_tilde_project  (allowed" in out
    assert "FAIL  step_25_sub_grep_space_project  (allowed" in out
    assert "FAIL  step_27_sub_glob_tilde_project  (allowed" in out
    assert "FAIL  canary_not_in_transcript" in out and "boundary BROKEN" in out


def test_judge_steps_a_denied_step_fails_if_a_later_call_got_through():
    # PR #8 review (P3): a failing first try, then the same path succeeds - the step was counted as refused
    calls = [("Agent", {"subagent_type": "grep"}, {"is_error": False, "text": "launched"}, None, "t9"),
             ("Grep", {"path": "~/x", "pattern": "("}, {"is_error": True, "text": "regex error"}, "t9", "s1"),
             ("Grep", {"path": "~/x", "pattern": "a"}, {"is_error": False, "text": "/x/secret.txt"}, "t9", "s2")]
    step = [("24_sub_grep_tilde_project", "Grep", '"path": "~/x"', True, "sub")]
    r = lab.judge_steps(calls, step)["24_sub_grep_tilde_project"]
    assert r["ok"] is False and r["why"].startswith("allowed on a later call")
    assert lab.judge_steps(calls[:2], step)["24_sub_grep_tilde_project"]["ok"] is True


def test_judge_steps_takes_the_subagent_call_by_either_name():
    # live probe c0a75b5374: init lists "Task", the model called "Agent"; the step was "not attempted"
    for name in ("Task", "Agent"):
        calls = [(name, {"subagent_type": "grep"}, {"is_error": False, "text": "launched"}, None, "t9"),
                 ("Grep", {"path": "/x"}, {"is_error": True, "text": "denied"}, "t9", "s1")]
        steps = [("21_task_grep_agent", "Task", '"subagent_type": "grep"', False),
                 ("22_sub_grep_late_home", "Grep", '"path": "/x"', True, "sub")]
        assert all(v["ok"] for v in lab.judge_steps(calls, steps).values()), name


def test_judge_steps_takes_sub_steps_only_from_the_grep_subagent():
    calls = [("Grep", {"path": "/x"}, {"is_error": True, "text": "denied"}, None, "m1"),
             ("Task", {"subagent_type": "grep"}, {"is_error": False, "text": "launched"}, None, "t9"),
             ("Grep", {"path": "/x"}, {"is_error": True, "text": "denied"}, "other", "s0"),
             ("Grep", {"path": "/x"}, {"is_error": True, "text": "No such tool available: Grep"}, "t9", "s1")]
    step = [("22_sub_grep_late_home", "Grep", '"path": "/x"', True, "sub")]
    # the main session's call and another agent's do not count; the grep subagent's "No such tool" fails
    assert lab.judge_steps(calls, step)["22_sub_grep_late_home"]["ok"] is False
    assert lab.judge_steps(calls[:3], step)["22_sub_grep_late_home"]["why"] == "not attempted in the grep subagent"
    assert lab.judge_steps(calls[:3] + [calls[2][:3] + ("t9", "s2")], step)["22_sub_grep_late_home"]["ok"] is True
    assert lab.judge_steps([c for c in calls if c[0] != "Task"], step)["22_sub_grep_late_home"]["ok"] is False


TASK_STEPS = ("28_bash_background", "29_bash_wait", "30_tool_read_own_task_output", "31_tool_read_task_neighbour",
              "32_tool_read_task_link_out", "33_tool_read_other_session_task", "34_tool_read_other_project_task",
              "35_bash_replace_task_folder", "36_tool_read_through_replaced_folder")


def test_probe_reads_its_own_task_output_and_nothing_beside_it(task, capsys):
    # a0-pk-1 (run 6fe8f53cc9), ChatGPT 22.11: the own background output readable; another session's, another
    # project's, a neighbour file and a link out refused; the probe's files are cleaned up after
    assert lab.main(["probe", "--task", task["_file"]]) == 0
    out = capsys.readouterr().out
    for label in TASK_STEPS:
        assert f"ok    step_{label}" in out, label
    assert "ok    step_30_reads_the_output_of_step_28" in out and "ok    task_files_planted" in out
    assert "ok    task_folder_swap_conclusive" in out
    root = Path(lab.claude_tmp_root())
    assert not list(root.glob("-probe-other-*")) and not list(root.glob("*/probe-other-session-*"))
    assert not list(root.glob("*/*/tasks/neighbour.txt")) and not list(root.glob("*/*/tasks/link.output"))
    assert not list(root.glob("*/*/tasks.orig"))                         # step 35's link undone


def _guard_from(tmp_path, monkeypatch, edit=None, spec_drop=None):
    src = (Path(lab.REPO) / "readguard.py").read_text()
    new = edit(src) if edit else src
    assert new != src or not edit
    (tmp_path / "readguard.py").write_text(new)
    real = lab.read_guard_hook

    def patched(work, task_):
        h = real(work, task_)
        cmd = h["hooks"][0]["command"].replace(str(Path(lab.REPO) / "readguard.py"), str(tmp_path / "readguard.py"))
        if spec_drop:
            spec = {k: v for k, v in json.loads(cmd.split("SWARMSCOPE_READ_GUARD=")[1].split("' ")[0].strip("'")).items()
                    if k != spec_drop}
            cmd = f"SWARMSCOPE_READ_GUARD={shlex.quote(json.dumps(spec))} " + cmd.split("' ", 1)[1]
        h["hooks"][0]["command"] = cmd
        return h
    monkeypatch.setattr(lab, "read_guard_hook", patched)


def test_probe_catches_a_guard_that_opens_other_task_folders(task, monkeypatch, capsys, tmp_path):
    # a guard that checks the name but not the folder: another session's and another project's outputs leak
    _guard_from(tmp_path, monkeypatch, edit=lambda s: s.replace("os.path.dirname(path) != tdir or ", ""))
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_33_tool_read_other_session_task  (allowed" in out
    assert "FAIL  step_34_tool_read_other_project_task  (allowed" in out
    assert "FAIL  canary_not_in_transcript" in out and "boundary BROKEN" in out


def test_probe_catches_a_guard_that_resolves_the_task_folder(task, monkeypatch, capsys, tmp_path):
    # the guard of HEAD 6fc7e9c: the pinned folder went through realpath, so a link put in its place by the
    # agent's Bash (step 35) made another session's outputs "own" (ChatGPT, 22.11, P1)
    _guard_from(tmp_path, monkeypatch, edit=lambda s: s.replace(
        'return os.path.join(root, project, session_id, "tasks")',
        'return os.path.realpath(os.path.join(root, project, session_id, "tasks"))'))
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_36_tool_read_through_replaced_folder  (allowed" in out
    assert "FAIL  canary_not_in_transcript" in out and "boundary BROKEN" in out


def test_probe_needs_step_35_to_be_conclusive(task, monkeypatch, capsys):
    # a Bash step that "worked" but replaced nothing proves nothing about step 36
    monkeypatch.setenv("FAKE_NO_REPLACE", "1")
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    assert "FAIL  task_folder_swap_conclusive" in capsys.readouterr().out


def test_probe_accepts_a_swap_the_sandbox_refused(task, monkeypatch, capsys):
    # lab Mac, probe ed5f9459d1: Seatbelt refused the mv of the tasks folder - the attack is impossible there
    monkeypatch.setenv("FAKE_NO_REPLACE", "refused")
    assert lab.main(["probe", "--task", task["_file"]]) == 0
    out = capsys.readouterr().out
    assert "ok    task_folder_swap_conclusive" in out and "blocked by the sandbox" in out


def test_probe_catches_a_guard_without_the_task_output_rule(task, monkeypatch, capsys, tmp_path):
    # the guard of a0-pk-1: the own background output was refused
    _guard_from(tmp_path, monkeypatch, spec_drop="task_output_root")
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_30_tool_read_own_task_output  (refused" in out
    assert "FAIL  step_30_reads_the_output_of_step_28" in out and "boundary BROKEN" in out


def test_probe_step_30_must_read_the_file_step_28_named(task, monkeypatch, capsys, tmp_path):
    other = tmp_path / "w" / "elsewhere.output"
    monkeypatch.setenv("FAKE_TASK_READ", str(other))
    assert lab.main(["probe", "--task", task["_file"]]) == 1
    assert "FAIL  step_30_reads_the_output_of_step_28" in capsys.readouterr().out


def test_probe_fails_when_a_step_is_skipped(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SKIP", "9")
    assert lab.cmd_probe(argparse.Namespace(task=task["_file"])) == 1
    out = capsys.readouterr().out
    assert "FAIL  step_9_bash_network  (not attempted)" in out and "boundary BROKEN" in out


def test_two_jev_contexts_on_one_stop_are_a_problem():
    rules_ = [{"id": "s-1", "rule": "R1_tests", "state": "pass"}]
    recs = [{"kind": "context", "ctx_id": "x", "rules": rules_}, {"kind": "context", "ctx_id": "y", "rules": rules_},
            {"kind": "jev", "ctx_id": "x", "status": "ok"}, {"kind": "jev", "ctx_id": "y", "status": "ok"}]
    _, problems = lab.summarize_jev(recs, [{"kind": "decision", "id": "s-1", "stop_ts": 1.0}], True)
    assert any("more than one jev context" in p for p in problems)


def test_advisor_model_must_be_named_in_the_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    msg = lambda its: {"type": "assistant", "message": {"usage": {"iterations": its}}}  # noqa: E731
    t.write_text("\n".join(json.dumps(x) for x in [
        msg([{"type": "message", "model": "claude-sonnet-5-5"}]), msg([{"type": "advisor_message"}])]))
    assert lab.advisor_models_in_transcript(str(t)) == [None]          # unnamed: never filled in from config
    t.write_text(json.dumps(msg([{"type": "advisor_message", "model": "claude-opus-5-5"}])))
    assert lab.advisor_models_in_transcript(str(t)) == ["claude-opus-5-5"]


def test_config_change_breaks_the_batch_invariant(task):
    expect = lab.batch_expect(task, lab.load_modes()[1])
    (lab.lab_root() / "config" / "settings.json").write_text('{"theme": "dark"}')
    r = lab.attempt(task, "A", expect=expect)
    assert r["session_status"] == "not_started" and any("config_fingerprint" in p for p in r["integrity"])


def test_permission_denials_in_a_run_make_it_invalid(task, monkeypatch):
    monkeypatch.setenv("FAKE_DENY", "1")
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("permission denial" in p for p in r["integrity"])
    assert r["config"]["permission_mode"] == "acceptEdits" and r["loaded"]["permissionMode"] == "acceptEdits"


# --- review 3: nothing reaches the test run before the sandbox does ------------------------------------

def test_link_out_of_the_snapshot_is_never_copied_or_run(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("runner secret")
    snap = tmp_path / "snap"
    snap.mkdir()
    os.symlink(outside, snap / "data")
    os.symlink("/etc/hosts", snap / "hosts")
    marker = tmp_path / "ran"
    r = lab.run_suite("hidden", f"touch {marker}; cat data/secret.txt; echo 'Ran 1 test in 0s'; echo; echo OK",
                      snap, tmp_path, work_base=tmp_path)
    assert r["ok"] is False and r["sandbox"] == "not_run" and "data ->" in r["refused"] and "hosts ->" in r["refused"]
    assert not marker.exists() and "runner secret" not in r["tail"]
    assert not any(p.name.startswith("_check-") for p in tmp_path.iterdir())     # nothing was even copied


def test_links_inside_the_snapshot_stay_links(tmp_path):
    snap = tmp_path / "snap"
    (snap / "pkg").mkdir(parents=True)
    (snap / "pkg" / "real.py").write_text("x = 1\n")
    os.symlink("real.py", snap / "pkg" / "alias.py")
    r = lab.run_suite("public", "test -L pkg/alias.py && echo 'Ran 1 test in 0s' && echo && echo OK", snap, tmp_path,
                      work_base=tmp_path)
    assert r["ok"] and lab.escaping_links(snap) == []


def test_no_sandbox_means_no_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARMSCOPE_LAB_UNSANDBOXED_TESTS", raising=False)
    monkeypatch.setattr(lab.shutil, "which", lambda name: None)                 # no sandbox-exec
    marker = tmp_path / "ran"
    d = tmp_path / "check"
    d.mkdir()
    r, kind = lab.isolated(f"touch {marker}", d, 10)
    assert kind == "unavailable" and r["code"] is None and not marker.exists()


def test_symlink_changes_are_in_the_diff(tmp_path):
    base, snap, run = tmp_path / "base", tmp_path / "snap", tmp_path / "run"
    for d in (base, snap, run):
        d.mkdir()
    (base / "a.py").write_text("x = 1\n")
    os.symlink("/etc/passwd", snap / "a.py")
    d = lab.final_diff(base, snap, run)
    assert [f["path"] for f in d["files"]] == ["a.py"] and "symlink -> /etc/passwd" in (run / "final.diff").read_text()


# --- per-test outcomes for task calibration (4b) -------------------------------------------------------------

VERBOSE_311 = """test_a (__main__.Hidden.test_a) ... ok
test_b (__main__.Hidden.test_b) ... FAIL
test_c (__main__.Hidden.test_c) ... ERROR
test_d (__main__.Hidden.test_d) ... skipped 'later'

----------------------------------------------------------------------
Ran 4 tests in 0.01s

FAILED (failures=1, errors=1, skipped=1)
"""
VERBOSE_39 = """test_a (__main__.Hidden) ... ok
test_b (__main__.Hidden)
What b checks. ... FAIL
test_c (pkg.mod.Other) ... expected failure

----------------------------------------------------------------------
Ran 3 tests in 0.01s

FAILED (failures=1, expected failures=1)
"""


@pytest.mark.parametrize("text,expected", [
    (VERBOSE_311, {"__main__.Hidden.test_a": "ok", "__main__.Hidden.test_b": "FAIL", "__main__.Hidden.test_c": "ERROR",
                   "__main__.Hidden.test_d": "skipped"}),
    (VERBOSE_39, {"__main__.Hidden.test_a": "ok", "__main__.Hidden.test_b": "FAIL",
                  "pkg.mod.Other.test_c": "expected failure"}),
    ("..F\n----\nRan 3 tests in 0.01s\n\nFAILED (failures=1)\n", {}),
    # subTest, 3.9: the failing test has no status of its own, the next test is printed on the same line
    ("test_a (__main__.T) ... test_b (__main__.T) ... ok\n\n" + "=" * 70 + "\nFAIL: test_a (__main__.T) (i=1)\n"
     + "-" * 70 + "\nTraceback\n\nRan 2 tests in 0.0s\n\nFAILED (failures=2)\n",
     {"__main__.T.test_a": "FAIL", "__main__.T.test_b": "ok"}),
    # subTest, 3.11+: an empty status line, then one indented line per failing subtest
    ("test_a (__main__.T.test_a) ... \n  test_a (__main__.T.test_a) (i=1) ... FAIL\ntest_b (__main__.T.test_b) ... ok\n"
     "\n" + "=" * 70 + "\nFAIL: test_a (__main__.T.test_a) (i=1)\n" + "-" * 70 + "\nTraceback\n\nRan 2 tests in 0.0s\n\n"
     "FAILED (failures=2)\n", {"__main__.T.test_a": "FAIL", "__main__.T.test_b": "ok"}),
])
def test_parse_cases_reads_both_unittest_formats(text, expected):
    assert lab.parse_cases(text) == expected


SUBTESTS = '''import unittest
class T(unittest.TestCase):
    def test_a(self):
        for i in range(3):
            with self.subTest(i=i):
                self.assertEqual(i, 0)
    def test_b(self):
        pass
unittest.main(verbosity=2)
'''


def test_hidden_suite_records_which_tests_failed_and_subtests_do_not_go_negative(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "t.py").write_text(SUBTESTS)
    r = lab.run_suite("hidden", f"{sys.executable} t.py", snap, tmp_path, expected=2, work_base=tmp_path)
    # unittest says "Ran 2 tests ... FAILED (failures=2)": two subtests of one test; ran - failed would be 0 here
    # and below 0 with a third failing subtest, so count passes per test instead
    assert r["tests"] == 2 and r["passed"] == 1 and r["ok"] is False
    assert r["cases"] == {"__main__.T.test_a": "FAIL", "__main__.T.test_b": "ok"}
    assert r["not_passed"] == ["__main__.T.test_a"]


def _rj(run_id, task="semver", mode="A", valid=True, success=False, passed=5, not_passed=(), cost=0.1, probe=False,
        batch="b1", task_sha="t1", finished=True, status="success"):
    r = {"run_id": run_id, "task_id": task, "mode": mode, "valid": valid, "success": success, "session_status": status,
         "plan": {"probe": True} if probe else {"batch_id": batch}, "costs": {"claude_code_total_usd": cost},
         "task": {"sha256": task_sha, "hidden_tests_sha256": "h1"}, "versions": {"modes_file_sha256": "m1",
                                                                                "claude_code": "2.1.326"},
         "config": {"policy_sha256": "p1", "model": "claude-sonnet-5-5", "advisor": "claude-opus-5-5",
                    "effort_requested": "high"},
         "result": {"is_error": False, "modelUsage": {"claude-sonnet-5-5": {}}},
         "tests": {"hidden": {"passed": passed, "expected": 7, "not_passed": list(not_passed)}}}
    if finished:
        r.update(finished_at="2026-11-17T19:00:00+0200", verdict={"task_passed": success})
    return r


def test_calibration_counts_valid_runs_per_task_and_failing_tests():
    runs = [_rj("r1", success=True, passed=7),
            _rj("r2", passed=5, not_passed=["m.Hidden.test_caret", "m.Hidden.test_prerelease"]),
            _rj("r3", passed=6, not_passed=["m.Hidden.test_prerelease"], status="timeout"),   # a valid timeout counts
            _rj("r4", valid=False, success=True, passed=7),
            _rj("r5", mode="B", success=True, passed=7),
            _rj("r6", task="cron", probe=True)]
    t = lab.calibration(runs, "A")
    assert len(t) == 1
    s = next(iter(t.values()))
    assert s["task_id"] == "semver"
    assert (s["valid"], s["invalid"], s["success"], s["passed"], s["expected"]) == (3, 1, 1, [7, 5, 6], 7)
    assert s["not_passed"] == {"m.Hidden.test_caret": 1, "m.Hidden.test_prerelease": 2}
    assert s["runs"] == ["r1", "r2", "r3"] and s["batches"] == ["b1"]


def test_calibration_keeps_experiment_versions_apart_and_skips_unfinished_records():
    """Review of PR #3: same task_id with another task/tests/modes hash must not be pooled; a record still marked
    running (or left behind by a dead runner) is not an attempt."""
    runs = [_rj("r1", success=True, passed=7), _rj("r2", task_sha="t2", passed=3, batch="b2"),
            _rj("r3", success=True, passed=7, finished=False, status="running")]
    t = lab.calibration(runs, "A")
    assert sorted((v["task_id"], v["runs"]) for v in t.values()) == [("semver", ["r1"]), ("semver", ["r2"])]
    assert [v["runs"] for v in lab.calibration(runs, "A", batch="b2").values()] == [["r2"]]


def test_calib_command_prints_per_task_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARMSCOPE_LAB", str(tmp_path))
    for rid, sha in (("abc", "t1"), ("def", "t2")):
        d = tmp_path / "runs" / rid
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps(_rj(rid, task="cron", passed=9, cost=0.25, task_sha=sha,
                                                   not_passed=["__main__.Hidden.test_star"])))
    assert lab.main(["calib"]) == 0
    out = capsys.readouterr().out
    assert out.count("cron@") == 2 and "0/1 success" in out and "[9] of 7" in out and "$0.250" in out
    assert "1x __main__.Hidden.test_star" in out and "! cron has 2 experiment versions" in out


def test_calibration_ignores_old_runs_that_never_reached_the_model():
    old = _rj("4df49f778f", passed=0, not_passed=["__main__.Hidden.test_or"], status="success:is_error")
    old["result"] = {"subtype": "success", "is_error": True, "total_cost_usd": 0, "modelUsage": {}}
    t = next(iter(lab.calibration([old], "A").values()))
    assert (t["valid"], t["invalid"], t["not_passed"]) == (0, 1, {})


@pytest.mark.parametrize("result,limits,expected", [
    ({"is_error": False, "modelUsage": {"s": {}}}, [{"rate_limit_info": {"status": "rejected"}}], None),
    ({"is_error": True, "modelUsage": {}, "terminal_reason": "api_error"}, [], "api_error"),
    ({"is_error": True, "modelUsage": {"s": {}}, "terminal_reason": "api_error"}, [], "api_interrupted"),
    ({"is_error": True, "modelUsage": {"s": {}}, "terminal_reason": "max_turns"}, [], None),
    (None, [{"rate_limit_info": {"status": "rejected"}}], None),
])
def test_api_failure_is_decided_by_the_terminal_result(result, limits, expected):
    got = lab.api_failure(result, limits)
    assert (got[0] if got else None) == expected


def test_rejected_event_in_a_finished_session_does_not_invalidate_it(task, monkeypatch):
    monkeypatch.setenv("FAKE_RATE_LIMIT", "warn")
    r = lab.attempt(task, "A")
    assert r["session_status"] == "success" and r["valid"] is True
    assert r["rate_limit_events"][0]["status"] == "rejected"


def test_api_failure_after_work_began_is_recorded_apart_and_stops_the_batch(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RATE_LIMIT", "mid")
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A", "--repeats", "2", "--seed", "1"]) == 1
    out = capsys.readouterr().out
    assert "stopping the batch: api_interrupted" in out and out.count("session running") == 1
    rj = json.loads(next((lab.lab_root() / "runs").glob("*/run.json")).read_text())
    assert rj["valid"] is False and any("after work had begun" in p for p in rj["integrity"])


def test_same_test_name_in_two_modules_stays_apart():
    text = ("test_x (pkg.a.Hidden.test_x) ... ok\ntest_x (pkg.b.Hidden.test_x) ... FAIL\n\n" + "=" * 70
            + "\nFAIL: test_x (pkg.b.Hidden.test_x)\n\nRan 2 tests in 0.0s\n\nFAILED (failures=1)\n")
    assert lab.parse_cases(text) == {"pkg.a.Hidden.test_x": "ok", "pkg.b.Hidden.test_x": "FAIL"}


def _tiny_task(root, mutant_new):
    t = root / "tiny"
    (t / "base" / "src").mkdir(parents=True)
    (t / "base" / "tests").mkdir()
    (t / "base" / "tests" / "__init__.py").write_text("")
    (t / "base" / "src" / "m.py").write_text("def f(x):\n    raise NotImplementedError\n")
    (t / "base" / "tests" / "test_m.py").write_text(
        "import unittest\nfrom m import f\nclass T(unittest.TestCase):\n    def test_one(self):\n"
        "        self.assertEqual(f(1), 2)\n")
    (t / "ref" / "src").mkdir(parents=True)
    (t / "ref" / "src" / "m.py").write_text("def f(x):\n    return x * 2\n")
    (t / "hidden").mkdir()
    (t / "hidden" / "test_hidden.py").write_text(
        "import unittest\nfrom m import f\nclass Hidden(unittest.TestCase):\n    def test_zero(self):\n"
        "        self.assertEqual(f(0), 0)\n    def test_neg(self):\n        self.assertEqual(f(-3), -6)\n"
        "unittest.main(verbosity=2)\n")
    (t / "task.json").write_text(json.dumps({"id": "tiny", "mutants": [
        {"name": "m1", "edits": [{"file": "src/m.py", "old": "return x * 2", "new": mutant_new}]}]}))
    manifest = {"public_tests": f"PYTHONPATH=src {sys.executable} -m unittest discover -s tests -t .",
                "hidden_tests": {"cmd": f"PYTHONPATH=src {sys.executable} {{hidden}}/test_hidden.py",
                                 "expected_tests": 2}}
    return t, manifest


def test_task_repos_are_deterministic_and_validation_catches_a_weak_hidden_suite(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "lab", "tasks"))
    import build_tasks
    t, manifest = _tiny_task(tmp_path, "return x + x if x else 0")     # equivalent: no hidden test can catch it
    a = build_tasks.build_repo(t / "base", tmp_path / "r1")
    b = build_tasks.build_repo(t / "base", tmp_path / "r2")
    assert a == b
    assert any("tiny/m1" in p for p in build_tasks.validate(t, manifest))
    t2, manifest = _tiny_task(tmp_path / "second", "return x * 2 if x > 0 else x")   # wrong for -3: caught
    assert build_tasks.validate(t2, manifest) == []


def test_rate_limited_session_is_invalid_and_stops_the_batch(task, monkeypatch, capsys):
    """17.11: with the 5-hour window used up, Claude Code exits in ~3 s with is_error and $0. That is no attempt
    at the task: it must not count as a failed A run (0/11), and the batch must not burn through the rest."""
    monkeypatch.setenv("FAKE_RATE_LIMIT", "1")
    r = lab.attempt(task, "A")
    assert r["valid"] is False and r["success"] is False and r["session_status"] == "api_error"
    assert any("rate limit rejected (five_hour" in p for p in r["integrity"])
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A", "--repeats", "3", "--seed", "1"]) == 1
    out = capsys.readouterr().out
    assert "stopping the batch: api_error" in out and out.count("session running") == 2      # one attempt per call


# --- A0: Sonnet without the advisor -------------------------------------------------------------------------

def test_a0_runs_without_the_advisor_flag_and_records_null(task, monkeypatch):
    modes, _ = lab.load_modes()
    assert lab.advisor_for(modes, "A") == "claude-opus-5-5" and lab.advisor_for(modes, "A0") is None
    assert "--advisor" in lab.claude_cmd(modes, "s.json", "A")
    assert "--advisor" not in lab.claude_cmd(modes, "s.json", "A0")
    monkeypatch.setenv("FAKE_VOLUNTARY_ADVISOR", "1")     # would call it if it could: without --advisor it cannot
    r = lab.attempt(task, "A0")
    assert r["valid"] is True, r["integrity"]
    assert r["config"]["advisor"] is None and "--advisor" not in r["config"]["argv"]
    assert r["advisor"]["called"] == 0 and r["models_seen"] == ["claude-sonnet-5-5"]


def test_a0_run_that_still_reaches_the_advisor_is_invalid(task, monkeypatch):
    """If something (a profile advisorModel, a Claude Code default) gives the agent an advisor anyway, the A0 run
    is not an A0 run."""
    monkeypatch.setenv("FAKE_VOLUNTARY_ADVISOR", "1")
    monkeypatch.setenv("FAKE_PROFILE_ADVISOR", "claude-opus-5-5")
    r = lab.attempt(task, "A0")
    assert r["valid"] is False
    assert any("unexpected model ran: claude-opus-5-5" in p for p in r["integrity"])
    assert any("advisor called 1 time(s) in a mode without an advisor" in p for p in r["integrity"])


def test_a_with_a_voluntary_advisor_call_stays_valid(task, monkeypatch):
    monkeypatch.setenv("FAKE_VOLUNTARY_ADVISOR", "1")
    r = lab.attempt(task, "A")
    assert r["valid"] is True, r["integrity"]
    assert r["advisor"]["called"] == 1 and r["advisor"]["voluntary"] == 1 and r["config"]["advisor"] == "claude-opus-5-5"


def test_a0_and_a_are_different_experiments_for_calib():
    a, a0 = _rj("x"), _rj("y")
    a0["config"]["advisor"] = None
    assert lab.experiment_key(a) != lab.experiment_key(a0)
# --- pytest suites (historical-bug tasks: packaging, Click) -------------------------------------------------

@pytest.mark.parametrize("text,tests,failed,passed,ids", [
    ("....F\nFAILED tests/test_a.py::test_x[1-2] - assert 1 == 2\n4 failed, 63746 passed, 444 deselected in 41.25s\n",
     63750, 4, 63746, ["tests/test_a.py::test_x[1-2]"]),
    ("=== short test summary info ===\nFAILED t.py::T::test_b - AssertionError\nERROR t.py::test_c - KeyError\n"
     "==== 1 failed, 2 passed, 24 skipped, 1 xfailed, 1 error, 3 warnings in 0.50s ====\n",
     4, 2, 2, ["t.py::T::test_b", "t.py::test_c"]),
    ("ERROR tests/test_deprecations.py\n!!!!!! Interrupted: 1 error during collection !!!!!!\n"
     "31000 deselected, 1 error in 2.09s\n", 1, 1, 0, ["tests/test_deprecations.py"]),
    ("2083 passed, 24 skipped, 31000 deselected, 1 xfailed in 4.87s\n", 2083, 0, 2083, []),
    # parametrized ids with spaces and brackets in the message
    ("FAILED tests/t.py::test_l[(LICENSEREF-Foo OR licenseref-foo) AND MIT-x] - AssertionError: assert [1] == [2]\n"
     "FAILED tests/t.py::test_o[envvar1-'--units' (env var: 'UNITS')] - assert 2 == 2\nFAILED tests/t.py::test_p[a b]\n"
     "3 failed, 5 passed in 0.1s\n", 8, 3, 5,
     ["tests/t.py::test_l[(LICENSEREF-Foo OR licenseref-foo) AND MIT-x]", "tests/t.py::test_o[envvar1-'--units' (env var: 'UNITS')]",
      "tests/t.py::test_p[a b]"]),
])
def test_parse_pytest_summary_and_failing_ids(text, tests, failed, passed, ids):
    p = lab.parse_pytest(text)
    assert (p["tests"], p["failed"], p["passed"], p["not_passed"]) == (tests, failed, passed, ids)


def test_parse_pytest_needs_a_summary():
    assert lab.parse_pytest("no tests ran in 0.01s\n") is None
    assert lab.parse_pytest("Traceback (most recent call last):\nKeyboardInterrupt\n") is None


PYTEST_SUITE = '''import pytest
def test_ok(): pass
@pytest.mark.parametrize("x", [1, 2])
def test_param(x): assert x == 1
@pytest.mark.skip(reason="platform")
def test_skipped(): pass
'''


@pytest.mark.skipif(importlib.util.find_spec("pytest") is None, reason="needs pytest")
def test_hidden_pytest_suite_counts_and_ids(tmp_path):
    snap = tmp_path / "snap"
    (snap / "tests").mkdir(parents=True)
    (snap / "tests" / "test_p.py").write_text(PYTEST_SUITE)
    r = lab.run_suite("hidden", f"{sys.executable} -m pytest -q -p no:cacheprovider tests", snap, tmp_path,
                      expected=3, work_base=tmp_path)
    assert r["framework"] == "pytest" and r["report"] is True and r["ok"] is False
    assert (r["tests"], r["passed"], r["not_passed"]) == (3, 2, ["tests/test_p.py::test_param[2]"])
    assert r["pytest_counts"]["skipped"] == 1
    (snap / "tests" / "test_p.py").write_text(PYTEST_SUITE.replace("assert x == 1", "assert x"))
    r = lab.run_suite("hidden", f"{sys.executable} -m pytest -q -p no:cacheprovider tests", snap, tmp_path,
                      expected=3, work_base=tmp_path)
    assert r["ok"] is True and r["passed"] == 3 and r["not_passed"] == []


def test_task_can_raise_the_test_suite_timeout(task, monkeypatch):
    seen = []
    real = lab.run_suite

    def spy(name, cmd, *a, **kw):
        seen.append((name, kw.get("timeout")))
        return real(name, cmd, *a, **kw)
    monkeypatch.setattr(lab, "run_suite", spy)
    lab.attempt(dict(task, tests_timeout_s=900), "A")
    lab.attempt(task, "A")
    assert seen[:2] == [("public", 900), ("hidden", 900)] and seen[2:] == [("public", 300), ("hidden", 300)]


# --- no web: WebFetch/WebSearch denied everywhere, and any use invalidates the run ------------------------------

def test_settings_deny_web_tools(task):
    r = lab.attempt(task, "A")
    settings = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())
    assert {"WebFetch", "WebSearch"} <= set(settings["permissions"]["deny"])
    assert r["valid"] is True and r["observation"]["web_tool_uses"] == 0


@pytest.mark.parametrize("where,tool", [("main", "WebFetch"), ("sub", "WebSearch")])
def test_web_tool_use_in_session_or_subagent_invalidates_the_run(task, monkeypatch, where, tool):
    monkeypatch.setenv("FAKE_WEB", where)
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any(f"web tool used 1 time(s): ['{tool}']" in p for p in r["integrity"])


def test_hidden_bug_tests_and_regressions_are_reported_apart(task, monkeypatch):
    """Historical tasks: the base fails some known tests (the bug); the rest of the fix's suite checks regressions."""
    spy_real = lab.run_suite

    def fake_suite(name, cmd, *a, **kw):
        r = spy_real(name, cmd, *a, **kw)
        if name == "hidden":
            r.update(report=True, ok=False, not_passed=["t.py::test_bug[2]", "t.py::test_other"])
        return r
    monkeypatch.setattr(lab, "run_suite", fake_suite)
    t = json.loads(json.dumps(task))
    t["hidden_tests"]["bug_tests"] = ["t.py::test_bug[1]", "t.py::test_bug[2]"]
    r = lab.attempt(t, "A")
    h = r["tests"]["hidden"]
    assert h["bug_tests"] == {"total": 2, "not_passed": ["t.py::test_bug[2]"]}
    assert h["regressions"] == ["t.py::test_other"]
    c = next(iter(lab.calibration([r], "A").values()))
    assert c["bug_fixed"] == [False] and c["regressions"] == [1]


def test_screen_runs_batches_one_after_another(task, capsys):
    second = Path(task["_file"]).with_name("task2.json")
    second.write_text(json.dumps(dict(json.loads(Path(task["_file"]).read_text()), task_id="t2")))
    assert lab.main(["screen", "--tasks", f"{task['_file']},{second}", "--seed", "3"]) == 0
    out = capsys.readouterr().out
    assert out.count("started ") == 2 and out.count("VALID") == 2 and "2/2 batches all valid" in out
    batches = list((lab.lab_root() / "batches").glob("*.json"))
    assert len(batches) == 2 and len({b.name for b in batches}) == 2


def test_screen_stops_starting_batches_after_an_api_failure(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RATE_LIMIT", "1")
    assert lab.main(["screen", "--tasks", ",".join([task["_file"]] * 3), "--seed", "3", "--jobs", "1"]) == 1
    out = capsys.readouterr().out
    assert out.count("started ") == 1 and "not started:" in out


# --- review of PR #5: isolation of runs side by side, strict validation of historical tasks -----------------------

def test_check_copies_live_apart_and_are_denied_to_the_agent(task, monkeypatch):
    roots = []
    real = lab.run_suite

    def spy(name, cmd, snapshot, run_dir, **kw):
        roots.append(kw.get("work_base"))
        return real(name, cmd, snapshot, run_dir, **kw)
    monkeypatch.setattr(lab, "run_suite", spy)
    r = lab.attempt(task, "A")
    modes, _ = lab.load_modes()
    check, work = lab.check_root(modes), lab.work_root(modes)
    assert roots and all(Path(x) == check for x in roots) and check != work and not str(check).startswith(str(work) + "/")
    st = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())
    assert f"Read(/{check}/**)" in st["permissions"]["deny"] and f"Edit(/{check}/**)" in st["permissions"]["deny"]
    fs = st["sandbox"]["filesystem"]
    assert str(check) in fs["denyRead"] and str(work) in fs["denyRead"]          # siblings closed to Bash ...
    assert any(a.startswith(str(work) + "/") for a in fs["allowRead"])         # ... its own dir re-opened


def test_screen_refuses_sessions_side_by_side(task, capsys):
    """Review of PR #5: the Read tool is not kept out of sibling work dirs, so no parallel sessions for now."""
    assert lab.main(["screen", "--tasks", task["_file"], "--seed", "3", "--jobs", "2"]) == 2
    out = capsys.readouterr().out
    assert "--jobs > 1 is disabled" in out and "started" not in out


@pytest.mark.skipif(importlib.util.find_spec("pytest") is None, reason="needs pytest")
def test_historical_validation_needs_a_full_report_failing_exactly_the_bug_tests(tmp_path):
    """Review of PR #5: a base that fails before the hidden tests run (ImportError, no report) used to pass, since
    an empty list of failures is a subset of the bug tests."""
    sys.path.insert(0, os.path.join(ROOT, "lab", "tasks"))
    import build_tasks
    t = tmp_path / "hist"
    (t / "base" / "src").mkdir(parents=True)
    (t / "base" / "tests").mkdir()
    (t / "base" / "src" / "m.py").write_text("def f(x):\n    return x\n")
    (t / "base" / "tests" / "test_old.py").write_text("from m import f\ndef test_one():\n    assert f(1) == 1\n")
    (t / "ref" / "src").mkdir(parents=True)
    (t / "ref" / "src" / "m.py").write_text("def f(x):\n    return abs(x)\n")
    (t / "hidden" / "tests").mkdir(parents=True)
    (t / "hidden" / "tests" / "test_old.py").write_text((t / "base" / "tests" / "test_old.py").read_text())
    (t / "hidden" / "tests" / "test_bug.py").write_text("from m import f\ndef test_neg():\n    assert f(-2) == 2\n")
    (t / "task.json").write_text(json.dumps({"id": "hist", "kind": "historical",
                                             "bug_tests": ["tests/test_bug.py::test_neg"]}))
    py = f"PYTHONPATH=src {sys.executable} -m pytest -q -p no:cacheprovider tests"
    good = {"public_tests": py, "hidden_tests": {"expected_tests": 2,
                                                  "cmd": "rm -rf tests && cp -R {hidden}/tests tests && " + py}}
    assert build_tasks.validate_historical(t, good) == []
    broken = json.loads(json.dumps(good))                    # fails before pytest: no report at all
    broken["hidden_tests"]["cmd"] = f"{sys.executable} -c 'import no_such_module' && " + good["hidden_tests"]["cmd"]
    assert any("hist/base" in p for p in build_tasks.validate_historical(t, broken))
    wrong = json.loads(json.dumps(good))                     # bug tests declared but another test fails instead
    (t / "task.json").write_text(json.dumps({"id": "hist", "kind": "historical",
                                             "bug_tests": ["tests/test_bug.py::test_neg", "tests/test_old.py::test_one"]}))
    assert any("hist/base" in p for p in build_tasks.validate_historical(t, wrong))


@pytest.mark.skipif(importlib.util.find_spec("pytest") is None, reason="needs pytest")
def test_feature_validation_allows_old_tests_the_feature_changes(tmp_path):
    """A feature may change behaviour the base's own tests asserted: the reference only has to pass the feature's
    suite. The base has to fail it (here at import, since the new function does not exist yet)."""
    sys.path.insert(0, os.path.join(ROOT, "lab", "tasks"))
    import build_tasks
    t = tmp_path / "feat"
    (t / "base" / "src").mkdir(parents=True)
    (t / "base" / "tests").mkdir()
    (t / "base" / "src" / "m.py").write_text("def f(x):\n    raise TypeError(x)\n")
    (t / "base" / "tests" / "test_old.py").write_text(
        "import pytest\nfrom m import f\ndef test_rejects():\n    with pytest.raises(TypeError):\n        f(1)\n")
    (t / "ref" / "src").mkdir(parents=True)
    (t / "ref" / "src" / "m.py").write_text("def f(x):\n    return x\ndef g():\n    return 2\n")
    (t / "hidden" / "tests").mkdir(parents=True)
    (t / "hidden" / "tests" / "test_new.py").write_text(
        "from m import f, g\ndef test_f():\n    assert f(1) == 1\ndef test_g():\n    assert g() == 2\n")
    (t / "task.json").write_text(json.dumps({"id": "feat", "kind": "historical", "feature": True,
                                             "bug_tests": ["tests/test_new.py::test_f", "tests/test_new.py::test_g"]}))
    py = f"PYTHONPATH=src {sys.executable} -m pytest -q -p no:cacheprovider tests"
    m = {"public_tests": py, "hidden_tests": {"expected_tests": 2,
                                               "cmd": "rm -rf tests && cp -R {hidden}/tests tests && " + py}}
    assert build_tasks.validate_historical(t, m) == []
    (t / "task.json").write_text(json.dumps({"id": "feat", "kind": "historical",
                                             "bug_tests": ["tests/test_new.py::test_f", "tests/test_new.py::test_g"]}))
    assert any("feat/" in p for p in build_tasks.validate_historical(t, m))   # as a bug fix it is not valid


def test_mode_can_set_its_own_effort(task):
    modes, _ = lab.load_modes()
    assert lab.effort_for(modes, "A") == "high" and lab.effort_for(modes, "Am") == "medium"
    cmd = lab.claude_cmd(modes, "s.json", "Am")
    assert cmd[cmd.index("--effort") + 1] == "medium" and "--advisor" in cmd
    r = lab.attempt(task, "Am")
    assert r["config"]["effort_requested"] == "medium" and r["valid"] is True, r["integrity"]
    a = lab.attempt(task, "A")
    assert lab.experiment_key(a) != lab.experiment_key(r)


# --- pilot: cost of B against A, in pairs ----------------------------------------------------------------------

def only_row(report, task_id):
    rows = [t for t in report["rows"].values() if t["task_id"] == task_id]
    assert len(rows) == 1, list(report["rows"])
    return rows[0]


def test_paired_batch_orders_each_pair_on_its_own(task):
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "3", "--seed", "7",
                     "--paired"]) == 0
    m = json.loads(next((lab.lab_root() / "batches").glob("*.json")).read_text())
    assert m["paired"] is True
    pairs = [m["order"][i:i + 2] for i in range(0, 6, 2)]
    assert all(sorted(x[0] for x in p) == ["A", "B"] and p[0][1] == p[1][1] for p in pairs)
    assert [p[0][1] for p in pairs] == [1, 2, 3]
    report = lab.pair_report([json.loads(f.read_text()) for f in (lab.lab_root() / "runs").glob("*/run.json")])
    t = only_row(report, task["task_id"])
    assert len(t["pairs"]) == 3 and t["invalid_pairs"] == 0
    # the fake agent consults the advisor only when B blocks a Stop: B costs more in every pair
    assert all(x["diff_usd"] > 0 for x in t["pairs"]) and t["mean_diff_usd"] > 0
    assert all(x["B"]["opus_usd"] and not x["A"]["opus_usd"] for x in t["pairs"])


def test_quadruple_batch_gives_pairs_for_any_two_of_its_modes(task, monkeypatch, jev_stub):
    # pilot b-jev-1: A, B, BJ and B12 in each repeat, compared two at a time
    monkeypatch.setenv("JEV_STUB_P", "0.8")
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B,BJ,B12", "--repeats", "2", "--seed", "3",
                     "--paired"]) == 0
    m = json.loads(next((lab.lab_root() / "batches").glob("*.json")).read_text())
    quads = [m["order"][i:i + 4] for i in range(0, 8, 4)]
    assert all(sorted(x[0] for x in q) == ["A", "B", "B12", "BJ"] and {x[1] for x in q} == {i + 1}
               for i, q in enumerate(quads))
    runs = [json.loads(f.read_text()) for f in (lab.lab_root() / "runs").glob("*/run.json")]
    for modes in (("B", "BJ"), ("BJ", "B12"), ("A", "BJ")):
        t = only_row(lab.pair_report(runs, modes=modes), task["task_id"])
        assert len(t["pairs"]) == 2 and t["invalid_pairs"] == 0, modes
    # Jev says yes: BJ consults like B, B12 never does
    t = only_row(lab.pair_report(runs, modes=("BJ", "B12")), task["task_id"])
    assert all(x["BJ"]["opus_usd"] and not x["B12"]["opus_usd"] for x in t["pairs"])


def test_pairs_pilot_reports_only_its_tagged_batches_and_refuses_an_empty_selection(task, tmp_path, capsys):
    # review of f03edc5: with an empty "batches" list, pairs --pilot reported over every batch
    decl = tmp_path / "p.json"
    decl.write_text(json.dumps({"pilot": "b-jev-1", "batches": []}))
    assert lab.main(["pairs", "--pilot", str(decl), "--modes", "A,B"]) == 2      # nothing tagged yet
    assert "refusing" in capsys.readouterr().out
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "1",
                     "--paired"]) == 0                                              # not the pilot's
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "2",
                     "--paired", "--pilot", "b-jev-1"]) == 0
    tagged = [m for m in lab.load_manifests().values() if m.get("pilot") == "b-jev-1"]
    assert len(tagged) == 1 and tagged[0]["seed"] == 2
    capsys.readouterr()
    assert lab.main(["pairs", "--pilot", str(decl), "--modes", "A,B", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {x["batch"] for row in out["rows"].values() for x in row["pairs"]} == {tagged[0]["batch_id"]}
    with pytest.raises(SystemExit):                                                # a typo in the name
        lab.main(["batch", "--task", task["_file"], "--modes", "A", "--repeats", "1", "--seed", "3",
                  "--pilot", "b-jev-l"])


def test_pair_report_counts_only_valid_pairs_and_every_valid_run_cost():
    def rj(mode, rep_, usd, valid=True, success=True, batch="p1"):
        return {"run_id": f"{mode}{rep_}{batch}", "task_id": "pk-x", "mode": mode, "valid": valid, "success": success,
                "plan": {"paired": True, "batch_id": batch, "repeat": rep_}, "finished_at": "x", "verdict": {"task_passed": success},
                "costs": {"claude_code_total_usd": usd, "by_model": {"lead": {"usd": usd}}},
                "session": {"wall_s": 60}, "tests": {"hidden": {"regressions": []}},
                "decisions": {"stops": 2 if mode == "B" else 1, "blocks": 1 if mode == "B" else 0},
                "jev": {"answers": [{"would_consult": mode == "B"}]}}
    runs = [rj("A", 1, 0.10), rj("B", 1, 0.30), rj("A", 2, 0.20, success=False), rj("B", 2, 0.25),
            rj("A", 3, 0.10), rj("B", 3, 0.50, valid=False), rj("A", 4, 0.1, batch="other")]
    t = only_row(lab.pair_report(runs), "pk-x")
    assert len(t["pairs"]) == 2 and t["invalid_pairs"] == 1
    assert round(t["mean_diff_usd"], 3) == round(((0.30 - 0.10) + (0.25 - 0.20)) / 2, 3)
    assert (t["A_success"], t["B_success"]) == (1, 2)
    assert t["pairs"][0]["B"]["extra_stops"] == 1 and t["pairs"][0]["B"]["first_stop_would_consult"] is True



def test_transient_api_failure_is_retried_once_and_the_batch_goes_on(task, monkeypatch, tmp_path, capsys):
    """18.11 night: one api_interrupted run (rate limits allowed) stopped the pilot after its first task."""
    monkeypatch.setenv("FAKE_FLAKY_FILE", str(tmp_path / "flaky"))
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "1",
                     "--paired"]) == 0
    out = capsys.readouterr().out
    assert "retrying" in out and "stopping the batch" not in out
    m = json.loads(next((lab.lab_root() / "batches").glob("*.json")).read_text())
    assert [x.get("replaced_by_retry", False) for x in m["runs"]] == [True, False, False] and m["valid"] is True
    runs = [json.loads(f.read_text()) for f in (lab.lab_root() / "runs").glob("*/run.json")]
    retry = [r for r in runs if (r.get("plan") or {}).get("retry_of")]
    assert len(retry) == 1 and retry[0]["valid"] is True
    t = only_row(lab.pair_report(runs), task["task_id"])
    assert len(t["pairs"]) == 1 and t["invalid_pairs"] == 0


def test_rate_limited_interruption_still_stops_the_batch(task, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RATE_LIMIT", "mid")
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A", "--repeats", "2", "--seed", "1"]) == 1
    out = capsys.readouterr().out
    assert "retrying" not in out and "stopping the batch: api_interrupted" in out


# --- review of PR #6: versions in the pairs report, planned pairs, real spend; two misclassified runs ------------

def _prj(mode, rep_, usd, batch="p1", task="pk-x", task_sha="t1", modes_sha="m1", valid=True, finished=True,
         bundle="b1", fingerprint="f1"):
    return {"run_id": f"{mode}{rep_}{batch}{task_sha}", "task_id": task, "mode": mode, "valid": valid, "success": True,
            "plan": {"paired": True, "batch_id": batch, "repeat": rep_},
            **({"finished_at": "x", "verdict": {"task_passed": True}} if finished else {}),
            "task": {"sha256": task_sha, "hidden_tests_sha256": "h"},
            "versions": {"modes_file_sha256": modes_sha, "claude_code": "2.1.326",
                         "swarmscope": {"bundle_sha256": bundle}},
            "config_fingerprint": fingerprint,
            "config": {"policy_sha256": mode, "model": "s", "advisor": "o", "effort_requested": "high"},
            "costs": {"claude_code_total_usd": usd, "by_model": {"lead": {"usd": usd}}},
            "session": {"wall_s": 60}, "tests": {"hidden": {"regressions": []}}, "decisions": {"stops": 1, "blocks": 0}}


def test_pairs_of_different_experiment_versions_are_never_pooled(capsys):
    """P1: pairs of one task with a different task sha or modes sha are separate rows, and totals are per setup."""
    runs = [_prj("A", 1, 0.1), _prj("B", 1, 0.3),
            _prj("A", 1, 0.1, batch="p2", task_sha="t2"), _prj("B", 1, 0.9, batch="p2", task_sha="t2"),
            _prj("A", 1, 0.1, batch="p3", modes_sha="m2"), _prj("B", 1, 0.2, batch="p3", modes_sha="m2")]
    rows = lab.pair_report(runs)["rows"]
    assert len(rows) == 3 and all(len(t["pairs"]) == 1 for t in rows.values())
    assert sorted(round(t["mean_diff_usd"], 2) for t in rows.values()) == [0.1, 0.2, 0.8]
    assert len({t["setup"] for t in rows.values()}) == 2       # the task sha alone does not change the setup
    # the same pair twice in one row: same versions pool
    runs += [_prj("A", 2, 0.1), _prj("B", 2, 0.5)]
    row = [t for t in lab.pair_report(runs)["rows"].values() if len(t["pairs"]) == 2]
    assert len(row) == 1 and round(row[0]["mean_diff_usd"], 2) == 0.3


@pytest.mark.parametrize("field", ["bundle", "fingerprint"])
def test_pairs_split_on_the_code_bundle_and_on_the_config_fingerprint(field):
    """Review of PR #6 (P2): two batches whose runner/hook code or lab profile differ were one row and one total."""
    runs = [_prj("A", 1, 0.1), _prj("B", 1, 0.3),
            _prj("A", 1, 0.1, batch="p2", **{field: "x2"}), _prj("B", 1, 0.9, batch="p2", **{field: "x2"})]
    r = lab.pair_report(runs)
    assert len(r["rows"]) == 2 and sorted(round(t["mean_diff_usd"], 2) for t in r["rows"].values()) == [0.2, 0.8]
    assert len({t["setup"] for t in r["rows"].values()}) == 2          # two setups: two overall totals
    # the calibration groups the same way
    cal = lab.calibration(runs, mode="A")
    assert len(cal) == 2


def test_pairs_account_for_every_planned_pair_and_the_real_spend():
    """P2: a pair with a half missing or unfinished is reported, a declared replacement is linked to its slot, and
    spend counts interrupted, invalid, unpaired and replaced runs."""
    manifests = {"p1": {"batch_id": "p1", "task": "pk-x", "paired": True,
                        "order": [["A", 1], ["B", 1], ["B", 2], ["A", 2], ["A", 3], ["B", 3], ["B", 4], ["A", 4]]},
                 "p9": {"batch_id": "p9", "task": "pk-x", "paired": True, "order": [["B", 1], ["A", 1]]}}
    runs = [_prj("A", 1, 0.1), _prj("B", 1, 0.3),                       # valid
            _prj("B", 2, 0.4, valid=False), _prj("A", 2, 0.2),          # invalid
            _prj("A", 3, 0.25),                                          # B#3 never ran: incomplete
            _prj("B", 3, 0.17, valid=False, finished=False),            # ... its interrupted attempt is spend
            _prj("B", 4, 0.3), _prj("A", 4, 0.1),                       # superseded by p9#1
            _prj("B", 1, 0.5, batch="p9"), _prj("A", 1, 0.2, batch="p9")]
    rep = lab.replacements_from(manifests, {"replacements": [
        {"batch_id": "p9", "repeat": 1, "replaces": {"batch_id": "p1", "repeat": 4}}]})
    r = lab.pair_report(runs, manifests=manifests, replacements=rep)
    acc = r["accounting"]["pk-x"]
    assert (acc["planned"], acc["valid"], acc["invalid"], acc["incomplete"], acc["replaced"]) == (4, 1, 1, 1, 1)
    assert acc["replacements"] == {"valid": 1}
    row = only_row(r, "pk-x")
    assert len(row["pairs"]) == 2 and row["invalid_pairs"] == 1
    assert [x.get("replaces") for x in row["pairs"] if x["batch"] == "p9"] == [["p1", 4]]
    sp = r["spend"]["pk-x"]
    assert sp["runs"] == 10 and round(sp["usd"], 2) == 2.52
    # outside the valid pairs: invalid pair 0.6, A#3 alone 0.25, interrupted B#3 0.17, replaced pair 0.4
    assert round(sp["outside_pairs_usd"], 2) == 1.42


def test_batch_replaces_declares_the_slot_it_stands_in_for(task):
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "3",
                     "--paired", "--replaces", "20261118-023802-ee05:3"]) == 0
    m = json.loads(next((lab.lab_root() / "batches").glob("*.json")).read_text())
    assert m["replaces"] == [{"batch_id": "20261118-023802-ee05", "repeat": 3}]
    assert lab.replacements_from({m["batch_id"]: m}) == {(m["batch_id"], 1): ("20261118-023802-ee05", 3)}
    with pytest.raises(SystemExit):
        lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "3",
                  "--paired", "--replaces", "a:1,b:2"])


def test_a_diagnostic_replacement_leaves_research_pairs_alone():
    # ChatGPT review of PR #9 (P2): a diagnostic manifest declared as a replacement made the A/B pair "replaced"
    manifests = {"p1": {"batch_id": "p1", "task": "pk-x", "paired": True, "order": [["A", 1], ["B", 1]]},
                 "d1": {"batch_id": "d1", "task": "pk-x", "paired": True, "order": [["BJd", 1]],
                        "replaces": [{"batch_id": "p1", "repeat": 1}]}}
    diag = _prj("BJd", 1, 0.5, batch="d1")
    diag["config"]["policy"] = {"r3_gate_force": "consult"}
    runs = [_prj("A", 1, 0.1), _prj("B", 1, 0.3), diag]
    rep = lab.replacements_from(manifests)
    r = lab.pair_report(runs, manifests=manifests, replacements=rep)
    assert len(only_row(r, "pk-x")["pairs"]) == 1 and r["accounting"]["pk-x"]["replaced"] == 0
    # a replacement slot of other modes (no A/B pair in it) does not knock the pair out either
    other = {"p1": manifests["p1"], "x1": {"batch_id": "x1", "task": "pk-x", "paired": True,
                                           "order": [["C", 1], ["D", 1]], "replaces": [{"batch_id": "p1", "repeat": 1}]}}
    r = lab.pair_report(runs[:2] + [_prj("C", 1, 0.2, batch="x1"), _prj("D", 1, 0.2, batch="x1")],
                        manifests=other, replacements=lab.replacements_from(other))
    assert len(only_row(r, "pk-x")["pairs"]) == 1


def test_batch_refuses_a_diagnostic_replacement(task):
    with pytest.raises(SystemExit, match="diagnostic"):
        lab.main(["batch", "--task", task["_file"], "--modes", "B,BJd", "--repeats", "1", "--seed", "3",
                  "--paired", "--replaces", "20261118-023802-ee05:3"])


def test_cost_gap_after_a_retried_request_is_an_incomplete_reconstruction(task):
    """18.11, 3ef45c38c8: the Mac slept, Claude Code retried a request and billed more than the transcript holds.
    Claude Code's sum is consistent and only exceeds the transcript: the run stays valid, marked incomplete."""
    import os
    os.environ["FAKE_SUSPEND"] = "1"
    try:
        r = lab.attempt(task, "A")
    finally:
        del os.environ["FAKE_SUSPEND"]
    assert r["valid"] is True, r["integrity"]
    assert r["costs"]["reconstruction"] == "incomplete"
    assert r["observation"]["api_retries"][0]["code"] == "StreamSuspended"
    assert r["costs"]["reconstruction_gap"]["gap_by_model"]["claude-sonnet-5-5"] > 0.04


def test_cost_gap_without_a_retry_still_invalidates(task, monkeypatch):
    monkeypatch.setenv("FAKE_SUSPEND", "noretry")
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any(p.startswith("cost mismatch") for p in r["integrity"])
    assert r["costs"]["reconstruction"] == "mismatch"


def test_classify_cost_gap_rules():
    c = {"total_usd": 0.776, "lead": {"model": "s", "usd": 0.472}, "advisor": {"model": "o", "usd": 0.304}, "subs": {}}
    retry = [{"code": "StreamSuspended"}]
    ok = {"total_cost_usd": 0.8266, "modelUsage": {"s": {"costUSD": 0.5226}, "o": {"costUSD": 0.304}}}
    assert lab.classify_cost_gap(ok, c, retry)[0] == "retry_unrecorded"
    assert lab.classify_cost_gap(ok, c, [])[0] == "mismatch"                       # no retry seen
    less = {"total_cost_usd": 0.8266, "modelUsage": {"s": {"costUSD": 0.6026}, "o": {"costUSD": 0.224}}}
    assert lab.classify_cost_gap(less, c, retry)[0] == "mismatch"                  # fewer advisor dollars
    inconsistent = {"total_cost_usd": 0.9, "modelUsage": {"s": {"costUSD": 0.5226}, "o": {"costUSD": 0.304}}}
    assert lab.classify_cost_gap(inconsistent, c, retry)[0] == "mismatch"
    below = {"total_cost_usd": 0.70, "modelUsage": {"s": {"costUSD": 0.40}, "o": {"costUSD": 0.30}}}
    assert lab.classify_cost_gap(below, c, retry)[0] == "mismatch"                 # less than the transcript
    assert lab.classify_cost_gap({"total_cost_usd": 0.78}, c, [])[0] == "ok"


def test_unanswered_unpaid_advisor_request_is_telemetry(task, monkeypatch):
    """18.11, 3206e00905: a voluntary advisor request without a result and without any advisor usage."""
    monkeypatch.setenv("FAKE_ADVISOR_NO_RESULT", "1")
    r = lab.attempt(task, "A")
    assert r["valid"] is True, r["integrity"]
    assert r["advisor"]["no_result"] == 1 and r["advisor"]["unanswered_unpaid"] is True


def _adv_tp(tmp_path, name, recs):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(p)


def _am(mid, blocks, advisor_usage=False):
    its = [{"type": "message"}] + ([{"type": "advisor_message", "model": "claude-opus-5-5"}] if advisor_usage else [])
    return {"type": "assistant", "timestamp": "2026-11-21T01:00:00.000Z",
            "message": {"id": mid, "content": blocks, "usage": {"iterations": its}}}


BASH = {"type": "tool_use", "id": "tu1", "name": "Bash"}
SRV = {"type": "server_tool_use", "id": "srv1", "name": "advisor"}
RES = {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result"}}


def test_no_result_reason_is_read_from_the_transcript(tmp_path):
    # ChatGPT, 21.11 (bj-diag-1): a subtype of no_result from observed signs, not a guessed cause
    tp = _adv_tp(tmp_path, "a.jsonl", [_am("m1", [BASH]), _am("m1", [SRV]),
                                       {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1"}]}},
                                       {"type": "api-request-blob", "message": {"content": [BASH]}}])
    c = lab.advisor_calls(tp)[0]
    assert c["status"] == "no_result" and c["no_result_reason"] == "client_tool_handoff_not_resumed"
    assert c["no_result_signs"] == {"client_tool_before": True, "advisor_usage_in_message": False, "id_seen_later": False}
    # advisor first, then Bash: the order bj-diag-1 saw on the call that returned - not this pattern
    c = lab.advisor_calls(_adv_tp(tmp_path, "b.jsonl", [_am("m1", [SRV]), _am("m1", [BASH])]))[0]
    assert c["no_result_reason"] == "unobserved" and c["no_result_signs"]["client_tool_before"] is False
    # the id is carried into a later request: resumption may be under way, so not "not resumed"
    c = lab.advisor_calls(_adv_tp(tmp_path, "c.jsonl", [_am("m1", [BASH, SRV]),
                                                       {"type": "api-request-blob", "message": {"content": [SRV]}}]))[0]
    assert c["no_result_reason"] == "unobserved" and c["no_result_signs"]["id_seen_later"] is True
    # advisor usage in that message: something ran
    c = lab.advisor_calls(_adv_tp(tmp_path, "d.jsonl", [_am("m1", [BASH, SRV], advisor_usage=True)]))[0]
    assert c["no_result_reason"] == "unobserved"
    # the same id answered later: one continued attempt, returned, no reason
    c = lab.advisor_calls(_adv_tp(tmp_path, "e.jsonl", [_am("m1", [BASH, SRV]), _am("m2", [RES], advisor_usage=True)]))
    assert len(c) == 1 and c[0]["status"] == "returned" and "no_result_reason" not in c[0]


def test_advisor_counts_split_called_and_answered(task, monkeypatch, capsys):
    # ChatGPT, 21.11: requested_answered counted a block with ANY call; "opus 2" read as two consultations
    monkeypatch.setenv("FAKE_ADVISOR_HANDOFF", "1")
    r = lab.attempt(task, "B")
    a = r["advisor"]
    assert r["valid"] is True, r["integrity"]
    assert (a["called"], a["returned"], a["no_result"]) == (2, 1, 1)
    assert (a["requested"], a["requested_called"], a["requested_answered"]) == (1, 1, 1)
    assert a["no_result_reasons"] == {"client_tool_handoff_not_resumed": 1} and a["counts_version"] == 2
    lab.main(["summary"])
    assert "advisor: 2 attempts, 1 returned, 1 no_result; consult blocks 1, answered 1" in capsys.readouterr().out
    monkeypatch.setenv("FAKE_ADVISOR_HANDOFF", "only")              # the block got an attempt but no answer
    a = lab.attempt(task, "B")["advisor"]
    assert (a["requested"], a["requested_called"], a["requested_answered"], a["returned"]) == (1, 1, 0, 0)


def test_paid_advisor_without_a_model_name_still_invalidates(task, monkeypatch):
    monkeypatch.setenv("FAKE_ADVISOR_NO_RESULT", "paid")
    r = lab.attempt(task, "A")
    assert "advisor called but no advisor_message.model in the transcript" in r["integrity"]


def test_revalidate_writes_an_overlay_and_the_corrected_report_uses_it(task, monkeypatch, capsys):
    """Old records stay as they were: revalidated.json beside run.json, and `pairs --revalidated` reads it."""
    monkeypatch.setenv("FAKE_SUSPEND", "1")
    assert lab.main(["batch", "--task", task["_file"], "--modes", "A,B", "--repeats", "1", "--seed", "5",
                     "--paired"]) == 0
    runs = {p.parent.name: p for p in (lab.lab_root() / "runs").glob("*/run.json")}
    rid, path = next((k, p) for k, p in runs.items() if json.loads(p.read_text())["mode"] == "A")
    rj = json.loads(path.read_text())                 # make it look like a record from before the fix
    rj["valid"], rj["integrity"] = False, ["cost mismatch: claude code 0.0530 vs costs.py 0.0045"]
    path.write_text(json.dumps(rj))
    before = path.read_text()
    capsys.readouterr()
    assert lab.main(["pairs"]) == 0
    assert "pairs 0" in capsys.readouterr().out
    assert lab.main(["revalidate", rid]) == 0
    assert "VALID after revalidation" in capsys.readouterr().out
    assert path.read_text() == before and (path.parent / "revalidated.json").exists()
    assert lab.main(["pairs", "--revalidated"]) == 0
    out = capsys.readouterr().out
    assert "corrected report" in out and "pairs 1" in out and "(1 revalidated)" in out


# --- next pilot: B with R3 gated by Jev, and B without R3 ------------------------------------------------------

def _jev_recs(r):
    import jevshadow
    sw = lab.lab_root() / "runs" / r["run_id"] / "sw" / "jev"
    return [x for f in sw.glob("*.jsonl") for x in jevshadow.load(f)[0]]


def test_bj_declines_r3_when_jev_says_no_and_blocks_when_it_says_yes(task, monkeypatch, jev_stub):
    monkeypatch.setenv("JEV_STUB_P", "0.3")
    r = lab.attempt(task, "BJ")
    assert r["valid"] is True, r["integrity"]
    assert r["config"]["policy"]["r3_gate"] == "jev"
    assert r["decisions"]["by_rule"]["R3_high_risk"] == {"declined": 1} and r["decisions"]["blocks"] == 0
    assert r["advisor"]["called"] == 0
    recs = _jev_recs(r)
    ctx = [x for x in recs if x.get("kind") == "context"]
    assert len(ctx) == r["decisions"]["stops"] == 1 and ctx[0]["gating"] == "R3"      # one Jev call per Stop
    assert [x["gating"] for x in recs if x.get("kind") == "jev"] == [True]
    assert r["jev"]["answers"][0]["would_consult"] is False
    # regression (first live BJ run, 18.11): the gate's decision reached the raw journals but not run.json
    assert r["jev"]["answers"][0]["gating"] == "R3"
    r3 = [d for d in r["decisions"]["decisions"] if d["rule"] == "R3_high_risk"]
    assert r3[0]["jev_gate"]["decided_by"] == "jev" and r3[0]["jev_gate"]["would_consult"] is False
    assert r["decisions"]["r3_gate"] == {"asked": 1, "yes": 0, "no": 1, "fallback": 0, "forced": 0, "p_consult": [0.3]}

    monkeypatch.setenv("JEV_STUB_P", "0.8")
    r = lab.attempt(task, "BJ")
    assert r["valid"] is True, r["integrity"]
    assert r["decisions"]["blocks"] == 1 and r["advisor"]["requested_answered"] == 1
    assert r["decisions"]["by_rule"]["R3_high_risk"].get("requested") == 1
    ctx = [x for x in _jev_recs(r) if x.get("kind") == "context"]
    assert len(ctx) == r["decisions"]["stops"] == 2                  # the second Stop is asked in shadow mode
    assert [c.get("gating") for c in sorted(ctx, key=lambda c: c["ts"])] == ["R3", None]
    assert r["decisions"]["r3_gate"] == {"asked": 1, "yes": 1, "no": 0, "fallback": 0, "forced": 0, "p_consult": [0.8]}
    assert [a.get("gating") for a in sorted(r["jev"]["answers"], key=lambda a: a["stop_ts"])] == ["R3", None]


def test_bj_falls_back_to_the_rule_when_jev_is_unreachable(task, monkeypatch):
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", "http://127.0.0.1:9/v1/systemone")   # nothing listens
    r = lab.attempt(task, "BJ")
    assert r["decisions"]["blocks"] == 1                              # r3_gate_on_error "enforce": R3 stands
    gate = [x for x in _jev_recs(r) if x.get("kind") == "jev" and x.get("gating")]
    assert gate and gate[0]["status"] in ("error", "timeout")
    assert any("jev answers not ok" in p for p in r["integrity"])     # a Jev failure is still reported
    assert r["decisions"]["r3_gate"]["fallback"] == 1 and r["decisions"]["r3_gate"]["yes"] == 0


# --- BJ-diag: the forced consultation's chain ---------------------------------------------------------------

def test_r3_gate_summary_counts_forced_apart_from_fallback():
    # condition 2: before, any decided_by other than "jev" counted as a fallback
    dec = [{"jev_gate": {"decided_by": "forced", "forced": "consult",
                         "real": {"status": "ok", "p_consult": 0.2, "would_consult": False}}},
           {"jev_gate": {"decided_by": "forced", "forced": "consult", "real": {"status": "timeout"}}},
           {"jev_gate": {"decided_by": "fallback_enforce", "status": "error"}},
           {"jev_gate": {"decided_by": "jev", "would_consult": True, "p_consult": 0.7}}]
    s = lab.r3_gate_summary(dec)
    assert (s["asked"], s["yes"], s["no"], s["fallback"], s["forced"]) == (4, 1, 0, 1, 2)
    assert s["forced_real"] == {"yes": 0, "no": 1, "failed": 1, "p_consult": [0.2, None]}
    assert s["p_consult"] == [None, 0.7]                          # forced answers never mix with Jev's decisions


def test_bjd_forces_the_block_and_the_chain_is_complete(task, monkeypatch, jev_stub, capsys):
    monkeypatch.setenv("JEV_STUB_P", "0.3")                       # Jev says no: BJ would decline, BJd blocks anyway
    r = lab.attempt(task, "BJd")
    assert r["valid"] is True, r["integrity"]
    assert r["plan"]["diagnostic"] is True and r["config"]["policy"]["r3_gate_force"] == "consult"
    assert r["decisions"]["blocks"] == 1 and r["decisions"]["by_rule"]["R3_high_risk"].get("requested") == 1
    g = r["decisions"]["r3_gate"]
    assert g["forced"] == 1 and g["yes"] == g["no"] == g["fallback"] == 0
    assert g["forced_real"]["no"] == 1 and g["forced_real"]["p_consult"] == [0.3]
    d = r["diag"]
    assert d["verdict"] == "complete", d
    assert all(d["links"][k] for k in lab.DIAG_LINKS)
    assert d["info"]["jev_real"][0]["would_consult"] is False and d["chains"][0]["advisor_models_of_call"] == ["claude-opus-5-5"]
    assert lab.main(["diag"]) == 0
    out = capsys.readouterr().out
    assert "chain: complete" in out and "jev real answer (not used): status ok, p=0.30" in out
    assert "chain 1/1 (decision" in out
    lab.main(["summary"])
    assert "BJd" in capsys.readouterr().out.split("DIAGNOSTIC")[0]


def test_diagnostic_runs_stay_out_of_pairs_and_calib(task, monkeypatch, jev_stub):
    # condition 3: by the policy the run had (r3_gate_force), not only by the plan's label
    monkeypatch.setenv("JEV_STUB_P", "0.3")
    assert lab.main(["batch", "--task", task["_file"], "--modes", "B,BJd", "--repeats", "1", "--seed", "5",
                     "--paired"]) == 0
    runs = lab.load_runs()
    assert sorted(r["mode"] for r in runs) == ["B", "BJd"]
    def no_pairs(rs):
        rows = [t for t in lab.pair_report(rs, modes=("B", "BJd"))["rows"].values() if t["task_id"] == task["task_id"]]
        return all(not t["pairs"] for t in rows)
    assert no_pairs(runs)
    assert lab.calibration(runs, "BJd") == {}
    for r in runs:                                             # the label lost: the policy alone still excludes it
        r["plan"].pop("diagnostic", None)
    assert lab.calibration(runs, "BJd") == {}
    assert no_pairs(runs)


def _diag_rj(**over):
    rj = {"session_status": "success",
          "decisions": {"decisions": [{"id": "s-3", "rule": "R3_high_risk", "state": "requested",
                                       "jev_gate": {"decided_by": "forced", "real": {"status": "ok", "p_consult": 0.2}}}]},
          "advisor": {"blocks": [{"ts": 100.0, "until": 200.0, "decision_ids": ["s-3"], "requests_consult": True,
                                  "calls": ["srv1"]}],
                      "calls": [{"id": "srv1", "ts": "2026-11-20T00:02:00.000Z", "message_id": "m1", "status": "returned",
                                 "answers_block": 100.0}]},
          "result": {"modelUsage": {"claude-opus-5-5": {"costUSD": 0.27}}},
          "costs": {"reconstruction": "complete", "by_model": {"advisor": {"usd": 0.27, "source": "iterations"}}}}
    rj.update(over)
    return rj


def _diag_tp(tmp_path, model="claude-opus-5-5", after=True):
    recs = [{"type": "assistant", "timestamp": "2026-11-20T00:02:00.000Z", "message": {"id": "m1", "content": [
        {"type": "server_tool_use", "id": "srv1", "name": "advisor"},
        {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result"}}],
        "usage": {"iterations": [{"type": "advisor_message", "model": model}]}}}]
    if after:
        recs.append({"type": "assistant", "timestamp": "2026-11-20T00:02:30.000Z", "message": {"id": "m2",
                     "content": [{"type": "text", "text": "applying the advice"}]}})
    p = tmp_path / f"t-{model}-{after}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(p)


def test_diag_chain_reports_the_first_missing_link(tmp_path):
    tp = _diag_tp(tmp_path)
    assert lab.diag_chain(_diag_rj(), tp, "claude-opus-5-5")["verdict"] == "complete"
    nothing = _diag_rj(decisions={"decisions": [{"id": "s-1", "rule": "R3_high_risk", "state": "pass"}]})
    d = lab.diag_chain(nothing, tp, "claude-opus-5-5")
    assert d["verdict"] == "incomplete" and d["info"]["r3_states"] == ["pass"]
    no_call = _diag_rj(advisor={"blocks": [dict(_diag_rj()["advisor"]["blocks"][0], calls=[])], "calls": []})
    assert lab.diag_chain(no_call, tp, "claude-opus-5-5")["verdict"] == "broken at advisor_called_in_block"
    failed = _diag_rj()
    failed["advisor"]["calls"][0]["status"] = "failed"
    assert lab.diag_chain(failed, tp, "claude-opus-5-5")["verdict"] == "broken at advisor_returned"
    assert lab.diag_chain(_diag_rj(), _diag_tp(tmp_path, model="claude-sonnet-5-5"),
                          "claude-opus-5-5")["verdict"] == "broken at pinned_model"
    assert lab.diag_chain(_diag_rj(), _diag_tp(tmp_path, after=False),
                          "claude-opus-5-5")["verdict"] == "broken at session_continued"
    gap = _diag_rj(costs={"reconstruction": "incomplete", "by_model": {"advisor": {"usd": 0.27, "source": "iterations"}}})
    assert lab.diag_chain(gap, tp, "claude-opus-5-5")["verdict"] == "broken at opus_cost_reconciled"
    est = _diag_rj(costs={"reconstruction": "complete", "by_model": {"advisor": {"usd": 0.27, "source": "estimate"}}})
    assert lab.diag_chain(est, tp, "claude-opus-5-5")["verdict"] == "broken at opus_cost_reconciled"
    off = _diag_rj(result={"modelUsage": {"claude-opus-5-5": {"costUSD": 0.40}}})
    assert lab.diag_chain(off, tp, "claude-opus-5-5")["verdict"] == "broken at opus_cost_reconciled"
    # ChatGPT review of PR #9 (P2): only advisor results after the answer are no lead continuation
    only_srv = tmp_path / "srv.jsonl"
    only_srv.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "server_tool_use", "id": "srv1", "name": "advisor"},
            {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result"}},
            {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result"}}],
            "usage": {"iterations": [{"type": "advisor_message", "model": "claude-opus-5-5"}]}}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "server_tool_use", "id": "srv2", "name": "advisor"}, {"type": "text", "text": "  "}]}}]))
    assert lab.diag_chain(_diag_rj(), str(only_srv), "claude-opus-5-5")["verdict"] == "broken at session_continued"
    # ChatGPT review of PR #9 (P2): every forced decision has its own chain; the second block got no consultation
    two = _diag_rj()
    two["decisions"]["decisions"].append({"id": "s-7", "rule": "R3_high_risk", "state": "requested",
                                          "jev_gate": {"decided_by": "forced", "real": {"status": "ok"}}})
    two["advisor"]["blocks"].append({"ts": 300.0, "until": 400.0, "decision_ids": ["s-7"], "requests_consult": True,
                                     "calls": []})
    d = lab.diag_chain(two, tp, "claude-opus-5-5")
    assert d["verdict"] == "broken at advisor_called_in_block" and d["info"]["forced_decisions"] == 2
    assert [c["verdict"] for c in d["chains"]] == ["complete", "broken at advisor_called_in_block"]
    assert d["links"]["advisor_called_in_block"] is False and d["links"]["r3_blocked"] is True
    # a call outside the R3 block's window is not this block's answer
    stray = _diag_rj()
    stray["advisor"]["calls"][0]["answers_block"] = None
    assert lab.diag_chain(stray, tp, "claude-opus-5-5")["verdict"] == "broken at advisor_called_in_block"


@pytest.fixture
def jev_trickle(monkeypatch):
    """A Jev endpoint that accepts the request and then sends its answer one byte a second: no socket operation
    ever waits 10 s, so urllib's per-operation timeout never fires; only a deadline for the whole request does."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(120):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(1)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SWARMSCOPE_JEV_TEST_ENDPOINT", f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone")
    yield
    srv.shutdown()


def test_bj_hanging_jev_times_out_inside_the_stop_budget_and_r3_blocks(task, monkeypatch, jev_trickle, tmp_path):
    # review of f03edc5: the Stop hook had 10 s, as much as Jev may take; a hanging Jev got the hook killed before
    # the fallback was written, and the Stop went through unblocked
    kills = tmp_path / "hook_kills"
    monkeypatch.setenv("FAKE_HOOK_KILLS", str(kills))
    jev_s = lab.build_policy(lab.load_modes()[0], "BJ")["jev"]["timeout_s"]
    t0 = time.time()
    r = lab.attempt(task, "BJ")
    settings = json.loads((lab.lab_root() / "runs" / r["run_id"] / "settings.json").read_text())
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] >= jev_s + 20
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 10
    assert not kills.exists()                                         # the hook finished every time
    gate = [x for x in _jev_recs(r) if x.get("kind") == "jev" and x.get("gating")]
    assert gate and gate[0]["status"] == "timeout" and gate[0]["latency_ms"] < (jev_s + 2) * 1000
    assert r["decisions"]["blocks"] == 1                              # r3_gate_on_error "enforce": the block happened
    assert r["decisions"]["by_rule"]["R3_high_risk"].get("requested") == 1
    assert r["decisions"]["r3_gate"]["fallback"] == 1
    assert any("jev answers not ok" in p for p in r["integrity"])     # and the run is not counted as valid
    assert time.time() - t0 < 120


def test_b12_never_blocks_on_r3(task):
    r = lab.attempt(task, "B12")
    assert r["valid"] is True, r["integrity"]
    assert r["decisions"]["by_rule"]["R3_high_risk"] == {"triggered": 1} and r["decisions"]["blocks"] == 0
    assert r["config"]["policy"]["enforce_rules"] == ["R1_tests", "R2_repeat_error"]
    assert r["decisions"]["r3_gate"] is None


# --- session budget on a clock that counts the host's sleep (a0-pk-1, run 6fe8f53cc9; ChatGPT, 22.11) ---------

class Clocks:
    """Injected clocks: monotonic is the real one (it stops in sleep on macOS); the budget clock is monotonic plus
    `slept`, which the test raises to stand for a sleep; the calendar clock is monotonic plus `shift`.
    then_slept / then_shift apply from the second reading on, i.e. after the session's start: a timer is no good
    for that, because the scans before the start take ~1 s on the Mac (lsof) and a 0.2 s timer fired before the
    first reading (Mac pytest, 22.11)."""
    def __init__(self, then_slept=0.0, then_shift=0.0):
        self.slept, self.shift = 0.0, 0.0
        self.then_slept, self.then_shift, self.reads = then_slept, then_shift, {"b": 0, "w": 0}

    def _b(self):
        self.reads["b"] += 1
        return time.monotonic() + self.slept + (self.then_slept if self.reads["b"] > 1 else 0.0)

    def _w(self):
        self.reads["w"] += 1
        return time.time() + self.shift + (self.then_shift if self.reads["w"] > 1 else 0.0)

    def __call__(self):
        return {"budget": ("FAKE_CONTINUOUS", self._b), "monotonic": time.monotonic, "wall": self._w}


def _session(tmp_path, script, timeout_s, clocks, **kw):
    return lab.run_session(["sh", "-c", script], tmp_path, dict(os.environ), "", tmp_path / "s.jsonl",
                           tmp_path / "e.txt", timeout_s, clocks=clocks(), poll_s=0.1, **kw)


def _later(delay, fn):
    t = threading.Timer(delay, fn)
    t.start()
    return t


def test_budget_ordinary_deadline(tmp_path):
    r = _session(tmp_path, "sleep 30", 1, Clocks())
    assert r["timed_out"] and not r["host_slept"] and 1 <= r["budget_s"] < 3, r
    assert r["budget_clock"] == "FAKE_CONTINUOUS" and r["sleep_tolerance_s"] == 5.0


def test_budget_runs_through_a_sleep_and_marks_it(tmp_path):
    # 6fe8f53cc9: the budget clock passes the deadline in sleep; on waking the session is stopped and marked
    c = Clocks(then_slept=4000.0)
    t0 = time.monotonic()
    r = _session(tmp_path, "sleep 30", 1800, c)
    assert time.monotonic() - t0 < 10
    assert r["timed_out"] and r["host_slept"] and r["slept_s_estimate"] >= 3999, r


def test_budget_sleep_then_a_quick_exit_is_still_caught(tmp_path):
    # the process ends on its own right after waking, before any deadline check: the end check catches the sleep
    c = Clocks()
    real = lab.ProcTracker.scan

    def scan(self, *a, **k):                                 # the "sleep" happens once the agent is running
        c.slept = 120.0
        return real(self, *a, **k)
    import unittest.mock as um
    with um.patch.object(lab.ProcTracker, "scan", scan):
        r = _session(tmp_path, "sleep 0.6; exit 0", 1800, c)
    assert r["exit_code"] == 0 and not r["timed_out"] and r["host_slept"], r


def test_budget_ignores_the_calendar_clock_and_short_gaps(tmp_path):
    c = Clocks(then_shift=3600.0)                            # the calendar clock is set an hour ahead
    r = _session(tmp_path, "sleep 0.6", 5, c)
    assert not r["timed_out"] and not r["host_slept"] and r["wall_s"] > 3000 and r["budget_s"] < 5, r
    assert not _session(tmp_path, "sleep 0.6", 30, Clocks(then_slept=4.0))["host_slept"]   # under the 5 s tolerance


def _gone(pid):
    st = lab.process_table().get(pid)
    return st is None or st[2].startswith("Z")         # a zombie left to a pid 1 that reaps late is gone too


def test_budget_stops_background_commands_before_the_snapshot(tmp_path):
    pidf = tmp_path / "bg.pid"
    r = _session(tmp_path, f"sleep 300 & echo $! > {pidf}; exit 0", 30, Clocks())
    pid = int(pidf.read_text())
    assert r["background_left_running"] is True and pid in r["stopped_after_session"] and r["stop_failed"] == []
    assert _gone(pid)


def test_no_sleep_counting_clock_refuses_to_run(tmp_path):
    with pytest.raises(RuntimeError):
        lab.run_session(["true"], tmp_path, {}, "", tmp_path / "s", tmp_path / "e", 5,
                        clocks={"budget": None, "monotonic": time.monotonic, "wall": time.time})


def _slept_at_start(monkeypatch, seconds):
    # the budget clock jumps after its first reading: the host slept as soon as the session started
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return time.monotonic() + (seconds if calls["n"] > 1 else 0)
    monkeypatch.setattr(lab, "default_clocks", lambda: {"budget": ("FAKE_CONTINUOUS", clock),
                                                         "monotonic": time.monotonic, "wall": time.time})


def test_a_slept_session_is_infra_invalid_even_when_it_succeeds(task, monkeypatch):
    _slept_at_start(monkeypatch, 60)
    r = lab.attempt(task, "A")
    assert r["session"]["host_slept"] and r["infra_invalid"] == "host_slept"
    assert r["valid"] is False and any(p.startswith("infrastructure: host slept") for p in r["integrity"])
    assert r["session_status"] == "success" and "timeout_class" not in r


def test_a_timeout_in_sleep_is_host_slept_not_budget(task, monkeypatch):
    modes_with(monkeypatch, timeout_s=30)
    monkeypatch.setenv("FAKE_SLEEP", "5")
    _slept_at_start(monkeypatch, 100)
    r = lab.attempt(task, "A")
    assert r["session_status"] == "timeout" and r["timeout_class"] == "host_slept" and r["valid"] is False


def test_a_budget_timeout_without_sleep_stays_a_valid_failure(task, monkeypatch):
    modes_with(monkeypatch, timeout_s=1)
    monkeypatch.setenv("FAKE_SLEEP", "4")
    r = lab.attempt(task, "A")
    assert r["timeout_class"] == "budget" and not r["session"]["host_slept"] and "infra_invalid" not in r
    assert r["session"]["budget_clock"] == lab.budget_clock()[0]


def test_keep_awake_holds_caffeinate_on_macos(monkeypatch, tmp_path):
    fake = tmp_path / "caffeinate"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path}/args\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(lab.sys, "platform", "darwin")
    monkeypatch.setattr(lab.shutil, "which", lambda n, *a, **k: str(fake) if n == "caffeinate" else None)
    monkeypatch.setattr(lab, "_KEEP_AWAKE", {})
    try:
        k = lab.keep_awake()
        assert k["required"] and k["running"], k
        end = time.monotonic() + 10                  # the fake writes its args a moment after it starts (Mac: 0.31 s)
        while not (tmp_path / "args").exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert (tmp_path / "args").read_text().split() == ["-i", "-s", "-w", str(os.getpid())]
        assert lab.keep_awake()["pid"] == k["pid"]                       # held once, not one per run
    finally:
        lab._KEEP_AWAKE["p"].kill()
    monkeypatch.setattr(lab, "_KEEP_AWAKE", {})
    monkeypatch.setattr(lab.shutil, "which", lambda n, *a, **k: None)
    assert lab.keep_awake() == {"required": True, "running": False, "reason": "caffeinate not found"}


def test_a_missing_keep_awake_on_macos_stops_the_run(task, monkeypatch):
    monkeypatch.setattr(lab, "keep_awake", lambda: {"required": True, "running": False, "reason": "caffeinate not found"})
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("keep-awake: caffeinate not found" in p for p in r["integrity"])
    assert "session" not in r or not r.get("session")


# ChatGPT, 22.11 (P1): a background command that left the session's process group (start_new_session, setsid)
# outlived the session and wrote after the snapshot had begun

DETACH = ("import subprocess, sys, time; subprocess.Popen(['/bin/sh', '-c', 'sleep {wait}; touch {f}'], "
          "start_new_session=True, env={env}, cwd={cwd!r}); time.sleep({stay})")


def _detached(tmp_path, env="None", stay=0, wait=2, cwd=None):
    f = tmp_path / "late-write"
    return f, f"{sys.executable} -c \"{DETACH.format(f=f, env=env, stay=stay, wait=wait, cwd=cwd)}\""


def _no_late_write(f, wait=3):
    time.sleep(wait)
    return not f.exists()


def test_a_detached_child_is_stopped_before_the_snapshot(tmp_path):
    f, script = _detached(tmp_path)                       # the parent exits at once; the child keeps the tag
    r = _session(tmp_path, script, 30, Clocks())
    assert r["stopped_after_session"] and r["stop_failed"] == [] and r["background_left_running"], r
    assert _no_late_write(f)


def test_a_detached_child_without_the_tag_is_caught_by_the_tree(tmp_path):
    # its environment cleared (no tag); its parent lives on for 1.5 s, so a scan sees it as a descendant
    f, script = _detached(tmp_path, env="{'PATH': '/usr/bin:/bin'}", stay=1.5)
    r = _session(tmp_path, script, 30, Clocks())
    assert r["stopped_after_session"] and r["stop_failed"] == [], r
    assert _no_late_write(f)


def test_a_detached_child_by_the_tag_alone(tmp_path, monkeypatch):
    # the tree is made blind (every process a child of pid 1 in a group of its own) and the child leaves the
    # working copy: the tag still finds it (ps -E on macOS, /proc/*/environ on Linux: PR #19 review, P2)
    f, script = _detached(tmp_path, cwd="/")
    real = lab.process_table
    monkeypatch.setattr(lab, "process_table", lambda: {pid: (1, pid, st, start)
                                                       for pid, (_, _, st, start) in real().items()})
    r = _session(tmp_path, script, 30, Clocks())
    assert r["tag_scan"] == ("ps_E" if sys.platform == "darwin" else "proc_environ"), r
    if sys.platform == "darwin" and not r["stopped_after_session"]:
        # lab Mac, 22.11: ps -E did not show the tag in the environment of /bin/sh: on macOS the tag scan is
        # best effort (README); the method is checked, the stop by the tag alone is not promised there
        pytest.xfail("macOS: ps -E did not reveal the run tag of a /bin/sh child (tag scan best effort)")
    assert r["stopped_after_session"], r
    assert _no_late_write(f)


def test_a_fast_detach_with_a_clean_environment_in_the_working_copy(tmp_path):
    # PR #19 review (P1): env cleared, own group, parent gone at once (stay=0) - no tag, no tree; the child kept
    # the agent's folder as its cwd, which the final scan finds (Linux: also the subreaper)
    f, script = _detached(tmp_path, env="{'PATH': '/usr/bin:/bin'}", stay=0)
    r = _session(tmp_path, script, 30, Clocks())
    assert r["stopped_after_session"] and r["stop_failed"] == [], r
    assert _no_late_write(f)


def test_a_fast_detach_that_also_leaves_the_working_copy(tmp_path):
    # no tag, no tree, no cwd: only an adopting reaper sees it. Linux: the runner is subreaper (prctl), so the
    # orphan is its child. macOS has no unprivileged equivalent: the known limit (PR #19, open decision)
    if sys.platform == "darwin":
        pytest.xfail("macOS: no unprivileged mechanism adopts an orphan that left the tree, tag and cwd")
    f, script = _detached(tmp_path, env="{'PATH': '/usr/bin:/bin'}", stay=0, cwd="/")
    r = _session(tmp_path, script, 30, Clocks())
    assert r["subreaper"] is True and r["stopped_after_session"] and r["stop_failed"] == [], r
    assert _no_late_write(f)


def test_scans_fail_loudly(tmp_path, monkeypatch):
    # PR #19 review (P1): ps failing or not starting is a scan error, never "no processes"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "ps").write_text("#!/bin/sh\nexit 1\n")
    (bin_ / "ps").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    with pytest.raises(lab.ScanError):
        lab.process_table()
    (bin_ / "ps").write_text("#!/bin/sh\nexit 0\n")             # succeeds, lists nothing
    with pytest.raises(lab.ScanError):
        lab.process_table()
    (bin_ / "ps").write_text("#!/bin/sh\necho '1 0 1 S Sun Nov 22 00:00:00 2026'\n")   # lists others, not itself
    with pytest.raises(lab.ScanError):
        lab.process_table()
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    with pytest.raises(lab.ScanError):
        lab.process_table()


def test_no_scan_no_session(tmp_path, monkeypatch):
    def broken():
        raise lab.ScanError("ps exited with 1")
    monkeypatch.setattr(lab, "process_table", broken)
    with pytest.raises(RuntimeError, match="process scan unavailable"):
        _session(tmp_path, "touch started", 30, Clocks())
    assert not (tmp_path / "started").exists()                   # the agent never started


def test_a_scan_failure_with_the_session_running_kills_it_and_says_so(tmp_path, monkeypatch):
    real, calls = lab.process_table, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 3:
            raise lab.ScanError("ps could not run: OSError")
        return real()
    monkeypatch.setattr(lab, "process_table", flaky)
    pidf = tmp_path / "pid"
    t0 = time.monotonic()
    r = _session(tmp_path, f"echo $$ > {pidf}; sleep 300", 600, Clocks())
    assert r["scan_error"] and "ps could not run" in r["scan_error"] and time.monotonic() - t0 < 30, r
    monkeypatch.undo()
    assert _gone(int(pidf.read_text()))


def test_a_scan_failure_makes_the_run_invalid(task, monkeypatch):
    real = lab.run_session
    monkeypatch.setattr(lab, "run_session", lambda *a, **k: dict(real(*a, **k), scan_error="ScanError: ps exited 1"))
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("process scan failed" in p for p in r["integrity"])


def test_an_unavailable_scan_stops_the_run_before_the_session(task, monkeypatch):
    def broken(work):
        raise lab.ScanError("lsof not found")
    monkeypatch.setattr(lab, "scan_check", broken)
    r = lab.attempt(task, "A")
    assert r["valid"] is False and r["session_status"] == "not_started"
    assert any("process scan unavailable" in p for p in r["integrity"])


def test_a_child_that_cannot_be_stopped_is_reported(tmp_path, monkeypatch):
    f, script = _detached(tmp_path, wait=30)
    real_kill, real_killpg = os.kill, os.killpg
    monkeypatch.setattr(lab.os, "kill", lambda *a: None)
    monkeypatch.setattr(lab.os, "killpg", lambda *a: None)
    real_stop = lab.stop_tree
    monkeypatch.setattr(lab, "stop_tree", lambda tr, p, clock, grace_s=15, exempt=(): real_stop(tr, p, clock, 0.3, exempt))
    try:
        r = _session(tmp_path, script, 30, Clocks())
    finally:
        monkeypatch.undo()
    for pid in r["stop_failed"]:
        try:
            real_killpg(pid, 9)
        except OSError:
            real_kill(pid, 9)
    assert r["stop_failed"], r


def test_a_stop_failure_reaches_the_integrity_list(task, monkeypatch):
    real = lab.run_session
    monkeypatch.setattr(lab, "run_session", lambda *a, **k: dict(real(*a, **k), stop_failed=[4242]))
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("still running after the stop: [4242]" in p for p in r["integrity"])


def test_the_runners_jev_workers_survive_the_stop(task):
    # the Stop hook's shadow worker answers after the session: registered in the runner's folder, left running
    r = lab.attempt(task, "A")
    assert r["valid"], r["integrity"]
    assert r["session"]["observers_kept"] or r["session"]["stopped_after_session"] == []


def test_observer_pids_need_the_registry_and_the_worker_command(tmp_path):
    (tmp_path / "jev").mkdir()
    w = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "jevshadow.py", "--worker"])
    other = subprocess.Popen(["sleep", "30"])
    try:
        assert lab.observer_pids(tmp_path) == set()                   # nothing registered
        (tmp_path / "jev" / "workers.pids").write_text(f"{w.pid}\n{other.pid}\n")
        assert lab.observer_pids(tmp_path) == {w.pid}                 # a registered pid must be a worker too
    finally:
        w.kill()
        other.kill()


@pytest.mark.parametrize("script,timeout_s", [("sleep 300 & exit 0", 30), ("sleep 300", 1)])
def test_a_sleep_during_the_cleanup_is_caught(tmp_path, monkeypatch, script, timeout_s):
    # ChatGPT, 22.11 (P2): the clocks were read before the cleanup; a sleep inside it went unseen
    c = Clocks()
    real = lab.stop_tree

    def sleepy(*a, **k):
        c.slept = 120.0
        return real(*a, **k)
    monkeypatch.setattr(lab, "stop_tree", sleepy)
    r = _session(tmp_path, script, timeout_s, c)
    assert r["host_slept"] and r["slept_s_estimate"] >= 119, r
    assert r["timed_out"] == (timeout_s == 1)


def test_the_cleanup_wait_runs_on_the_budget_clock(tmp_path):
    # children that ignore SIGTERM: the 15 s grace period ends on the budget clock, which a sleep moves on
    c = Clocks()
    p = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 300 & wait"], start_new_session=True)
    tr = lab.ProcTracker(p.pid, None)
    time.sleep(0.3)
    _later(1.0, lambda: setattr(c, "slept", 1000.0))       # the host "sleeps" during the grace period
    t0 = time.monotonic()
    found, left = lab.stop_tree(tr, p, c()["budget"][1])
    assert found and left == [] and time.monotonic() - t0 < 8, (found, left, time.monotonic() - t0)



@pytest.mark.parametrize("code,out,err", [(0, "", ""), (1, "", ""), (1, "", "lsof: WARNING"), (0, "garbage\n", ""),
                                          (0, "p999\nn/x\n", "")])
def test_an_lsof_answer_without_itself_is_a_scan_error(code, out, err, tmp_path):
    # PR #19 review (P2): lsof lists the cwd of every process of this user, itself included
    with pytest.raises(lab.ScanError):
        lab.parse_lsof_cwd(code, out, err, 4242, str(tmp_path))


def test_lsof_answers_that_list_itself(tmp_path):
    inside = tmp_path / "w"
    inside.mkdir()
    out = f"p4242\nn/\np77\nn{inside}\np78\nn{tmp_path}\n"
    for code in (0, 1):
        assert lab.parse_lsof_cwd(code, out, "", 4242, str(inside)) == {77}
    with pytest.raises(lab.ScanError):
        lab.parse_lsof_cwd(2, out, "", 4242, str(inside))           # any other exit code


def test_the_runners_own_children_are_not_the_sessions(tmp_path):
    # PR #19 review (P1): a helper the runner started before the agent - its cwd in the working copy, so the cwd
    # scan sees it too - stays alive; the agent's fast-detached orphan (adopted by the subreaper on Linux) is stopped
    helper = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        f, script = _detached(tmp_path, env="{'PATH': '/usr/bin:/bin'}", stay=0, cwd="/")
        r = _session(tmp_path, script, 30, Clocks())
        assert helper.poll() is None and helper.pid not in r["stopped_after_session"], r
        if sys.platform.startswith("linux"):
            assert r["stopped_after_session"] and r["stop_failed"] == [], r
            assert _no_late_write(f)
    finally:
        helper.kill()
        helper.wait()


def test_a_baseline_child_is_never_taken_for_an_orphan():
    me = os.getpid()
    table = {500: (me, 500, "S", "Sun Nov 22 01:00:00 2026"), 501: (me, 501, "S", "Sun Nov 22 01:05:00 2026"),
             600: (me, 600, "S", "Sun Nov 22 01:06:00 2026")}
    tr = lab.ProcTracker(9999, None, reaper=True,
                         baseline={500: "Sun Nov 22 01:00:00 2026", 600: "Sun Nov 22 00:01:00 2026"})
    import unittest.mock as um
    with um.patch.object(lab, "process_table", lambda: table), um.patch.object(lab, "tagged_pids", lambda t: (set(), None)):
        tr.scan()
    assert sorted(tr.seen) == [501, 600]      # 500 is the old helper; 600 is a reused pid with a new start time
    # no rule takes a baseline process: not the cwd scan, the tag, nor the leader's group
    table[502] = (1, 9999, "S", "Sun Nov 22 00:30:00 2026")
    tr = lab.ProcTracker(9999, "t", work="/w", reaper=True, baseline={500: table[500][3], 502: table[502][3]})
    with um.patch.object(lab, "process_table", lambda: table), \
            um.patch.object(lab, "tagged_pids", lambda t: ({500, 502}, "proc_environ")), \
            um.patch.object(lab, "cwd_pids", lambda w: {500, 502}):
        tr.scan(cwd=True)
    assert 500 not in tr.seen and 502 not in tr.seen and sorted(tr.seen) == [501, 600]


def test_without_the_subreaper_on_linux_no_agent_starts(tmp_path, monkeypatch):
    # PR #19 review (P2): the Linux mode promises a complete stop; without the subreaper it refuses to start
    monkeypatch.setattr(lab.sys, "platform", "linux")
    monkeypatch.setattr(lab, "scan_check", lambda work: None)
    monkeypatch.setattr(lab, "set_subreaper", lambda on: False)
    with pytest.raises(RuntimeError, match="subreaper unavailable"):
        _session(tmp_path, "touch started", 30, Clocks())
    assert not (tmp_path / "started").exists()


def test_without_the_subreaper_the_attempt_does_not_start(task, monkeypatch):
    monkeypatch.setattr(lab, "subreaper_check", lambda: "prctl PR_SET_CHILD_SUBREAPER failed")
    r = lab.attempt(task, "A")
    assert r["valid"] is False and r["session_status"] == "not_started"
    assert any("subreaper unavailable" in p and "prctl" in p for p in r["integrity"])


def test_the_stop_guarantee_is_recorded(tmp_path):
    r = _session(tmp_path, "true", 30, Clocks())
    expected = "complete (subreaper)" if sys.platform.startswith("linux") else "incomplete"
    assert r["stop_guarantee"].startswith(expected), r
