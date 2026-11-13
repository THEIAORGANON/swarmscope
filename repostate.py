"""What actually changed on disk during a session, independent of which tool did it.

The working tree is snapshotted as a git tree object:
  * at the session's first event (baseline),
  * after every tool call that can change files (Edit/Write/Bash/…), giving a change timeline,
  * at Stop.
Diffing two snapshots shows new untracked files, staged and unstaged edits, edits the agent committed,
and edits made by Bash or any other process. Changes that existed before the session are in the
baseline, so they are not attributed to the agent. Edits that were made and then reverted net out.

Nothing is written into the user's repository:
  * a throw-away index (GIT_INDEX_FILE) leaves the staging area alone,
  * objects go to Swarmscope's own store (GIT_OBJECT_DIRECTORY); the repo's objects are only read
    through GIT_ALTERNATE_OBJECT_DIRECTORIES.
Files that look sensitive (.env, keys, credentials…) or are larger than MAX_FILE_BYTES are never read,
tracked or untracked: their snapshot entry keeps whatever the user's own index already had, and they are
listed in "skipped". Swarmscope's own folders are excluded. prune() removes object stores of old sessions.

Projects that are not git repositories get {"git": False}; rules then report "unknown" instead of
guessing from mtimes.
"""
import fnmatch
import os
import shutil
import subprocess
import tempfile

MAX_FILE_BYTES = 1_000_000
SENSITIVE = [".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
             "id_rsa*", "id_ed25519*", "id_ecdsa*", "*secret*", "*credential*", ".netrc", ".npmrc", ".pypirc",
             "*.sqlite", "*.sqlite3", "*.db"]
ALWAYS_EXCLUDE = [".swarmscope", ".claude"]


def _git(args, cwd, env=None, timeout=15, stdin=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=timeout, input=stdin)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout


def _env(root, store, index=None):
    objects = _git(["rev-parse", "--git-path", "objects"], root).decode().strip()
    objects = objects if os.path.isabs(objects) else os.path.join(root, objects)
    os.makedirs(store, exist_ok=True)
    env = dict(os.environ, GIT_OBJECT_DIRECTORY=os.path.abspath(store),
               GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.abspath(objects))
    if index:
        env["GIT_INDEX_FILE"] = index
    return env


def _sensitive(path):
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE)


def snapshot(cwd, store, exclude=()):
    """{"git": True, "root", "tree", "skipped": [paths]} or {"git": False, "why"}.
    store: directory for Swarmscope's own git objects. exclude: extra paths relative to the root."""
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
        env = _env(root, store, idx)
        skip_paths = list(ALWAYS_EXCLUDE) + [e for e in exclude if e]
        _git(["rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *skip_paths], root, env=env)

        def ours(p):
            return p and not any(p == x or p.startswith(x.rstrip("/") + "/") for x in skip_paths)

        # Every file is filtered BEFORE git reads it: modified tracked files (-m), deleted ones (-d) and new
        # untracked ones (-o). No `git add -u`: it would read tracked .env/keys/big files unfiltered.
        modified = _git(["ls-files", "-m", "-z"], root, env=env).decode(errors="replace").split("\0")
        deleted = set(_git(["ls-files", "-d", "-z"], root, env=env).decode(errors="replace").split("\0"))
        untracked = _git(["ls-files", "-o", "--exclude-standard", "-z"], root, env=env).decode(errors="replace")
        add, rm, skipped = [], [], []
        for p in [x for x in modified if ours(x)] + [x for x in untracked.split("\0") if ours(x)]:
            if p in deleted:
                rm.append(p)
                continue
            try:
                big = os.path.getsize(os.path.join(root, p)) > MAX_FILE_BYTES
            except OSError:
                continue
            (skipped if big or _sensitive(p) else add).append(p)
        rm += [p for p in deleted if ours(p) and p not in rm]
        if rm:
            _git(["rm", "--cached", "-q", "--pathspec-from-file=-", "--pathspec-file-nul"], root, env=env,
                 stdin="\0".join(rm).encode())
        if add:
            _git(["add", "--pathspec-from-file=-", "--pathspec-file-nul"], root, env=env, timeout=30,
                 stdin="\0".join(add).encode())
        tree = _git(["write-tree"], root, env=env).decode().strip()
        return {"git": True, "root": root, "tree": tree, "skipped": skipped[:50]}
    except Exception as e:
        return {"git": False, "why": str(e)[:120]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def prune(stores_dir, keep_days=7, keep=()):
    """Delete per-session object stores not touched for keep_days (they only matter while a session runs
    and for later analysis of it)."""
    import time
    cutoff = time.time() - keep_days * 86400
    try:
        names = os.listdir(stores_dir)
    except OSError:
        return 0
    n = 0
    for name in names:
        path = os.path.join(stores_dir, name)
        if name in keep or not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def diff(root, store, base_tree, cur_tree):
    """[{"path", "added", "deleted", "binary"}] between two snapshots."""
    if base_tree == cur_tree:
        return []
    out = _git(["diff", "--numstat", "-z", "--no-renames", base_tree, cur_tree], root,
               env=_env(root, store)).decode(errors="replace")
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
