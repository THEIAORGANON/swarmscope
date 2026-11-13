"""Rules Engine checks on synthetic events. The three failure types that would invalidate the A/B/C
experiment: missed file changes, failed tests counted as passed, endless Stop loops."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import repostate  # noqa: E402
import rules  # noqa: E402

POL = json.loads(json.dumps(rules.DEFAULTS))


def pol(**kw):
    p = json.loads(json.dumps(POL))
    for k, v in kw.items():
        if isinstance(v, dict):
            p[k].update(v)
        else:
            p[k] = v
    return p


def prompt(ts):
    return {"event": "UserPromptSubmit", "prompt": "do it", "ts": ts}


def edit(ts, path="src/app.py"):
    return {"event": "PostToolUse", "tool_name": "Edit", "arg": path, "ts": ts}


def bash(ts, cmd, err=None, out=None):
    e = {"event": "PostToolUseFailure" if err else "PostToolUse", "tool_name": "Bash", "cmd": cmd, "arg": cmd[:80],
         "ts": ts}
    if err:
        e["err"] = err
    if out:
        e["out"] = out
    return e


def files(*specs):
    return [{"path": p, "added": a, "deleted": d, "binary": False} for p, a, d in specs]


def run(events, diff, p=None, adv=(), log=(), stop=None, has_tests=True, now=100.0, times=None, since=None,
        before=None):
    """diff: baseline→now files (None = no git). times: when source changed; by default the edit events."""
    if diff is None:
        change = {"files": None, "src_times": None, "since_advisor": None}
    else:
        if times is None:
            times = [e["ts"] for e in events if e.get("tool_name") == "Edit"] if any(
                not rules._match(d["path"], POL["ignore"]) for d in diff) else []
        change = {"files": diff, "src_times": times, "since_advisor": since, "before_advisor": before}
    return rules.gate(events, stop or {}, p or pol(), change, list(adv), list(log), has_tests, "sess1234", now=now)


def by_rule(records, rule):
    return [r for r in records if r.get("kind") == "decision" and r["rule"] == rule][-1]


# --- 1. file changes are not missed -----------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return tmp_path, git


def objects_in_repo(root):
    return sorted(str(p) for p in (root / ".git" / "objects").rglob("*") if p.is_file())


def test_snapshot_sees_untracked_staged_committed_and_bash_changes(repo, tmp_path_factory):
    root, git = repo
    store = str(tmp_path_factory.mktemp("store"))
    (root / "a.py").write_text("a\npre-existing\n")          # dirty before the session: not ours
    base = repostate.snapshot(str(root), store)
    (root / "new.py").write_text("x = 1\ny = 2\n")            # untracked
    (root / "s.py").write_text("s\n")
    git("add", "s.py")                                        # staged
    (root / "c.py").write_text("c\n")
    git("add", "c.py")
    git("commit", "-qm", "agent commit")                      # committed by the agent
    subprocess.run("echo bash >> a.py", shell=True, cwd=root)  # changed by Bash
    (root / "x.log").write_text("ignored")
    index_before = (root / ".git" / "index").read_bytes()
    cur = repostate.snapshot(str(root), store)
    d = {f["path"]: f for f in repostate.diff(cur["root"], store, base["tree"], cur["tree"])}
    assert set(d) == {"a.py", "new.py", "s.py", "c.py"}
    assert d["a.py"]["added"] == 1                            # only the Bash line, not the pre-existing one
    assert (root / ".git" / "index").read_bytes() == index_before


def test_snapshot_writes_nothing_into_the_repo(repo, tmp_path_factory):
    root, _ = repo
    store = str(tmp_path_factory.mktemp("store"))
    before = objects_in_repo(root)
    (root / "new_module.py").write_text("print('new')\n")
    repostate.snapshot(str(root), store)
    assert objects_in_repo(root) == before


def test_snapshot_skips_secrets_big_files_and_own_folders(repo, tmp_path_factory):
    root, _ = repo
    store = str(tmp_path_factory.mktemp("store"))
    base = repostate.snapshot(str(root), store)
    (root / ".env").write_text("API_KEY=secret\n")
    (root / "prod.pem").write_text("-----BEGIN-----\n")
    (root / "dump.bin").write_bytes(b"0" * (repostate.MAX_FILE_BYTES + 1))
    (root / ".swarmscope").mkdir()
    (root / ".swarmscope" / "events.jsonl").write_text("{}\n")
    (root / "custom_state").mkdir()
    (root / "custom_state" / "x.json").write_text("{}\n")
    (root / "ok.py").write_text("ok\n")
    cur = repostate.snapshot(str(root), store, exclude=["custom_state"])
    paths = {f["path"] for f in repostate.diff(cur["root"], store, base["tree"], cur["tree"])}
    assert paths == {"ok.py"}
    assert {".env", "prod.pem", "dump.bin"} <= set(cur["skipped"])


def test_reverted_change_nets_out(repo, tmp_path_factory):
    root, _ = repo
    store = str(tmp_path_factory.mktemp("store"))
    base = repostate.snapshot(str(root), store)
    (root / "a.py").write_text("changed\n")
    (root / "a.py").write_text("a\n")
    cur = repostate.snapshot(str(root), store)
    assert repostate.diff(cur["root"], store, base["tree"], cur["tree"]) == []


def test_fresh_git_init_without_tracked_files(tmp_path):
    """Regression: older git fails `add -u -- .` when nothing is tracked yet (live run on macOS)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    store = str(tmp_path / ".swarmscope" / "store")
    base = repostate.snapshot(str(tmp_path), store)
    assert base["git"], base
    (tmp_path / "x.py").write_text("x\n")
    cur = repostate.snapshot(str(tmp_path), store)
    assert [f["path"] for f in repostate.diff(cur["root"], store, base["tree"], cur["tree"])] == ["x.py"]


