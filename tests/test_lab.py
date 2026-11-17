"""Lab runner end to end with a scripted fake Claude Code that calls the real hooks and a local Jev stub
(no network, no model). Each test pins one failure that would make a run look valid when it is not."""
import argparse
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lab"))
import lab  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")
KEY = "apikey_LABTEST_never_in_the_agent_env"


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=dict(os.environ, **lab.GIT_ENV))


@pytest.fixture
def jev_stub(monkeypatch):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
            out = json.dumps({"model": body["model"], "answers": {"consult_advisor": {"type": "noul", "noul": 0.3}},
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
    monkeypatch.setenv("SWARMSCOPE_CLAUDE_BIN", FAKE)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setenv("SWARMSCOPE_LAB_UNSANDBOXED_TESTS", "1")        # no Seatbelt on Linux
    monkeypatch.setenv("SWARMSCOPE_LAB_PASS_ENV", "FAKE_SKILLS,FAKE_NO_STOP,FAKE_SLEEP,FAKE_MODEL,FAKE_LEAK,FAKE_NET,FAKE_SKIP,FAKE_EVIL,FAKE_DENY")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
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
        assert f"Read(/{p}/**)" in s["permissions"]["deny"] and f"Edit(/{p}/**)" in s["permissions"]["deny"]
    assert "Read(~/**)" in s["permissions"]["deny"]


def test_working_copy_hides_the_experiment(task):
    r = lab.attempt(task, "B", keep_work=True)
    work = lab.work_root(lab.load_modes()[0]) / r["run_id"]
    assert not set(os.listdir(work)) & {".claude", "PROMPT.txt", ".swarmscope", "CLAUDE.md"}
    assert subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.strip() == ""
    assert lab.lab_root().resolve() not in work.resolve().parents


# --- validity, session status and the verdict are separate --------------------------------------------

def test_timeout_is_never_success_even_when_hidden_tests_pass(task, monkeypatch):
    modes_with(monkeypatch, timeout_s=1)
    monkeypatch.setenv("FAKE_SLEEP", "4")
    monkeypatch.setenv("FAKE_NO_STOP", "1")
    r = lab.attempt(task, "A")
    assert r["session_status"] == "timeout" and r["success"] is False and r["valid"] is False
    assert r["verdict"]["session_completed"] is False


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
    r = lab.attempt(task, "A")
    assert r["valid"] is False and any("without a sandbox" in p for p in r["integrity"])


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
