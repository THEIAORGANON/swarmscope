"""The lab's read guard (readguard.py, called by hook.py at PreToolUse): reading tools reach only the working
copy, allow_read and the session's own spilled tool outputs, decided on the path as it is when the tool runs."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import readguard  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    work = tmp_path / "w"
    (work / "src").mkdir(parents=True)
    proj = tmp_path / "cfg" / "projects" / "-w"
    (proj / "S1" / "tool-results").mkdir(parents=True)
    (proj / "S2" / "tool-results").mkdir(parents=True)
    (tmp_path / "late").mkdir()                       # created after the settings were written
    return tmp_path, work, proj


def call(tool, ti, work, proj, sid="S1"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti, "cwd": str(work),
            "session_id": sid, "transcript_path": str(proj / f"{sid}.jsonl")}


def test_allowed_reads(tree):
    t, work, proj = tree
    spec = {"allow": [str(work)]}
    for tool, ti in [("Read", {"file_path": "src/a.py"}), ("Read", {"file_path": str(work / "x")}),
                     ("Read", {"file_path": str(proj / "S1" / "tool-results" / "b.txt")}),
                     ("Grep", {"pattern": "k"}), ("Grep", {"pattern": "k", "path": "src"}),
                     ("Glob", {"pattern": "**/*.py"}), ("Glob", {"pattern": "src/*/x.py", "path": str(work)}),
                     ("Bash", {"command": "cat /etc/passwd"})]:      # Bash is the sandbox's business
        assert readguard.check(call(tool, ti, work, proj), spec) is None, (tool, ti)


def test_denied_reads(tree):
    t, work, proj = tree
    spec = {"allow": [str(work)]}
    for tool, ti in [("Read", {"file_path": str(t / "late" / "f")}),            # a path that appeared later
                     ("Read", {"file_path": "../late/f"}),
                     ("Read", {"file_path": str(proj / "S2" / "tool-results" / "b.txt")}),   # another session
                     ("Read", {"file_path": str(proj / "S1.jsonl")}),                       # own transcript
                     ("Glob", {"pattern": str(t / "late") + "/*"}),                        # pattern leaves path
                     ("Glob", {"pattern": "../late/**"}),
                     ("Glob", {"pattern": "src/*/../../../late/*"}),                         # .. after a wildcard
                     ("Grep", {"pattern": "k", "path": str(t)}), ("LS", {"path": "/"}),
                     ("NotebookRead", {"notebook_path": str(t / "late" / "n.ipynb")})]:
        assert readguard.check(call(tool, ti, work, proj), spec), (tool, ti)


def test_symlink_out_of_the_working_copy_is_denied(tree):
    t, work, proj = tree
    (t / "late" / "secret").write_text("s")
    os.symlink(t / "late", work / "link")
    assert readguard.check(call("Read", {"file_path": "link/secret"}, work, proj), {"allow": [str(work)]})


def test_errors_deny(tree):
    t, work, proj = tree
    assert readguard.check({"tool_name": "Read", "tool_input": None, "cwd": None}, {"allow": [str(work)]})


def test_absolute_glob_with_a_wildcard_in_the_first_folder_keeps_the_root(tree):
    # review of PR #8 (P2): for "/[U]sers/..." the fixed part was empty and the working copy was put in its place
    t, work, proj = tree
    spec = {"allow": [str(work)]}
    for pat in ("/[U]sers/x/*.txt", "/*/late/*", "/*", "/{a,b}/c", "~/x/*"):
        assert readguard.check(call("Glob", {"pattern": pat}, work, proj), spec), pat
        assert readguard.check(call("Glob", {"pattern": pat, "path": str(work)}, work, proj), spec), pat
    first = str(work).split("/")[1]
    inside = "/" + first + str(work)[len(first) + 1:] + "/src/*.py"         # an absolute pattern into the copy
    assert readguard.check(call("Glob", {"pattern": inside}, work, proj), spec) is None


def run_guard(d, env_spec, extra_env=None, pythonpath=None):
    env = dict(os.environ, **(extra_env or {}))
    if env_spec is None:
        env.pop("SWARMSCOPE_READ_GUARD", None)
    else:
        env["SWARMSCOPE_READ_GUARD"] = env_spec
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run([sys.executable, os.path.join(ROOT, "readguard.py")], input=json.dumps(d),
                          capture_output=True, text=True, env=env)


def test_guard_script_denies_and_allows(tree):
    t, work, proj = tree
    spec = json.dumps({"allow": [str(work)]})
    r = run_guard(call("Read", {"file_path": str(t / "late" / "f")}, work, proj), spec)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "outside the working copy" in out["hookSpecificOutput"]["permissionDecisionReason"]
    r = run_guard(call("Read", {"file_path": "src/a.py"}, work, proj), spec)
    assert r.returncode == 0 and r.stdout == ""


def test_guard_script_errors_before_the_decision_exit_non_zero(tree, tmp_path):
    # review of PR #8 (P1): an error before the check ended in exit 0 with no output, i.e. "no opinion"; now
    # nothing is swallowed and the exit code is an error, which onFailure "block" turns into a blocked call
    t, work, proj = tree
    d = call("Read", {"file_path": str(t / "late" / "f")}, work, proj)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "json.py").write_text("raise ImportError('simulated import failure')\n")
    for r in (run_guard(d, json.dumps({"allow": [str(work)]}), pythonpath=str(broken)),   # import fails
              run_guard(d, "{not json"),                                                  # broken spec
              run_guard(d, None)):                                                        # no spec at all
        assert r.returncode not in (0, 2) and r.stdout == "", r


def test_main_hook_no_longer_decides_on_reads(tree, tmp_path):
    t, work, proj = tree
    env = dict(os.environ, SWARMSCOPE_DIR=str(tmp_path / "sw"),
               SWARMSCOPE_READ_GUARD=json.dumps({"allow": [str(work)]}))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")],
                       input=json.dumps(call("Read", {"file_path": str(t / "late" / "f")}, work, proj)),
                       capture_output=True, text=True, env=env, cwd=str(work))
    assert r.stdout.strip() == ""