def test_tracked_files_in_excluded_folders_are_ignored(repo, tmp_path_factory):
    root, git = repo
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    git("add", ".claude/settings.json")
    git("commit", "-qm", "settings")
    store = str(tmp_path_factory.mktemp("store"))
    base = repostate.snapshot(str(root), store)
    (root / ".claude" / "settings.json").write_text('{"changed": 1}')
    cur = repostate.snapshot(str(root), store)
    assert repostate.diff(cur["root"], store, base["tree"], cur["tree"]) == []


def test_not_a_repo(tmp_path):
    assert repostate.snapshot(str(tmp_path), str(tmp_path / "store"))["git"] is False


def test_change_after_passing_tests_triggers_r1():
    ev = [prompt(1), bash(5, "pytest -q", out="3 passed"), bash(8, "sed -i s/a/b/ src/app.py")]
    recs, _ = run(ev, files(("src/app.py", 1, 1)), times=[8])
    r1 = by_rule(recs, "R1_tests")
    assert r1["state"] == "triggered" and r1["signals"]["last_change"] == 8


def test_git_status_after_passing_tests_is_not_a_change():
    """Regression: a read-only Bash command after a passing run must not demand another run."""
    ev = [prompt(1), bash(4, "sed -i s/a/b/ src/app.py"), bash(5, "pytest -q", out="3 passed"),
          bash(6, "git status")]
    assert by_rule(run(ev, files(("src/app.py", 1, 1)), times=[4])[0], "R1_tests")["state"] == "pass"


def test_no_snapshots_means_skipped_not_pass():
    recs, _ = run([prompt(1), edit(5), bash(6, "pytest -q", out="1 passed")], None)
    assert by_rule(recs, "R1_tests")["state"] == "skipped"
    assert by_rule(recs, "R3_high_risk")["state"] == "skipped"


# --- 2. failed tests are never counted as passed ------------------------------------------

@pytest.mark.parametrize("test_event,expected", [
    (bash(10, "pytest -q", err="exit code 1: 2 failed"), "failed"),
    (bash(10, "pytest -q || true", out="2 failed, 5 passed"), "failed"),
    (bash(10, "pytest -q || true"), "unknown"),
    (bash(10, "pytest -q | tail -5", out="all good"), "unknown"),
    (bash(10, "npm test", out="Tests: 1 failed, 9 passed"), "failed"),
    (bash(10, "pytest -q", out="done"), "unknown"),            # clean exit, no runner summary
    (bash(10, "pytest -q"), "unknown"),                        # no output captured at all
])

