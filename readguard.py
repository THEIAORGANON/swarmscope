"""Allow-list for the tools that read files (lab runs only).

Run as its own PreToolUse command hook for the reading tools, with onFailure "block": SWARMSCOPE_READ_GUARD
(JSON: {"allow": [dirs], "task_output_root": dir}) names the allowed dirs, and a call is denied unless every path it can reach lies in an
allowed dir or in the session's own spilled tool outputs (<project folder>/<session id>/tool-results, the project
folder being the transcript's), or it is a Read of one of the session's own background task outputs
(<task_output_root>/<project>/<session id>/tasks/<id>.output, own_task_output()). The hook (check) and the audit
after the session (lab.read_outside) decide through the same function, decide().
The settings' Read denies cannot name what appears only after they were written; this guard decides on the
path as it is when the tool runs. Paths are resolved through symlinks.
Failure: nothing is swallowed in main(): an error before the decision (import, bad spec, bad input) exits 1 and
Claude Code blocks the call (onFailure "block"), as it does for a timeout or a failed start. Inside check() an
error is a deny."""
import json
import os
import re
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


SAFE_NAME = re.compile(r"[A-Za-z0-9-]+")     # a project folder name ("-private-tmp-..."), a session id: no separator, no dot


def task_dir(root, transcript_path, session_id):
    """This session's own background task folder, <root>/<project>/<session id>/tasks (Claude Code >= 2.1.326:
    "Output is being written to: /tmp/claude-<uid>/<project>/<session>/tasks/<id>.output"), resolved; None when
    any part is missing or not a plain name. The root comes from the runner (spec "task_output_root": Claude
    Code's per-uid temp folder for the agent's environment), the project folder is the transcript's, the session
    is the hook's: nothing is taken from the requested path itself."""
    if not (root and transcript_path and session_id):
        return None
    project = os.path.basename(os.path.dirname(transcript_path))
    if not (SAFE_NAME.fullmatch(project) and isinstance(session_id, str) and SAFE_NAME.fullmatch(session_id)):
        return None
    return os.path.realpath(os.path.join(root, project, session_id, "tasks"))


def own_task_output(tool, path, tdir, live=True):
    """A Read of one of this session's own background task outputs: the resolved path (symlinks followed, so a
    link to anything outside fails) names a file *.output directly in tdir. live (the hook): it must exist as a
    regular file now. The audit after the session (live=False) cannot see links or files Claude Code has since
    removed; it checks the same place and name, the hook having checked the file when it was read."""
    if tool != "Read" or not tdir:
        return False
    name = os.path.basename(path)
    if os.path.dirname(path) != tdir or not name.endswith(".output") or name == ".output":
        return False
    return os.path.isfile(path) if live else not os.path.isdir(path)


def decide(tool, tool_input, cwd, allow, transcript_path=None, session_id=None, task_output_root=None, live=True):
    """None to let the call through, else the reason to deny it: every path the call can reach must lie in an
    allowed dir or in this session's own tool-results, or the call is a Read of an own task output."""
    if tool not in READ_TOOLS:
        return None
    allow = [os.path.realpath(a) for a in allow or []]
    if transcript_path and session_id:
        allow.append(os.path.realpath(os.path.join(os.path.dirname(transcript_path), session_id, "tool-results")))
    paths, problem = targets(tool, tool_input, cwd)
    if problem:
        return problem
    tdir = task_dir(task_output_root, transcript_path, session_id)
    for p in paths:
        if any(_inside(p, a) for a in allow) or own_task_output(tool, p, tdir, live):
            continue
        return f"outside the working copy: {p}"
    return None


def check(d, spec):
    """The hook's decision: None to let the call through, else the reason to deny it."""
    try:
        spec = spec or {}
        return decide(d.get("tool_name"), d.get("tool_input"), d.get("cwd"), spec.get("allow") or [],
                      d.get("transcript_path"), d.get("session_id"), spec.get("task_output_root"))
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
