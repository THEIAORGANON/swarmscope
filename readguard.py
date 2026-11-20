"""Allow-list for the tools that read files (lab runs only).

Run as its own PreToolUse command hook for the reading tools, with onFailure "block": SWARMSCOPE_READ_GUARD
(JSON: {"allow": [dirs]}) names the allowed dirs, and a call is denied unless every path it can reach lies in an allowed dir or in the session's own
spilled tool outputs (<project folder>/<session id>/tool-results, the project folder being the transcript's).
The settings' Read denies cannot name what appears only after they were written; this guard decides on the
path as it is when the tool runs. Paths are resolved through symlinks.
Failure: nothing is swallowed in main(): an error before the decision (import, bad spec, bad input) exits 1 and
Claude Code blocks the call (onFailure "block"), as it does for a timeout or a failed start. Inside check() an
error is a deny."""
import json
import os
import sys
import unicodedata

READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
GLOB_CHARS = set("*?[{")


def _odd(path):
    """Why a path string cannot be resolved the way Claude Code will resolve it, or None. Claude Code expands a
    leading '~' and trims surrounding whitespace before it reads (Grep/Glob path, 2.1.326: PR #8 review), so such
    a string named one place to this guard and another to the tool. Refused rather than imitated: any edge
    whitespace or invisible character, a control or format character anywhere, a leading '~' and a non-string."""
    if not isinstance(path, str):
        return f"path is not a string: {type(path).__name__}"
    if path.startswith("~"):
        return f"path starts with ~: {path!r}"
    if path and (path[0].isspace() or path[-1].isspace() or not path[0].isprintable() or not path[-1].isprintable()
                 or unicodedata.category(path[0]) in ("Cf", "Zs") or unicodedata.category(path[-1]) in ("Cf", "Zs")):
        return f"path has whitespace or an invisible character at an edge: {path!r}"
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in path):
        return f"path has a control or format character: {path!r}"
    return None


def _real(path, cwd):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or "/", path))


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def targets(tool, ti, cwd):
    """Every path the call can read: (paths, problem). A Glob pattern can leave its path: its fixed leading part
    joins the path, and a '..' after a wildcard is refused (its reach cannot be bounded)."""
    ti = ti if isinstance(ti, dict) else {}
    key = {"Read": "file_path", "NotebookRead": "notebook_path"}.get(tool, "path")
    raw = ti.get(key)
    if raw is not None:
        problem = _odd(raw)
        if problem:
            return [], problem
    if tool in ("Read", "NotebookRead"):
        return [_real(raw or "", cwd)], None
    base = _real(raw or cwd or "/", cwd)
    if tool != "Glob":
        return [base], None
    pat = ti.get("pattern")
    problem = _odd(pat) if pat is not None else None
    if problem:
        return [], "glob " + problem
    pat = pat or ""
    parts = pat.split("/")
    fixed = []
    for k, seg in enumerate(parts):
        if set(seg) & GLOB_CHARS:
            if any(p == ".." for p in parts[k:]):
                return [], f"glob pattern climbs out after a wildcard: {pat}"
            break
        fixed.append(seg)
    head = "/".join(fixed)
    if pat.startswith("/"):
        head = head or "/"                     # "/[U]sers/..." or "/*/...": the fixed part is the root itself
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


def main():
    spec = json.loads(os.environ["SWARMSCOPE_READ_GUARD"])       # missing or broken: an error, so a block
    d = json.load(sys.stdin)
    reason = check(d, spec)
    if reason:
        sys.stdout.write(json.dumps(deny_output(reason)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
