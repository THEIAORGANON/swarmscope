"""Rules Engine (mode B of the escalation experiment) + Decision Log.

Pure functions over data the hooks and transcripts already give us: no model calls, no guessing.
The Stop hook (hook.py) calls gate(); everything here is testable with synthetic events.

Rules, evaluated at every Stop of the main session:
  R1 tests_after_change   source changed → a test run must have passed after the last change
  R2 repeat_error         the same normalised error happened again → consult the advisor
  R3 high_risk_change     large or critical source change → consult the advisor after changing code

Decision life cycle (append-only log, one line per record):
  decision: pass | skipped | triggered → requested (enforce) | advised (advise) | deferred | limited
  outcome:  observed | not_observed (+cause when known) | unverified
"Observed" means the action itself was seen (an advisor call in the transcript, a test run in the hooks).
Whether Sonnet followed the advice is never claimed: the advice is encrypted.

Policy file: <project>/.claude/swarmscope-policy.json (presence turns the engine on), or
$SWARMSCOPE_POLICY=observe|advise|enforce. Without either the engine does nothing.
"""
import fnmatch
import json
import os
import re
import time

POLICY_VERSION = "b-1"

DEFAULTS = {
    "mode": "observe",                 # observe | advise | enforce
    "policy_version": POLICY_VERSION,
    "risk": {
        "min_files": 3,                # source files changed
        "min_lines": 50,               # added + deleted source lines
        "critical": ["**/auth/**", "**/*auth*", "**/security/**", "**/migrations/**",
                     "**/*secret*", "**/*crypt*", "**/*permission*", "**/*payment*"],
    },
    # never counted as source: docs, test data, lockfiles
    "ignore": ["*.md", "*.rst", "*.txt", "docs/**", "**/fixtures/**", "**/testdata/**",
               "**/__snapshots__/**", "*.lock", "**/package-lock.json", "LICENSE*", ".gitignore",
               ".claude/**", ".swarmscope/**"],
    "test_command": None,              # e.g. "pytest -q": marks a run as the project's full check
    "test_patterns": [r"\bpytest\b", r"python3? -m (pytest|unittest)", r"\b(npm|pnpm|yarn|bun)( run)? test\b",
                      r"\bgo test\b", r"\bcargo test\b", r"\bmake (test|check)\b", r"\btox\b", r"\bjest\b",
                      r"\bvitest\b", r"\brspec\b", r"\bmvn test\b", r"\bgradlew? test\b", r"\bdeno test\b"],
    "max_blocks_per_turn": 2,
    "defer_on_background_agents": True,
}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def load_policy(cwd):
    """Policy dict, or None when the engine is off for this project."""
    env = os.environ.get("SWARMSCOPE_POLICY")
    path = os.path.join(cwd or ".", ".claude", "swarmscope-policy.json")
    user = None
    if os.path.exists(path):
        try:
            user = json.loads(open(path).read() or "{}")
        except Exception:
            user = {}
    if user is None and not env:
        return None
    pol = json.loads(json.dumps(DEFAULTS))
    for k, v in (user or {}).items():
        if isinstance(v, dict) and isinstance(pol.get(k), dict):
            pol[k].update(v)
        else:
            pol[k] = v
    if env in ("observe", "advise", "enforce"):
        pol["mode"] = env
    if pol["mode"] not in ("observe", "advise", "enforce"):
        pol["mode"] = "observe"
    return pol


# --- helpers -----------------------------------------------------------------------

def _match(path, pats):
    for p in pats:
        if fnmatch.fnmatch(path, p) or (p.startswith("**/") and fnmatch.fnmatch(path, p[3:])):
            return True
        if "/" not in p and fnmatch.fnmatch(os.path.basename(path), p):
            return True
    return False


def normalize_error(text):
    """Make two occurrences of the same failure compare equal: drop numbers, paths, hex, quotes."""
    t = str(text).lower()
    t = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{7,}\b", "#", t)
    t = re.sub(r"(/[\w.\-]+)+", "/…", t)
    t = re.sub(r"\d+(\.\d+)?", "#", t)
    t = re.sub(r"['\"`]", "", t)
    return " ".join(t.split())[:100]


def cmd_head(cmd):
    toks = str(cmd or "").split()
    return " ".join(toks[:2])


def is_test_cmd(cmd, pol):
    return bool(cmd) and any(re.search(p, cmd) for p in pol["test_patterns"])


_MASK = re.compile(r"\|\|\s*(true|:|exit 0)\b|;\s*(true|exit 0)\s*$|\|\s*(tee|head|tail|grep|less|cat)\b")
_FAILED_IN_OUTPUT = re.compile(r"\b([1-9]\d*) (failed|errors?)\b|\bFAILED\b|\bFAIL\b")


