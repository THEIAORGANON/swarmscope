"""Optional allow-list for the tools that read files (lab runs only).

When SWARMSCOPE_READ_GUARD is set (JSON: {"allow": [dirs]}), hook.py asks check() at every PreToolUse of a
reading tool and denies the call unless every path it can reach lies in an allowed dir or in the session's own
spilled tool outputs (<project folder>/<session id>/tool-results, the project folder being the transcript's).
The settings' Read denies cannot name what appears only after they were written; this guard decides on the
path as it is when the tool runs. Paths are resolved through symlinks. Errors deny (fail closed)."""
import json
import os

READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
GLOB_CHARS = set("*?[{")


def _real(path, cwd):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or "/", path))


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def targets(tool, ti, cwd):
    """Every path the call can read: (paths, problem). A Glob pattern can leave its path: its fixed leading part
    joins the path, and a '..' after a wildcard is refused (its reach cannot be bounded)."""
    ti = ti if isinstance(ti, dict) else {}
    if tool == "Read":
        return [_real(ti.get("file_path") or "", cwd)], None
    if tool == "NotebookRead":
        return [_real(ti.get("notebook_path") or "", cwd)], None
    base = _real(ti.get("path") or cwd or "/", cwd)
    if tool != "Glob":
        return [base], None
    pat = str(ti.get("pattern") or "")
    parts = pat.split("/")
    fixed = []
    for k, seg in enumerate(parts):
        if set(seg) & GLOB_CHARS:
            if any(p == ".." for p in parts[k:]):
                return [], f"glob pattern climbs out after a wildcard: {pat}"
            break
        fixed.append(seg)
    head = "/".join(fixed)
    root = _real(head, base) if head else base
    return [root], None


def check(d, spec):
    """None to let the call through, else the reason to deny it."""
    try:
        tool = d.get("tool_name")
        if tool not in READ_TOOLS:
            return None
        allow = [os.path.realpath(a) for a in (spec or {}).get("allow") or []]
        tp, sid = d.get("transcript_path"), d.get("session_id")
        if tp and sid:
            allow.append(os.path.realpath(os.path.join(os.path.dirname(tp), sid, "tool-results")))
        paths, problem = targets(tool, d.get("tool_input"), d.get("cwd"))
        if problem:
            return problem
        for p in paths:
            if not any(_inside(p, a) for a in allow):
                return f"outside the working copy: {p}"
        return None
    except Exception as e:                       # fail closed
        return f"read guard error: {type(e).__name__}"


def spec_from_env():
    raw = os.environ.get("SWARMSCOPE_READ_GUARD")
    return json.loads(raw) if raw else None


def deny_output(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"swarmscope lab: {reason}"}}
