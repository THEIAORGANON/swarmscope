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


def test_hook_denies_through_claude_codes_hook_output(tree, tmp_path):
    t, work, proj = tree
    env = dict(os.environ, SWARMSCOPE_DIR=str(tmp_path / "sw"),
               SWARMSCOPE_READ_GUARD=json.dumps({"allow": [str(work)]}))

    def run(tool, ti):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")], input=json.dumps(call(tool, ti, work, proj)),
                           capture_output=True, text=True, env=env, cwd=str(work))
        return json.loads(r.stdout) if r.stdout.strip() else None
    out = run("Read", {"file_path": str(t / "late" / "f")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "outside the working copy" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert run("Read", {"file_path": "src/a.py"}) is None
    assert run("Read", {"file_path": str(proj / "S1" / "tool-results" / "b.txt")}) is None
    # without the variable (outside the lab) the hook never decides on reads
    env.pop("SWARMSCOPE_READ_GUARD")
    assert run("Read", {"file_path": str(t / "late" / "f")}) is None
    # a broken guard module denies reading tools (fail closed), never other tools
    env["SWARMSCOPE_READ_GUARD"] = "{not json"
    assert run("Read", {"file_path": "src/a.py"})["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run("Edit", {"file_path": "src/a.py", "old_string": "a", "new_string": "b"}) is None
