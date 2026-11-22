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


def test_paths_claude_code_would_rewrite_are_denied(tree, monkeypatch):
    # PR #8 review (ac8fd50), reproduced on the real Claude Code 2.1.326 through the lab's grep subagent: Grep/Glob
    # get their path with '~' expanded and edge whitespace trimmed by Claude Code, after the guard; the guard joined
    # '~/...' and ' /abs' to the working copy and let them through, and so did the audit (read_outside)
    t, work, proj = tree
    monkeypatch.setenv("HOME", str(t))
    spec = {"allow": [str(work)]}
    other = str(proj / "S2" / "tool-results")
    for tool, ti in [("Grep", {"pattern": "k", "path": "~/cfg/projects/-w"}),
                     ("Grep", {"pattern": "k", "path": "~"}),
                     ("Grep", {"pattern": "k", "path": " " + other}), ("Grep", {"pattern": "k", "path": other + " "}),
                     ("Grep", {"pattern": "k", "path": "\t" + other}), ("Grep", {"pattern": "k", "path": "﻿" + other}),
                     ("Grep", {"pattern": "k", "path": other + "​"}),
                     ("Grep", {"pattern": "k", "path": str(work) + "\n" + other}),
                     ("Glob", {"pattern": "**/*", "path": "~/cfg"}), ("Glob", {"pattern": "~/cfg/**"}),
                     ("Glob", {"pattern": " " + other + "/*"}), ("Glob", {"pattern": "*", "path": " " + other}),
                     ("LS", {"path": "~/cfg"}), ("Read", {"file_path": "~/cfg/projects/-w/S2.jsonl"}),
                     ("Read", {"file_path": " " + other + "/x.txt"}), ("NotebookRead", {"notebook_path": "~/n.ipynb"}),
                     ("Grep", {"pattern": "k", "path": ["/"]})]:
        reason = readguard.check(call(tool, ti, work, proj), spec)
        assert reason, (tool, ti)
        assert readguard.targets(tool, ti, str(work))[1], (tool, ti)        # the audit sees the same problem


def test_ordinary_paths_still_pass(tree):
    t, work, proj = tree
    spec = {"allow": [str(work)]}
    for tool, ti in [("Grep", {"pattern": "  spaced pattern  ", "path": "src"}),   # the pattern is not a path
                     ("Grep", {"pattern": "k", "path": str(work / "src") + "/"}),
                     ("Grep", {"pattern": "k", "path": "src/a b.py"}),
                     ("Glob", {"pattern": "src/**/*.py"}), ("Read", {"file_path": "src/~x.py"})]:
        assert readguard.check(call(tool, ti, work, proj), spec) is None, (tool, ti)


# own background task outputs (a0-pk-1, run 6fe8f53cc9: Read of <claude tmp>/<project>/<session>/tasks/<id>.output was
# refused; ChatGPT, 22.11: allow only an existing *.output directly in the session's own tasks folder, from a trusted
# root; hook and audit through one function)
@pytest.fixture
def tasks(tree):
    t, work, proj = tree
    root = t / "ctmp" / "claude-501"
    own = root / "-w" / "S1" / "tasks"
    for d in (own, root / "-w" / "S2" / "tasks", root / "-other" / "S1" / "tasks"):
        d.mkdir(parents=True)
        (d / "b1.output").write_text("out")
    (own / "notes.txt").write_text("x")
    (own / "sub").mkdir()
    (own / "sub" / "b2.output").write_text("x")
    (t / "late" / "secret.output").write_text("s")
    os.symlink(t / "late" / "secret.output", own / "link.output")         # a link out of the folder
    os.symlink(own / "b1.output", own / "alias.output")                   # a link to an own output
    return t, work, proj, root, own


def test_own_task_output_is_readable(tasks):
    t, work, proj, root, own = tasks
    spec = {"allow": [str(work)], "task_output_root": str(root)}
    for f in ("b1.output", "alias.output"):
        assert readguard.check(call("Read", {"file_path": str(own / f)}, work, proj), spec) is None, f
    # the agent may name it through a link (macOS /tmp -> /private/tmp): the requested path is resolved
    os.symlink(t / "ctmp", t / "ctmp-link")
    via = t / "ctmp-link" / "claude-501" / "-w" / "S1" / "tasks" / "b1.output"
    assert readguard.check(call("Read", {"file_path": str(via)}, work, proj), spec) is None
    # the root itself is never resolved by the guard: the runner pins it resolved (lab.claude_tmp_root)
    spec2 = {"allow": [str(work)], "task_output_root": str(t / "ctmp-link" / "claude-501")}
    assert readguard.check(call("Read", {"file_path": str(own / "b1.output")}, work, proj), spec2)
    assert readguard.check(call("Read", {"file_path": str(own / "b1.output")}, work, proj),
                           {"allow": [str(work)], "task_output_root": str(root) + "/"})