def classify_test_run(e, pol):
    """One Bash test command → {"result": passed|failed|unknown, "scope", "why"}."""
    cmd = e.get("cmd") or e.get("arg") or ""
    if e.get("err"):
        return {"result": "failed", "why": "command failed"}
    if _FAILED_IN_OUTPUT.search(e.get("out") or ""):
        return {"result": "failed", "why": "output reports failures"}
    if _MASK.search(cmd):
        return {"result": "unknown", "why": "exit code masked by the shell (|| true, pipe)"}
    if e.get("interrupted"):
        return {"result": "unknown", "why": "interrupted"}
    return {"result": "passed", "why": "exit 0"}


def test_scope(cmd, pol):
    if pol.get("test_command") and cmd.strip() == pol["test_command"].strip():
        return "configured"
    if re.search(r"\S+\.(py|js|ts|tsx|go|rs|rb|java)\b|::|\s-k\s|--testNamePattern|\s-run\s|\s-t\s", cmd):
        return "targeted"
    return "suite"


# --- facts from events ---------------------------------------------------------------

def facts(events, pol):
    """Everything the rules need, extracted once from the hook event stream (main thread only)."""
    f = {"turn": 0, "turn_start": None, "edits": [], "edited_paths": set(), "bash": [], "tests": [],
         "errors": [], "successes": []}
    for e in events:
        if e.get("agent_id"):
            continue                        # subagent activity never drives main-session rules
        ev, ts = e.get("event"), e.get("ts", 0)
        if ev == "UserPromptSubmit" and not str(e.get("prompt", "")).lstrip().startswith("<agent-message"):
            f["turn"] += 1
            f["turn_start"] = ts
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            tool = e.get("tool_name")
            if tool in EDIT_TOOLS and not e.get("err"):
                f["edits"].append(ts)
                if e.get("arg"):
                    f["edited_paths"].add(e["arg"])
            if tool == "Bash":
                cmd = e.get("cmd") or e.get("arg") or ""
                rec = {"ts": ts, "cmd": cmd, "err": e.get("err")}
                f["bash"].append(rec)
                if is_test_cmd(cmd, pol):
                    f["tests"].append(dict(rec, **classify_test_run(e, pol), scope=test_scope(cmd, pol)))
            if e.get("err"):
                f["errors"].append({"ts": ts, "tool": tool, "head": cmd_head(e.get("cmd") or e.get("arg")),
                                    "sig": normalize_error(e["err"]), "raw": e["err"][:120]})
            else:
                f["successes"].append({"ts": ts, "tool": tool, "head": cmd_head(e.get("cmd") or e.get("arg"))})
    return f


def source_changes(diff_files, pol):
    if diff_files is None:
        return None
    return [d for d in diff_files if not d["binary"] and not _match(d["path"], pol["ignore"])]


def last_change_ts(f, diff_files, pol):
    """When source last changed. Edit tools give exact times; files changed by other means (Bash)
    are dated by the last non-test Bash command, which is conservative (never too early)."""
    t = max(f["edits"]) if f["edits"] else None
    src = source_changes(diff_files, pol) or []
    by_other = [d for d in src if d["path"] not in f["edited_paths"]]
    if by_other:
        nontest = [b["ts"] for b in f["bash"] if not is_test_cmd(b["cmd"], pol)]
        if nontest:
            t = max(t or 0, max(nontest))
    return t


# --- rules -----------------------------------------------------------------------------

def r1_tests(f, diff_files, pol, has_tests):
    src = source_changes(diff_files, pol)
    if src is None and not f["edits"]:
        return {"rule": "R1_tests", "state": "skipped", "reason": "no git diff and no edits seen"}
    if src is not None and not src:
        return {"rule": "R1_tests", "state": "pass", "reason": "no source changes"}
    if not has_tests:
        return {"rule": "R1_tests", "state": "skipped", "reason": "project has no tests"}
    t_change = last_change_ts(f, diff_files, pol)
    after = [t for t in f["tests"] if t_change is None or t["ts"] > t_change]
    sig = {"files_changed": len(src) if src is not None else None, "last_change": t_change}
    if after:
        last = after[-1]
        sig.update(observed_test_result=last["result"], test_cmd=last["cmd"][:120], test_scope=last["scope"],
                   test_why=last["why"], test_ts=last["ts"], verified_test_result="not_run")
        if last["result"] == "passed":
            return {"rule": "R1_tests", "state": "pass", "signals": sig,
                    "reason": f"tests passed after the last change ({last['scope']}: {last['cmd'][:60]})"}
        why = {"failed": "the last test run after the change failed",
               "unknown": f"test result unknown: {last['why']}"}[last["result"]]
    else:
        sig.update(observed_test_result="none", verified_test_result="not_run")
        why = "no test run after the last code change"
    return {"rule": "R1_tests", "state": "triggered", "action": "run_tests", "reason": why, "signals": sig}