def test_bad_or_unknown_test_runs_trigger_r1(test_event, expected):
    recs, _ = run([prompt(1), edit(5), test_event], files(("src/app.py", 3, 1)))
    r1 = by_rule(recs, "R1_tests")
    assert r1["state"] == "triggered"
    assert r1["signals"]["observed_test_result"] == expected


@pytest.mark.parametrize("cmd,out,expected", [
    ("uv run python -m pytest -q 2>&1 | tail -15", "Installed 10 packages\n.....\n19 passed in 0.05s", "passed"),
    ("pytest -q || true", "5 passed in 0.1s", "passed"),
    ("pytest -q | grep passed", "19 passed", "unknown"),            # grep may have dropped failures
    ("pytest -q | head -3", "....", "unknown"),                     # summary cut off
])
def test_masked_exit_falls_back_to_runner_summary(cmd, out, expected):
    """Live run 13.11: Sonnet ran `pytest | tail -15`, output said 19 passed, b-2 called it unknown."""
    assert rules.classify_test_run(bash(10, cmd, out=out), POL)["result"] == expected


def test_passed_needs_a_success_event():
    e = dict(bash(10, "pytest -q", out="5 passed"), event="PreToolUse")
    assert rules.classify_test_run(e, POL)["result"] == "unknown"
    assert rules.classify_test_run(dict(e, event="PostToolUse"), POL)["result"] == "passed"


def test_test_before_last_edit_does_not_count():
    recs, _ = run([prompt(1), bash(3, "pytest -q", out="7 passed"), edit(5)], files(("src/app.py", 3, 1)))
    assert by_rule(recs, "R1_tests")["signals"]["observed_test_result"] == "none"


def test_passing_targeted_run_passes_but_records_scope():
    recs, _ = run([prompt(1), edit(5), bash(6, "pytest tests/test_x.py -q", out="2 passed")],
                  files(("src/app.py", 3, 1)))
    r1 = by_rule(recs, "R1_tests")
    assert r1["state"] == "pass" and r1["signals"]["test_scope"] == "targeted"
    assert r1["signals"]["verified_test_result"] == "not_run"


def test_no_tests_in_project_skips_r1():
    recs, _ = run([prompt(1), edit(5)], files(("src/app.py", 3, 1)), has_tests=False)
    assert by_rule(recs, "R1_tests")["state"] == "skipped"


def test_docs_only_change_needs_nothing():
    recs, _ = run([prompt(1), edit(5, "README.md")], files(("README.md", 200, 10), ("docs/x.md", 80, 0)))
    assert by_rule(recs, "R1_tests")["state"] == "pass"
    assert by_rule(recs, "R3_high_risk")["state"] == "pass"


# --- R2 repeat error ----------------------------------------------------------------------

def test_repeat_error_without_edit_triggers():
    ev = [prompt(1), bash(2, "pytest -q", err="fixture 'token' not found at line 12"),
          bash(4, "pytest -q", err="fixture 'token' not found at line 14")]
    r2 = by_rule(run(ev, [])[0], "R2_repeat_error")
    assert r2["state"] == "triggered" and r2["signals"]["edits_between"] == 0


def test_repeat_error_records_edits_between():
    ev = [prompt(1), bash(2, "pytest -q", err="boom 1"), edit(3), bash(4, "pytest -q", err="boom 2")]
    assert by_rule(run(ev, [])[0], "R2_repeat_error")["signals"]["edits_between"] == 1


def test_success_resets_repeat_error():
    ev = [prompt(1), bash(2, "pytest -q", err="boom"), bash(3, "pytest -q", err="boom"),
          bash(4, "pytest -q", out="5 passed")]
    assert by_rule(run(ev, [])[0], "R2_repeat_error")["state"] == "pass"


def test_advisor_after_first_error_satisfies_r2():
    ev = [prompt(1), bash(2, "pytest -q", err="boom"), bash(4, "pytest -q", err="boom")]
    assert by_rule(run(ev, [], adv=[3])[0], "R2_repeat_error")["state"] == "pass"


