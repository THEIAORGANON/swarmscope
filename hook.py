#!/usr/bin/env python3
"""Claude Code hook: append a compact event line for the dashboard. Never blocks, never fails."""
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

    # errors: PostToolUseFailure carries "error"; some tool responses carry is_error/error
    err = d.get("error")
    tr = d.get("tool_response")
    if not err and isinstance(tr, dict) and (tr.get("is_error") or tr.get("error")):
        err = tr.get("error") or tr.get("content") or "error"
    if err:
        ev["err"] = " ".join(str(err).split())[:160]

    ensure_dirs()
    with open(session_file(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


try:
    main()
except Exception:
    pass
sys.exit(0)