def r2_repeat_error(f, adv_times, pol):
    open_sigs = {}
    for ev in sorted([("err", x) for x in f["errors"]] + [("ok", x) for x in f["successes"]] +
                     [("edit", {"ts": t}) for t in f["edits"]], key=lambda p: p[1]["ts"]):
        kind, x = ev
        if kind == "err":
            key = (x["tool"], x["head"], x["sig"])
            s = open_sigs.setdefault(key, {"count": 0, "first": x["ts"], "edits_between": 0, "raw": x["raw"]})
            s["count"] += 1
            s["last"] = x["ts"]
        elif kind == "ok":
            for key in [k for k in open_sigs if k[0] == x["tool"] and k[1] == x["head"]]:
                del open_sigs[key]          # the same action succeeded: problem solved, reset
        else:
            for s in open_sigs.values():
                s["edits_between"] += 1
    stuck = [(k, s) for k, s in open_sigs.items() if s["count"] >= 2]
    if not stuck:
        return {"rule": "R2_repeat_error", "state": "pass", "reason": "no repeated unresolved error"}
    for key, s in stuck:
        if not any(t > s["first"] for t in adv_times):
            return {"rule": "R2_repeat_error", "state": "triggered", "action": "consult_advisor",
                    "reason": f"same error x{s['count']} without an advisor call: {s['raw'][:60]}",
                    "signals": {"count": s["count"], "edits_between": s["edits_between"], "tool": key[0],
                                "command": key[1], "signature": key[2], "first": s["first"]}}
    return {"rule": "R2_repeat_error", "state": "pass", "reason": "repeated error, advisor already consulted"}


def r3_high_risk(f, diff_files, adv_times, pol, baseline_ts=None):
    src = source_changes(diff_files, pol)
    if src is None:
        return {"rule": "R3_high_risk", "state": "skipped", "reason": "no git diff: change size unknown"}
    if not src:
        docs_only = bool(diff_files)
        return {"rule": "R3_high_risk", "state": "pass",
                "reason": "only docs / test data changed" if docs_only else "no source changes"}
    r = pol["risk"]
    lines = sum(d["added"] + d["deleted"] for d in src)
    critical = [d["path"] for d in src if _match(d["path"], r["critical"])]
    hits = []
    if len(src) >= r["min_files"]:
        hits.append(f"{len(src)} files")
    if lines >= r["min_lines"]:
        hits.append(f"{lines} lines")
    if critical:
        hits.append("critical: " + ", ".join(critical[:2]))
    sig = {"files": len(src), "lines": lines, "critical": critical[:5], "thresholds": [r["min_files"], r["min_lines"]]}
    if not hits:
        return {"rule": "R3_high_risk", "state": "pass", "reason": "small routine change: Sonnet decides",
                "signals": sig}
    t_first = min(f["edits"]) if f["edits"] else baseline_ts
    if any(t > (t_first or 0) for t in adv_times):
        return {"rule": "R3_high_risk", "state": "pass", "signals": sig,
                "reason": "high-risk change, advisor consulted after code changed"}
    return {"rule": "R3_high_risk", "state": "triggered", "action": "consult_advisor", "signals": sig,
            "reason": "high-risk change (" + "; ".join(hits) + ") without an advisor call"}


# --- decision log -----------------------------------------------------------------------

def load_log(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def append_log(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, default=list) + "\n")


def resolve_outcomes(log, f, adv_times, now, transcript_ok=True):
    """Outcome records for earlier decisions that asked for an action and have no outcome yet.
    Called at the next Stop: by then Sonnet has had its chance to act."""
    done = {r["id"] for r in log if r.get("kind") == "outcome"}
    out = []
    for d in log:
        if d.get("kind") != "decision" or d["id"] in done or not d.get("action"):
            continue
        if d["state"] not in ("triggered", "requested", "advised", "limited"):
            continue
        if d.get("stop_ts", d["ts"]) >= now:
            continue
        t0 = d["ts"]
        rec = {"kind": "outcome", "id": d["id"], "ts": now, "rule": d["rule"], "action": d["action"],
               "after_state": d["state"]}
        if d["action"] == "consult_advisor":
            if not transcript_ok:
                rec.update(outcome="unverified", cause="transcript unavailable")
            elif any(t > t0 for t in adv_times):
                rec.update(outcome="observed", evidence="advisor call in transcript after the decision")
            else:
                rec.update(outcome="not_observed")
        elif d["action"] == "run_tests":
            runs = [t for t in f["tests"] if t["ts"] > t0]
            if runs:
                rec.update(outcome="observed", evidence=runs[-1]["cmd"][:80], result=runs[-1]["result"])
            else:
                rec.update(outcome="not_observed")
        if rec.get("outcome") == "not_observed" and d["state"] == "limited":
            rec["cause"] = "block limit reached, nothing was requested"
        out.append(rec)
    return out


