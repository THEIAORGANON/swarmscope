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

The repository's own configuration is never read: the hooks run with the user's rights and outside any
agent sandbox, so a repo-local core.fsmonitor, filter driver, textconv or hook set by the agent must not run
here. Every git call uses a throw-away GIT_DIR (only HEAD/refs, no config) with GIT_WORK_TREE = the project,
the project's index copied in, its objects as read-only alternates, and no global/system config.
The repository root and its git dir are found by walking up for ".git" in Python, not by asking git.
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


# belt and braces on top of the empty GIT_DIR: nothing that can execute a program
SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.untrackedCache=false",
        "-c", "core.splitIndex=false", "-c", "diff.external=", "-c", "core.attributesFile=" + os.devnull]
_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_EXTERNAL_DIFF",
             "GIT_CEILING_DIRECTORIES")


def _git(args, cwd, env=None, timeout=15, stdin=None):
    r = subprocess.run(["git", *SAFE, *args], cwd=cwd, env=env, capture_output=True, timeout=timeout, input=stdin)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return r.stdout


def find_repo(cwd):
    """(root, git_dir) by walking up for .git (a directory, or a "gitdir: <path>" file). No git call."""
    d = os.path.realpath(cwd or ".")
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return d, g
        if os.path.isfile(g):
            try:
                line = open(g).read().strip()
            except OSError:
                return None
            if line.startswith("gitdir:"):
                gd = line[7:].strip()
                return d, os.path.realpath(gd if os.path.isabs(gd) else os.path.join(d, gd))
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _objects_dir(git_dir):
    common = os.path.join(git_dir, "commondir")          # linked worktrees keep objects in the main git dir
    if os.path.isfile(common):
        c = open(common).read().strip()
        git_dir = os.path.realpath(c if os.path.isabs(c) else os.path.join(git_dir, c))
    return os.path.join(git_dir, "objects")


def _fake_git_dir(tmpdir):
    g = os.path.join(tmpdir, "gitdir")
    if not os.path.isdir(g):
        os.makedirs(os.path.join(g, "refs", "heads"))
        os.makedirs(os.path.join(g, "objects"))
        with open(os.path.join(g, "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/swarmscope\n")
    return g


def _env(root, git_dir, store, tmpdir, index=None):
    os.makedirs(store, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _GIT_VARS}
    env.update(GIT_DIR=_fake_git_dir(tmpdir), GIT_WORK_TREE=root, GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1", GIT_OBJECT_DIRECTORY=os.path.abspath(store),
               GIT_ALTERNATE_OBJECT_DIRECTORIES=os.path.abspath(_objects_dir(git_dir)), GIT_TERMINAL_PROMPT="0")
    if index:
        env["GIT_INDEX_FILE"] = index
    return env


def _sensitive(path):
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE)


def snapshot(cwd, store, exclude=()):
    """{"git": True, "root", "tree", "skipped": [paths]} or {"git": False, "why"}.
    store: directory for Swarmscope's own git objects. exclude: extra paths relative to the root."""
    found = find_repo(cwd)
    if not found:
        return {"git": False, "why": "not a git repository"}
    root, git_dir = found
    tmpdir = tempfile.mkdtemp(prefix="swarmscope-idx-")
    try:
        idx = os.path.join(tmpdir, "index")
        real = os.path.join(git_dir, "index")
        if os.path.exists(real):
            shutil.copyfile(real, idx)          # reuse the stat cache: fast on big repos
        env = _env(root, git_dir, store, tmpdir, idx)
        skip_paths = list(ALWAYS_EXCLUDE) + [e for e in exclude if e]
        _git(["rm", "--cached", "-r", "-q", "-f", "--ignore-unmatch", "--", *skip_paths], root, env=env)

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
            _git(["rm", "--cached", "-q", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"], root, env=env,
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
    found = find_repo(root)
    if not found:
        raise RuntimeError("not a git repository")
    tmpdir = tempfile.mkdtemp(prefix="swarmscope-diff-")
    try:
        out = _git(["diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base_tree, cur_tree],
                   found[0], env=_env(found[0], found[1], store, tmpdir)).decode(errors="replace")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
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