def test_other_task_outputs_and_neighbours_are_denied(tasks):
    t, work, proj, root, own = tasks
    spec = {"allow": [str(work)], "task_output_root": str(root)}
    denied = [("Read", {"file_path": str(root / "-w" / "S2" / "tasks" / "b1.output")}),      # another session
              ("Read", {"file_path": str(root / "-other" / "S1" / "tasks" / "b1.output")}),  # another project
              ("Read", {"file_path": str(own / "notes.txt")}),                              # a neighbour
              ("Read", {"file_path": str(own / "sub" / "b2.output")}),                      # deeper
              ("Read", {"file_path": str(own / "link.output")}),                            # a link outside
              ("Read", {"file_path": str(own / "missing.output")}),                         # not there
              ("Read", {"file_path": str(own / ".output")}),
              ("Read", {"file_path": str(own)}),
              ("Read", {"file_path": str(own / "sub")}),
              ("NotebookRead", {"notebook_path": str(own / "b1.output")}),                  # Read only
              ("Grep", {"pattern": "o", "path": str(own)}),
              ("Glob", {"pattern": "*.output", "path": str(own)}),
              ("Glob", {"pattern": str(own) + "/*.output"}),
              ("LS", {"path": str(own)})]
    for tool, ti in denied:
        assert readguard.check(call(tool, ti, work, proj), spec), (tool, ti)
    # the root is the runner's, never the requested path's: without it, nothing in tasks/ is readable
    assert readguard.check(call("Read", {"file_path": str(own / "b1.output")}, work, proj), {"allow": [str(work)]})


def test_task_output_needs_plain_names(tasks):
    t, work, proj, root, own = tasks
    spec = {"allow": [str(work)], "task_output_root": str(root)}
    f = {"file_path": str(own / "b1.output")}
    for sid in ("..", "S1/../S1", "", None, "S 1", "S1\n"):
        d = call("Read", f, work, proj, sid="S1")
        d["session_id"] = sid
        assert readguard.check(d, spec), sid
    d = call("Read", f, work, proj)
    d["transcript_path"] = str(t / "S1.jsonl")                      # the transcript's folder is not "-w"
    assert readguard.check(d, spec)
    assert readguard.task_dir(str(root), None, "S1") is None


def test_audit_decides_like_the_hook(tasks):
    # one function for both: the audit (live=False) agrees with the hook on every case where the file still exists
    t, work, proj, root, own = tasks
    tp = str(proj / "S1.jsonl")
    for f in ("b1.output", "alias.output", "notes.txt", "link.output", "sub/b2.output"):
        hook = readguard.check(call("Read", {"file_path": str(own / f)}, work, proj),
                               {"allow": [str(work)], "task_output_root": str(root)})
        audit = readguard.decide("Read", {"file_path": str(own / f)}, str(work), [str(work)], tp, "S1", str(root),
                                 live=False)
        assert (hook is None) == (audit is None), f
    # a file Claude Code removed after the session: judged by place and name
    assert readguard.decide("Read", {"file_path": str(own / "gone.output")}, str(work), [str(work)], tp, "S1",
                            str(root), live=False) is None
    assert readguard.decide("Read", {"file_path": str(own / "gone.txt")}, str(work), [str(work)], tp, "S1",
                            str(root), live=False)


def test_task_output_check_errors_deny(tasks, monkeypatch):
    t, work, proj, root, own = tasks
    monkeypatch.setattr(readguard, "own_task_output", lambda *a, **k: 1 / 0)
    assert readguard.check(call("Read", {"file_path": str(own / "b1.output")}, work, proj),
                           {"allow": [str(work)], "task_output_root": str(root)}).startswith("read guard error")


@pytest.mark.parametrize("level", ["tasks", "session", "project", "root"])
def test_a_folder_replaced_by_a_link_opens_nothing(tasks, level):
    # ChatGPT, 22.11 (P1): own/project/S1/tasks -> other/project/S2/tasks let another session's output through
    t, work, proj, root, own = tasks
    other = t / "elsewhere" / "claude-501" / "-w" / "S1" / "tasks"
    other.mkdir(parents=True)
    (other / "secret.output").write_text("s")
    victim = {"tasks": own, "session": own.parent, "project": own.parent.parent, "root": root}[level]
    target = {"tasks": other, "session": other.parent, "project": other.parent.parent, "root": other.parents[2]}[level]
    os.rename(victim, str(victim) + ".orig")
    os.symlink(target, victim)
    spec = {"allow": [str(work)], "task_output_root": str(root)}
    f = {"file_path": str(own / "secret.output")}
    assert readguard.check(call("Read", f, work, proj), spec), level
    assert readguard.decide("Read", f, str(work), [str(work)], str(proj / "S1.jsonl"), "S1", str(root), live=False)