def test_subagent_errors_are_ignored():
    ev = [prompt(1)] + [dict(bash(t, "rg x", err="EACCES posix_spawn rg"), agent_id="a1") for t in (2, 3, 4)]
    assert by_rule(run(ev, [])[0], "R2_repeat_error")["state"] == "pass"


# --- R3 high risk -----------------------------------------------------------------------------

def test_three_files_trigger_r3():
    d = files(("src/a.py", 2, 0), ("src/b.py", 2, 0), ("src/c.py", 2, 0))
    r3 = by_rule(run([prompt(1), edit(5)], d)[0], "R3_high_risk")
    assert r3["state"] == "triggered" and "3 files" in r3["reason"]


def test_fifty_lines_trigger_r3_and_thresholds_are_configurable():
    d = files(("src/a.py", 40, 10))
    assert by_rule(run([prompt(1), edit(5)], d)[0], "R3_high_risk")["state"] == "triggered"
    p = pol(risk={"min_lines": 100})
    assert by_rule(run([prompt(1), edit(5)], d, p=p)[0], "R3_high_risk")["state"] == "pass"


def test_small_change_in_critical_module_triggers_r3():
    r3 = by_rule(run([prompt(1), edit(5, "src/auth/session.py")], files(("src/auth/session.py", 1, 1)))[0],
                 "R3_high_risk")
    assert r3["state"] == "triggered" and "critical" in r3["reason"]


def test_small_routine_change_left_to_sonnet():
    assert by_rule(run([prompt(1), edit(5)], files(("src/a.py", 3, 1)))[0], "R3_high_risk")["state"] == "pass"


def test_advisor_before_first_change_does_not_satisfy_r3_but_after_does():
    d = files(("src/a.py", 60, 0))
    assert by_rule(run([prompt(1), edit(5)], d, adv=[3])[0], "R3_high_risk")["state"] == "triggered"
    assert by_rule(run([prompt(1), edit(5)], d, adv=[7], since=[])[0], "R3_high_risk")["state"] == "pass"


def test_advisor_before_bash_only_change_does_not_count():
    """Regression: no Edit events, the change came from Bash at t=8; an advisor call at t=3 is not a review."""
    d = files(("src/a.py", 60, 0))
    r3 = by_rule(run([prompt(1), bash(8, "python gen.py")], d, adv=[3], times=[8])[0], "R3_high_risk")
    assert r3["state"] == "triggered"


def test_big_changes_after_the_last_consult_need_another():
    d = files(("src/a.py", 60, 0), ("src/b.py", 40, 0))
    r3 = by_rule(run([prompt(1)], d, adv=[6], times=[5, 9], since=files(("src/b.py", 90, 20)))[0], "R3_high_risk")
    assert r3["state"] == "triggered" and "after the last advisor call" in r3["reason"]
    r3 = by_rule(run([prompt(1)], d, adv=[6], times=[5, 9], since=files(("src/b.py", 2, 1)))[0], "R3_high_risk")
    assert r3["state"] == "pass"


def test_applying_advice_to_reviewed_critical_file_is_not_new_risk():
    """Live run 13.11: after a consult Sonnet changed client.py (already changed before the consult,
    55 lines). b-3 demanded another consult, then a third one for a 5-line metrics.py tweak."""
    d = files(("src/auth/client.py", 120, 10), ("src/auth/metrics.py", 40, 0), ("tests/test_m.py", 30, 0))
    before = files(("src/auth/client.py", 70, 5), ("src/auth/metrics.py", 35, 0), ("tests/test_m.py", 25, 0))
    r3 = by_rule(run([prompt(1)], d, adv=[6], times=[5, 9], since=files(("src/auth/client.py", 50, 5)),
                     before=before)[0], "R3_high_risk")
    assert r3["state"] == "pass"


def test_new_critical_file_after_consult_is_new_risk():
    d = files(("src/a.py", 60, 0), ("src/auth/token.py", 3, 0))
    r3 = by_rule(run([prompt(1)], d, adv=[6], times=[5, 9], since=files(("src/auth/token.py", 3, 0)),
                     before=files(("src/a.py", 60, 0)))[0], "R3_high_risk")
    assert r3["state"] == "triggered" and "critical" in r3["reason"]


