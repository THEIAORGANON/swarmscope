"""Lab runner end to end with a scripted fake Claude Code that calls the real hooks (no network, no model)."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lab"))
import lab  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, **lab.GIT_ENV))


@pytest.fixture
def task(tmp_path, monkeypatch):
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
        "    def test_h(self):\n        self.assertEqual(Inventory().available('z'), 0)\nunittest.main()\n")
    t = {"task_id": "t1", "source": {"repo": str(src), "commit": "HEAD", "exclude": [".claude", "PROMPT.txt"]},
         "prompt": "Fix it.", "public_tests": "PYTHONPATH=src python3 -m unittest discover -s tests -t .",
         "hidden_tests": {"dir": str(hidden), "cmd": "PYTHONPATH=src python3 {hidden}/test_hidden.py"}}
    p = tmp_path / "task.json"
    p.write_text(json.dumps(t))
    monkeypatch.setenv("SWARMSCOPE_LAB", str(tmp_path / "lab"))
    monkeypatch.setenv("SWARMSCOPE_CLAUDE_BIN", FAKE)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))          # no Jev key, no user files
    lab.cmd_setup(None)
    return lab.load_task(str(p))


def test_a_and_b_same_base_same_infrastructure(task):
    a = lab.attempt(task, "A")
    b = lab.attempt(task, "B")
    assert a["integrity"] == [] and b["integrity"] == []
    assert a["base"]["work_commit"] == b["base"]["work_commit"]
    assert a["base"]["excluded"] == [".claude", ".claude/swarmscope-policy.json", "PROMPT.txt"]
    # A records, B enforces; the advisor call after the block answers B's request
    assert a["decisions"]["by_rule"]["R3_high_risk"] == {"triggered": 1} and a["advisor"]["called"] == 0
    assert b["decisions"]["by_rule"]["R3_high_risk"] == {"requested": 1, "pass": 1}
    assert b["advisor"] == dict(b["advisor"], requested=1, called=1, returned=1, voluntary=0, requests_without_call=[])
    assert b["costs"]["by_model"]["advisor"]["source"] == "iterations"
    # Jev ran in both modes; with no key every answer is recorded as no_key, never as skip
    for r in (a, b):
        assert r["jev"]["contexts"] >= 1 and {x["status"] for x in r["jev"]["answers"]} == {"no_key"}
        assert r["tests"]["hidden"]["tests"] == 1 and r["success"] is True
        assert r["diff"]["lines"]["production"] == 60


def test_working_copy_hides_the_experiment(task):
    r = lab.attempt(task, "B")
    work = lab.lab_root() / "w" / r["run_id"]
    names = set(os.listdir(work))
    assert not names & {".claude", "PROMPT.txt", ".swarmscope", "CLAUDE.md"}
    remotes = subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout
    assert remotes.strip() == ""
    pol = json.loads((lab.lab_root() / "runs" / r["run_id"] / "policy.json").read_text())
    assert pol["mode"] == "enforce" and not str(lab.lab_root() / "runs").startswith(str(work))


def test_preflight_stops_on_instructions_above_the_working_copy(task):
    (lab.lab_root() / "CLAUDE.md").write_text("Consult the advisor before you finish.")
    r = lab.attempt(task, "A")
    assert r["completion_status"] == "not_started" and "session" not in r
    assert any("CLAUDE.md" in p for p in r["integrity"])


def test_agent_env_drops_experiment_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARMSCOPE_POLICY_FILE", "/x")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    env, dropped = lab.agent_env(tmp_path)
    assert "SWARMSCOPE_POLICY_FILE" not in env and "TYPESAFE_API_KEY" not in env
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env and env["CLAUDE_CONFIG_DIR"] == str(tmp_path)


def test_voluntary_and_answering_calls():
    reqs = [{"id": "r1", "ts": 100.0}]
    calls = [{"id": "c0", "ts": 50.0, "status": "returned"}, {"id": "c1", "ts": 101.0, "status": "returned"}]
    out, unanswered = lab.attribute_calls(calls, reqs)
    assert [c["voluntary"] for c in out] == [True, False] and out[1]["answers_request"] == "r1" and unanswered == []


def test_marketplace_catalogs_and_home_settings_are_not_problems(task):
    cfg = lab.lab_root() / "config"
    (cfg / "plugins" / "marketplaces" / "official").mkdir(parents=True)
    (cfg / "plugins" / "known_marketplaces.json").write_text("{}")
    (cfg / "plugins" / "marketplaces" / "official" / "marketplace.json").write_text("{}")
    home_claude = lab.lab_root().parent / ".claude"            # an ancestor's .claude/settings.json
    home_claude.mkdir(exist_ok=True)
    (home_claude / "settings.json").write_text('{"hooks": {}}')
    work = lab.lab_root() / "w" / "x"
    work.mkdir(parents=True)
    (cfg / "skills" / "synced" / "acct").mkdir(parents=True)          # account skills Claude Code syncs itself
    (cfg / "skills" / "synced" / "acct" / "SKILL.md").write_text("x")
    pf = lab.preflight(work, cfg)
    assert pf["ok"], pf["problems"]
    assert pf["config"]["skills"]                                       # recorded, checked at launch instead
    (home_claude / "CLAUDE.md").write_text("consult the advisor")
    pf = lab.preflight(work, cfg)
    assert any(".claude/CLAUDE.md" in p for p in pf["problems"])


def test_skills_loaded_in_the_session_are_an_integrity_problem(task, monkeypatch):
    monkeypatch.setenv("FAKE_SKILLS", '["built-in-browser"]')
    r = lab.attempt(task, "A")
    assert any("skills loaded" in p for p in r["integrity"])
    assert "--disable-slash-commands" in r["config"]["argv"]


def test_doctor_ping_runs_the_real_command_line(task, capsys):
    import argparse
    assert lab.cmd_doctor(argparse.Namespace(ping=True)) == 0
    out = capsys.readouterr().out
    assert "'OK'" in out and "advisor/effort fields in init" in out
    assert "ready" in out.splitlines()[-1]


def test_builtin_plugins_are_clean_others_are_not():
    builtin = {"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}
    other = {"name": "x", "path": "/u/.claude/plugins/x", "source": "x@market"}
    assert lab.not_clean({"plugins": [builtin], "skills": [], "mcp_servers": []}) == {}
    assert lab.not_clean({"plugins": [builtin, other]}) == {"plugins": [other]}
    assert lab.not_clean({"skills": ["built-in-browser"]}) == {"skills": ["built-in-browser"]}


def test_user_git_signing_config_does_not_reach_the_base_commit(task, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n"
                                     "[core]\n\thooksPath = /nonexistent\n")
    r = lab.attempt(task, "A")
    assert r["completion_status"] != "not_started", r["integrity"]
    work = lab.lab_root() / "w" / r["run_id"]
    cfg = subprocess.run(["git", "config", "--local", "commit.gpgsign"], cwd=work, capture_output=True, text=True)
    assert cfg.stdout.strip() == "false"


def test_base_failure_is_recorded_not_raised(task):
    task = dict(task, source=dict(task["source"], commit="no-such-commit"))
    r = lab.attempt(task, "A")
    assert r["completion_status"] == "not_started" and any(p.startswith("base:") for p in r["integrity"])
    assert (lab.lab_root() / "runs" / r["run_id"] / "run.json").exists()