def _deferring_tasks(stop, pol):
    if not pol.get("defer_on_background_agents"):
        return []
    tasks = stop.get("background_tasks") or []
    out = []
    for t in tasks if isinstance(tasks, list) else []:
        if not isinstance(t, dict):
            continue
        typ, status = str(t.get("type", "")).lower(), str(t.get("status", "")).lower()
        # subagents the work depends on; teammates never leave the list, shells/monitors may run forever
        if "agent" in typ and "teammate" not in typ and status in ("running", "pending", "in_progress"):
            out.append({"id": t.get("id"), "type": typ, "description": str(t.get("description", ""))[:60]})
    return out


ACTION_TEXT = {
    "run_tests": "run the project's tests and make sure they pass",
    "consult_advisor": "consult the advisor before finishing",
}


def gate(events, stop, pol, diff_files, adv_times, log, has_tests, sid, now=None, baseline_ts=None,
         transcript_ok=True):
    """Evaluate the rules at a Stop. Returns (records_to_append, hook_output_dict_or_None)."""
    now = now or time.time()
    f = facts(events, pol)
    records = resolve_outcomes(log, f, adv_times, now, transcript_ok)

    results = [r1_tests(f, diff_files, pol, has_tests), r2_repeat_error(f, adv_times, pol),
               r3_high_risk(f, diff_files, adv_times, pol, baseline_ts)]

    turn_start = f["turn_start"] or 0
    blocks = sum(1 for r in log if r.get("kind") == "block" and r["ts"] >= turn_start)
    background = _deferring_tasks(stop, pol)
    mode = pol["mode"]
    gate_state = {"mode": mode, "stop_hook_active": bool(stop.get("stop_hook_active")),
                  "blocks_this_turn": blocks, "max_blocks": pol["max_blocks_per_turn"],
                  "background_agents": background}

    triggered = [r for r in results if r["state"] == "triggered"]
    if triggered:
        if background:
            new_state = "deferred"
        elif mode == "observe":
            new_state = "triggered"
        elif mode == "advise":
            new_state = "advised"
        elif blocks < pol["max_blocks_per_turn"]:
            new_state = "requested"
        else:
            new_state = "limited"
        for r in triggered:
            r["state"] = new_state

    n0 = sum(1 for r in log if r.get("kind") == "decision")
    for i, r in enumerate(results):
        records.append(dict(r, kind="decision", id=f"{sid[:8]}-{n0 + i + 1}", ts=now, stop_ts=now, turn=f["turn"],
                            policy_version=pol["policy_version"], gate=gate_state))

    asked = [r for r in results if r["state"] in ("requested", "advised")]
    if not asked:
        return records, None
    actions = list(dict.fromkeys(r["action"] for r in asked))
    why = "; ".join(r["reason"] for r in asked)
    text = "swarmscope policy: " + " and ".join(ACTION_TEXT[a] for a in actions) + f". Why: {why}."
    if mode == "advise":
        return records, {"systemMessage": text}         # shown to the user, not sent to the model
    records.append({"kind": "block", "ts": now, "turn": f["turn"], "ids": [r["id"] for r in records
                                                                          if r.get("kind") == "decision"
                                                                          and r["state"] == "requested"]})
    return records, {"decision": "block", "reason": text}


def project_has_tests(root, pol):
    if pol.get("test_command"):
        return True
    if not root or not os.path.isdir(root):
        return False
    for name in ("tests", "test", "__tests__", "spec"):
        if os.path.isdir(os.path.join(root, name)):
            return True
    try:
        pkg = json.loads(open(os.path.join(root, "package.json")).read())
        if (pkg.get("scripts") or {}).get("test"):
            return True
    except Exception:
        pass
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", ".venv")]
        if any(fnmatch.fnmatch(x, p) for x in files for p in ("test_*.py", "*_test.py", "*_test.go", "*.test.*",
                                                              "*.spec.*")):
            return True
        if dirpath.count(os.sep) - root.count(os.sep) >= 3:
            dirs[:] = []
    return False
