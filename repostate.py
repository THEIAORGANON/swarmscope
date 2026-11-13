"""What actually changed on disk during a session, independent of which tool did it.

At the first event of a session we snapshot the whole working tree as a git tree object, and at Stop we
snapshot again and diff the two trees. The snapshot uses a throw-away index (GIT_INDEX_FILE), so the
user's index, working tree and branches are never touched; it only adds objects to .git/objects.

Because both sides are full working-tree snapshots, the diff includes:
  * new untracked files (git add -A respects .gitignore),
  * staged and unstaged edits,
  * edits the agent already committed,
  * edits made by Bash or any other process,
and excludes changes that existed before the session started (they are in the baseline).
Edits that were made and then reverted net out to nothing, which is what a review of the result needs.

Projects that are not git repositories get {"git": False}; rules that need a diff then report
"unknown" instead of guessing from mtimes.
"""
import os
import shutil
import subprocess
import tempfile


def _git(args, cwd, env=None, timeout=15):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout


def snapshot(cwd):
    """{"git": True, "root", "head", "tree"} or {"git": False, "why"}."""
    try:
        root = _git(["rev-parse", "--show-toplevel"], cwd).decode().strip()
    except Exception as e:  # not a repo, git missing, timeout
        return {"git": False, "why": str(e)[:120]}
    tmpdir = tempfile.mkdtemp(prefix="swarmscope-idx-")
    try:
        idx = os.path.join(tmpdir, "index")
        real = _git(["rev-parse", "--git-path", "index"], root).decode().strip()
        real = real if os.path.isabs(real) else os.path.join(root, real)
        if os.path.exists(real):
            shutil.copyfile(real, idx)          # reuse the stat cache: fast on big repos
        env = dict(os.environ, GIT_INDEX_FILE=idx)
        _git(["add", "-A", "--", "."], root, env=env, timeout=30)
        tree = _git(["write-tree"], root, env=env).decode().strip()
        try:
            head = _git(["rev-parse", "HEAD"], root).decode().strip()
        except Exception:
            head = None                         # repo without commits
        return {"git": True, "root": root, "head": head, "tree": tree}
    except Exception as e:
        return {"git": False, "why": str(e)[:120]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def diff(root, base_tree, cur_tree):
    """[{"path", "added", "deleted", "binary"}] between two snapshots."""
    if base_tree == cur_tree:
        return []
    out = _git(["diff", "--numstat", "-z", "--no-renames", base_tree, cur_tree], root).decode(errors="replace")
    files = []
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        binary = a == "-"
        files.append({"path": path, "added": 0 if binary else int(a), "deleted": 0 if binary else int(d),
                      "binary": binary})
    return files