def test_large_change_after_consult_still_counts():
    d = files(("src/a.py", 200, 0))
    r3 = by_rule(run([prompt(1)], d, adv=[6], times=[5, 9], since=files(("src/a.py", 120, 0)),
                     before=files(("src/a.py", 80, 0)))[0], "R3_high_risk")
    assert r3["state"] == "triggered"


def test_write_and_test_in_one_command_counts_as_tested():
    """Live run 13.11: `cat >> tests/x.py <<EOF … EOF` + pytest in one Bash call; b-3 asked for another run."""
    cmd = "cat >> tests/test_m.py <<'EOF'\ndef test_x(): pass\nEOF\nuv run python -m pytest -q 2>&1 | tail -3"
    ev = [prompt(1), bash(452, cmd, out="32 passed in 0.07s")]
    assert by_rule(run(ev, files(("src/a.py", 3, 0)), times=[452])[0], "R1_tests")["state"] == "pass"


def test_heredoc_body_is_not_a_test_run():
    """Live run 13.11: writing a test file containing `pytest.raises` was taken for a pytest run."""
    cmd = "cat >> tests/test_m.py <<'EOF'\nwith pytest.raises(X):\n    pass\nEOF"
    assert not rules.is_test_cmd(cmd, POL)
    assert rules.is_test_cmd(cmd + "\npytest -q | tail -3", POL)


def test_long_command_keeps_its_tail(tmp_path):
    body = "x = 1\n" * 400
    cmd = f"cat > tests/test_big.py <<'EOF'\n{body}EOF\npython -m pytest -q 2>&1 | tail -5"
    env = dict(os.environ, SWARMSCOPE_DIR=str(tmp_path))
    env.pop("SWARMSCOPE_POLICY", None)
    payload = {"session_id": "long", "cwd": str(tmp_path), "hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": cmd}, "tool_response": {"stdout": "3 passed"}}
    subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")], input=json.dumps(payload), text=True,
                   env=env, check=True)
    ev = json.loads((tmp_path / "events" / "long.jsonl").read_text().splitlines()[-1])
    assert ev["cmd"].endswith("tail -5") and rules.test_runs_last(ev["cmd"], POL)


def test_test_then_write_in_one_command_is_not_tested():
    cmd = "pytest -q && sed -i s/a/b/ src/a.py"
    ev = [prompt(1), bash(452, cmd, out="5 passed")]
    assert by_rule(run(ev, files(("src/a.py", 3, 0)), times=[452])[0], "R1_tests")["state"] == "triggered"


def test_unknown_tree_at_consult_is_skipped_not_pass():
    d = files(("src/a.py", 60, 0))
    assert by_rule(run([prompt(1)], d, adv=[6], times=[5], since=None)[0], "R3_high_risk")["state"] == "skipped"


# --- 3. no endless Stop loops; modes ------------------------------------------------------------

BIG = files(("src/a.py", 60, 0))


def test_observe_never_outputs():
    recs, out = run([prompt(1), edit(5)], BIG, p=pol(mode="observe"))
    assert out is None and by_rule(recs, "R3_high_risk")["state"] == "triggered"


def test_advise_talks_to_user_only():
    _, out = run([prompt(1), edit(5)], BIG, p=pol(mode="advise"))
    assert set(out) == {"systemMessage"}


def test_enforce_blocks_at_most_max_times_per_turn():
    p = pol(mode="enforce")
    ev, log, outs = [prompt(1), edit(5)], [], []
    for i in range(4):
        recs, out = run(ev, BIG, p=p, log=log, now=10.0 + i, stop={"stop_hook_active": i > 0})
        log += recs
        outs.append(out)
    assert [o and o.get("decision") for o in outs] == ["block", "block", None, None]
    assert by_rule(log, "R3_high_risk")["state"] == "limited"


