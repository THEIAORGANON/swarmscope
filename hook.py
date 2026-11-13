#!/usr/bin/env python3
"""Claude Code hook: append a compact event line for the dashboard.

Never fails. Blocks only when the project turned the policy engine on in enforce mode
(.claude/swarmscope-policy.json, see rules.py); any error inside the engine lets Claude stop normally."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from common import ensure_dirs, session_file
    d = json.load(sys.stdin)
    sid = d.get("session_id") or "unknown"
    ev = {"ts": time.time(), "event": d.get("hook_event_name")}
    for k in ("tool_name", "tool_use_id", "agent_id", "agent_type", "source",
              "transcript_path", "agent_transcript_path"):
        if d.get(k):
            ev[k] = d[k]

    ti = d.get("tool_input")
    if isinstance(ti, dict):
        if ti.get("subagent_type"):
            ev["subagent_type"] = ti["subagent_type"]
        if ti.get("description"):
            ev["description"] = str(ti["description"])[:80]
        for k in ("file_path", "pattern", "command", "path", "url", "query"):
            if ti.get(k):
                arg = str(ti[k])
                if k == "file_path":
                    arg = os.path.relpath(arg, d.get("cwd") or "/") if os.path.isabs(arg) else arg
                ev["arg"] = arg[:80]
                break

        # code for the live-code panel
        old = new = None
        if "new_string" in ti:
            old, new = ti.get("old_string", ""), ti.get("new_string", "")
        elif "content" in ti and d.get("tool_name") == "Write":
            old, new = "", ti.get("content", "")
        elif isinstance(ti.get("edits"), list):
            old = "\n".join(str(x.get("old_string", "")) for x in ti["edits"] if isinstance(x, dict))
            new = "\n".join(str(x.get("new_string", "")) for x in ti["edits"] if isinstance(x, dict))
        if new is not None and d.get("hook_event_name") == "PreToolUse":
            ev["old"], ev["new"] = str(old)[:3000], str(new)[:3000]

    if d.get("prompt"):
        ev["prompt"] = str(d["prompt"])[:120]

    # Bash: full command + output tail, so rules can tell a test run from a test result
    if d.get("tool_name") == "Bash" and isinstance(ti, dict) and ti.get("command"):
        ev["cmd"] = str(ti["command"])[:400]
    tr0 = d.get("tool_response")
    if d.get("tool_name") == "Bash" and tr0 is not None and not isinstance(tr0, dict):
        ev["resp_type"] = type(tr0).__name__
        ev["out"] = str(tr0)[-600:]
    if d.get("tool_name") == "Bash" and isinstance(tr0, dict):
        out = (str(tr0.get("stdout") or "") + "\n" + str(tr0.get("stderr") or "")).strip()
        if out:
            ev["out"] = out[-600:]
        if tr0.get("interrupted"):
            ev["interrupted"] = True
        ev["resp_keys"] = sorted(tr0.keys())[:12]
    if d.get("hook_event_name") == "Stop":
        ev["stop_hook_active"] = bool(d.get("stop_hook_active"))
        if isinstance(d.get("background_tasks"), list):
            ev["background_tasks"] = [{k: t.get(k) for k in ("id", "type", "status")}
                                      for t in d["background_tasks"] if isinstance(t, dict)][:10]

    # errors: PostToolUseFailure carries "error"; some tool responses carry is_error/error
    err = d.get("error")
    tr = d.get("tool_response")
    if not err and isinstance(tr, dict) and (tr.get("is_error") or tr.get("error")):
        err = tr.get("error") or tr.get("content") or "error"
    if not err and isinstance(tr, dict):
        rc = next((tr[k] for k in ("exit_code", "exitCode", "returnCode") if isinstance(tr.get(k), int)), 0)
        if rc:
            err = f"exit code {rc}: " + str(tr.get("stderr") or tr.get("stdout") or "")[-120:]
    if err:
        ev["err"] = " ".join(str(err).split())[:160]

    ensure_dirs()
    with open(session_file(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    try:
        policy(d, sid, ev)
    except Exception:
        pass                                    # fail-open: the policy engine never breaks a session


MUTATING = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}


def policy(d, sid, ev):
    """Rules Engine: baseline snapshot at the session's first event, a timeline snapshot after every tool
    call that can change files, the gate at Stop."""
    from common import BASE, advisor_activity, load_events
    import repostate
    import rules
    cwd = d.get("cwd") or os.getcwd()
    pol = rules.load_policy(cwd)
    if not pol:
        return
    store = str(BASE / "gitobj" / sid)
    tl_file = BASE / "baseline" / f"{sid}.timeline.jsonl"
    tl_file.parent.mkdir(parents=True, exist_ok=True)

    def excludes(root):
        try:
            rel = os.path.relpath(str(BASE.resolve()), os.path.realpath(root))
            return [] if rel.startswith("..") else [rel]
        except Exception:
            return []

    def snap():
        root = cwd
        try:
            import subprocess
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                                  text=True, timeout=5).stdout.strip() or cwd
        except Exception:
            pass
        return repostate.snapshot(cwd, store, excludes(root))

    timeline = rules.load_log(str(tl_file))
    if not timeline:
        b = snap()
        timeline = [dict(b, ts=ev["ts"], src=False, kind="baseline")]
        rules.append_log(str(tl_file), timeline)

    base = timeline[0]
    tool_done = ev["event"] in ("PostToolUse", "PostToolUseFailure") and ev.get("tool_name") in MUTATING
    if not base.get("git") or not (tool_done or ev["event"] == "Stop"):
        if ev["event"] == "Stop" and not base.get("git"):
            run_gate(d, sid, ev, pol, {}, BASE, advisor_activity, load_events, rules, cwd, base)
        return

    cur = snap()
    if not cur.get("git"):
        return
    last = timeline[-1]
    if cur["tree"] != last["tree"]:
        step = repostate.diff(cur["root"], store, last["tree"], cur["tree"])
        entry = {"kind": "change", "ts": ev["ts"], "tree": cur["tree"], "git": True,
                 "src": bool(rules.source_changes(step, pol)), "by": ev.get("tool_name") or ev["event"]}
        rules.append_log(str(tl_file), [entry])
        timeline.append(entry)
    if ev["event"] != "Stop":
        return

    tp = d.get("transcript_path")
    adv = advisor_activity(tp) if tp else {"times": []}
    times = adv.get("times") or []
    since = None
    if times:
        t_a = max(times)
        at = [e for e in timeline if e["ts"] <= t_a] or [base]
        since = repostate.diff(cur["root"], store, at[-1]["tree"], cur["tree"])
    change = {"files": repostate.diff(cur["root"], store, base["tree"], cur["tree"]),
              "src_times": [e["ts"] for e in timeline if e.get("src")], "since_advisor": since,
              "skipped": cur.get("skipped") or []}
    run_gate(d, sid, ev, pol, change, BASE, lambda _: adv, load_events, rules, cwd, base)


def run_gate(d, sid, ev, pol, change, BASE, advisor_activity, load_events, rules, cwd, base):
    tp = d.get("transcript_path")
    adv = advisor_activity(tp) if tp else {"times": []}
    logf = str(BASE / "decisions" / f"{sid}.jsonl")
    records, out = rules.gate(load_events(sid), d, pol, change, adv.get("times") or [], rules.load_log(logf),
                              rules.project_has_tests(base.get("root") or cwd, pol), sid, now=ev["ts"],
                              transcript_ok=bool(tp and os.path.exists(tp)))
    rules.append_log(logf, records)
    if out:
        sys.stdout.write(json.dumps(out))

try:
    main()
except Exception:
    pass
sys.exit(0)