def test_block_budget_resets_on_new_user_turn():
    p = pol(mode="enforce")
    log = []
    for i in range(3):
        recs, _ = run([prompt(1), edit(5)], BIG, p=p, log=log, now=10.0 + i)
        log += recs
    _, out = run([prompt(1), edit(5), prompt(50), edit(55)], BIG, p=p, log=log, now=60.0)
    assert out and out["decision"] == "block"


def test_background_subagent_defers_gate():
    stop = {"background_tasks": [{"id": "x", "type": "local_agent", "status": "running"}]}
    recs, out = run([prompt(1), edit(5)], BIG, p=pol(mode="enforce"), stop=stop)
    assert out is None and by_rule(recs, "R3_high_risk")["state"] == "deferred"


def test_long_running_shell_and_teammate_do_not_defer():
    stop = {"background_tasks": [{"id": "m", "type": "local_bash", "status": "running"},
                                 {"id": "t", "type": "in_process_teammate", "status": "running"}]}
    _, out = run([prompt(1), edit(5)], BIG, p=pol(mode="enforce"), stop=stop)
    assert out and out["decision"] == "block"


# --- decision life cycle ------------------------------------------------------------------------

def test_outcomes_observed_and_not_observed():
    p = pol(mode="enforce")
    log, _ = run([prompt(1), edit(5)], BIG, p=p, now=10.0)
    recs, _ = run([prompt(1), edit(5)], BIG, p=p, log=log, adv=[12], now=20.0)
    oc = {o["rule"]: o for o in recs if o.get("kind") == "outcome"}
    assert oc["R3_high_risk"]["outcome"] == "observed"
    assert oc["R1_tests"]["outcome"] == "not_observed"             # no test run after the request
    recs, _ = run([prompt(1), edit(5)], BIG, p=p, log=log, now=20.0)
    oc = {o["rule"]: o for o in recs if o.get("kind") == "outcome"}
    assert oc["R3_high_risk"]["outcome"] == "not_observed" and "cause" not in oc["R3_high_risk"]


def test_limited_decision_is_a_followup_with_cause():
    p = pol(mode="enforce", max_blocks_per_turn=0)
    log, _ = run([prompt(1), edit(5)], BIG, p=p, now=10.0)
    recs, _ = run([prompt(1), edit(5)], BIG, p=p, log=log, now=20.0)
    assert not [r for r in recs if r.get("kind") == "outcome"]
    fu = [r for r in recs if r.get("kind") == "followup" and r["rule"] == "R3_high_risk"][0]
    assert fu["outcome"] == "not_observed" and "block limit" in fu["cause"]


@pytest.mark.parametrize("mode", ["observe", "advise"])
def test_unenforced_decisions_never_become_compliance_outcomes(mode):
    """Regression: a decision Sonnet was never told about gets a followup, not an outcome."""
    log, _ = run([prompt(1), edit(5)], BIG, p=pol(mode=mode), now=10.0)
    recs, _ = run([prompt(1), edit(5)], BIG, p=pol(mode=mode), log=log, now=20.0)
    assert not [r for r in recs if r.get("kind") == "outcome"]
    assert {r["rule"] for r in recs if r.get("kind") == "followup"} == {"R1_tests", "R3_high_risk"}


def test_unverified_without_transcript():
    p = pol(mode="enforce")
    log, _ = run([prompt(1), edit(5)], BIG, p=p, now=10.0)
    change = {"files": BIG, "src_times": [5], "since_advisor": None}
    recs, _ = rules.gate([prompt(1), edit(5)], {}, p, change, [], log, True, "sess1234", now=20.0, transcript_ok=False)
    oc = {o["rule"]: o for o in recs if o.get("kind") == "outcome"}
    assert oc["R3_high_risk"]["outcome"] == "unverified"


def test_decision_records_carry_id_policy_version_and_gate_state():
    recs, _ = run([prompt(1), edit(5)], BIG, p=pol(mode="enforce"))
    d = by_rule(recs, "R3_high_risk")
    assert d["id"].startswith("sess1234-") and d["policy_version"] == rules.POLICY_VERSION
    assert {"mode", "stop_hook_active", "blocks_this_turn", "background_agents"} <= set(d["gate"])


# --- end to end through hook.py -------------------------------------------------------------------

def hook(payload, env):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "hook.py")], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_hook_end_to_end_enforce(repo, tmp_path_factory):
    root, _ = repo
    (root / ".claude").mkdir()
    (root / ".claude" / "swarmscope-policy.json").write_text(json.dumps({"mode": "enforce"}))
    (root / "tests").mkdir()
    env = dict(os.environ, SWARMSCOPE_DIR=str(tmp_path_factory.mktemp("sw")))
    env.pop("SWARMSCOPE_POLICY", None)
    common = {"session_id": "e2e-sess", "cwd": str(root)}
    assert hook(dict(common, hook_event_name="UserPromptSubmit", prompt="go"), env) is None
    (root / "src").mkdir()
    (root / "src" / "big.py").write_text("x = 1\n" * 60)               # written via "Bash"
    hook(dict(common, hook_event_name="PostToolUse", tool_name="Bash",
              tool_input={"command": "python gen.py"}, tool_response={"stdout": "ok"}), env)
    out1 = hook(dict(common, hook_event_name="Stop", stop_hook_active=False), env)
    out2 = hook(dict(common, hook_event_name="Stop", stop_hook_active=True), env)
    out3 = hook(dict(common, hook_event_name="Stop", stop_hook_active=True), env)
    assert out1["decision"] == "block" and "advisor" in out1["reason"] and "tests" in out1["reason"]
    assert out2["decision"] == "block" and out3 is None
    log = rules.load_log(os.path.join(env["SWARMSCOPE_DIR"], "decisions", "e2e-sess.jsonl"))
    assert any(r.get("kind") == "outcome" and r["outcome"] == "not_observed" for r in log)


def test_hook_fails_open_on_broken_policy_and_is_off_without_one(repo, tmp_path_factory):
    root, _ = repo
    env = dict(os.environ, SWARMSCOPE_DIR=str(tmp_path_factory.mktemp("sw")))
    env.pop("SWARMSCOPE_POLICY", None)
    common = {"session_id": "s2", "cwd": str(root)}
    hook(dict(common, hook_event_name="UserPromptSubmit", prompt="go"), env)
    assert not os.path.exists(os.path.join(env["SWARMSCOPE_DIR"], "baseline"))     # engine off
    (root / ".claude").mkdir()
    (root / ".claude" / "swarmscope-policy.json").write_text("{not json")
    (root / "big.py").write_text("x\n" * 99)
    assert hook(dict(common, hook_event_name="Stop"), env) is None                   # observe default, no crash


def test_hook_end_to_end_timeline(repo, tmp_path_factory):
    """Bash edit → passing tests → git status → Stop: nothing to require. Then an external change that no
    tool event reported → Stop: R1 must notice it."""
    root, _ = repo
    (root / ".claude").mkdir()
    (root / ".claude" / "swarmscope-policy.json").write_text(json.dumps({"mode": "enforce"}))
    (root / "tests").mkdir()
    env = dict(os.environ, SWARMSCOPE_DIR=str(root / ".swarmscope"))     # inside the repo, like test2
    env.pop("SWARMSCOPE_POLICY", None)
    c = {"session_id": "tl-sess", "cwd": str(root)}
    hook(dict(c, hook_event_name="UserPromptSubmit", prompt="go"), env)
    subprocess.run("echo 'b = 2' >> a.py", shell=True, cwd=root)
    hook(dict(c, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "echo 'b = 2' >> a.py"},
              tool_response={"stdout": ""}), env)
    hook(dict(c, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q"},
              tool_response={"stdout": "....\n4 passed in 0.03s", "stderr": ""}), env)
    hook(dict(c, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "git status"},
              tool_response={"stdout": " M a.py"}), env)
    assert hook(dict(c, hook_event_name="Stop"), env) is None
    log = rules.load_log(str(root / ".swarmscope" / "decisions" / "tl-sess.jsonl"))
    assert {r["rule"]: r["state"] for r in log if r.get("kind") == "decision"}["R1_tests"] == "pass"
    subprocess.run("echo 'c = 3' >> a.py", shell=True, cwd=root)           # external, no tool event
    out = hook(dict(c, hook_event_name="Stop"), env)
    assert out and "tests" in out["reason"]
