#!/usr/bin/env python3
"""Calibration task set (4b): unpack task sources, build each task's git repo with one deterministic commit,
check the commits against the manifests in lab/tasks/, and validate every task with the local python3.

  python lab/tasks/build_tasks.py [--archive tasks-src.tar.gz] [--no-check] [--force]

Runner side only (the agent can read none of it): $SWARMSCOPE_LAB or ~/Downloads/swarmscope-lab
  tasks-src/<id>/task.json   prompt, summary, traps, mutants (plausible wrong solutions)
  tasks-src/<id>/base/       the task's starting state (becomes the repo)
  tasks-src/<id>/hidden/     hidden acceptance tests (test_hidden.py)
  tasks-src/<id>/ref/        reference solution, laid over base
  tasks/<id>/                git repo built from base/ (source.repo in lab/tasks/<id>.json)

Validation, per task: base fails the public and the hidden tests; base+ref passes both with exactly
expected_tests hidden tests; every mutant passes the public tests and fails at least one hidden test.
Hidden tests must not use subTest (unittest would count failing subtests as extra failures).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import lab  # noqa: E402

COMMIT_MESSAGE = "base"
SKIP = {"__pycache__", ".DS_Store"}


def git(cwd, *args):
    env = dict(os.environ, **lab.GIT_ENV)
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def files(root):
    return sorted(p for p in Path(root).rglob("*") if p.is_file() and not SKIP.intersection(p.parts))


def build_repo(base, dest):
    """dest: a new git repo holding base/ in one commit. Files are copied without their modes (all 0644) and
    the commit uses fixed names and dates, so the same base/ always gives the same commit id."""
    dest.mkdir(parents=True)
    for p in files(base):
        t = dest / p.relative_to(base)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, t)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "commit.gpgsign", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "--no-verify", "-m", COMMIT_MESSAGE)
    return git(dest, "rev-parse", "HEAD")


def unpack(archive, dest):
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts:
                raise RuntimeError(f"refusing archive member {m.name!r}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)


# --- validation ---------------------------------------------------------------------------------------------

def _overlay(src, dst):
    for p in files(src):
        t = dst / p.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, t)


def _variant(task_dir, ref, edits):
    d = Path(tempfile.mkdtemp(prefix=f"task-{task_dir.name}-"))
    _overlay(task_dir / "base", d)
    if ref:
        _overlay(task_dir / "ref", d)
    for e in edits or []:
        f = d / e["file"]
        s = f.read_text()
        if s.count(e["old"]) != 1:
            raise RuntimeError(f"{task_dir.name}: mutant edit matches {s.count(e['old'])} times in {e['file']}")
        f.write_text(s.replace(e["old"], e["new"]))
    shutil.copytree(task_dir / "hidden", d / ".hidden_tests")
    return d


def _run(cmd, cwd):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR", "TZ") or k.startswith("LC_")}
    env.update(HOME=str(cwd), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    r = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    text = r.stdout + r.stderr
    ran, failed = lab.parse_unittest(text)
    cases = lab.parse_cases(text)
    return {"code": r.returncode, "ran": ran, "failed": failed, "not_passed": sorted(k for k, v in cases.items()
                                                                                    if v != "ok"), "text": text}


def validate(task_dir, manifest):
    meta = json.loads((task_dir / "task.json").read_text())
    if "subTest" in (task_dir / "hidden" / "test_hidden.py").read_text():
        return [f"{task_dir.name}: hidden tests use subTest"]
    public = manifest["public_tests"]
    hidden = manifest["hidden_tests"]["cmd"].replace("{hidden}", ".hidden_tests")
    expected = manifest["hidden_tests"]["expected_tests"]
    problems, rows = [], []
    variants = [("base", False, None), ("ref", True, None)] + [(m["name"], True, m["edits"]) for m in meta["mutants"]]
    for name, ref, edits in variants:
        d = _variant(task_dir, ref, edits)
        try:
            p, h = _run(public, d), _run(hidden, d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if name == "base":
            good = p["code"] != 0 and h["code"] != 0
        elif name == "ref":
            good = p["code"] == 0 and h["code"] == 0 and h["ran"] == expected and h["failed"] == 0
        else:
            good = p["code"] == 0 and h["code"] != 0
        rows.append(f"    {'ok ' if good else 'BAD'} {name:<26} public {p['ran']}/{p['failed']} failed  hidden "
                    f"{h['ran']}/{h['failed']} failed  {', '.join(h['not_passed'])[:90]}")
        if not good:
            problems.append(f"{task_dir.name}/{name}: public exit {p['code']}, hidden exit {h['code']}\n"
                            + h["text"][-1200:])
    print("\n".join(rows))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--archive", help="tasks-src archive to unpack into the lab first")
    ap.add_argument("--no-check", action="store_true", help="build only, skip validation")
    ap.add_argument("--force", action="store_true", help="rebuild repos and replace tasks-src from the archive")
    ap.add_argument("--only", help="comma-separated task ids")
    a = ap.parse_args(argv)
    root = lab.lab_root()
    src_root, repos = root / "tasks-src", root / "tasks"
    if a.archive:
        if src_root.exists():
            if not a.force:
                print(f"{src_root} exists; use --force to replace it")
                return 1
            shutil.rmtree(src_root)
        tmp = Path(tempfile.mkdtemp(dir=root))
        unpack(a.archive, tmp)
        (tmp / "tasks-src").rename(src_root)
        shutil.rmtree(tmp)
    ids = sorted(p.parent.name for p in src_root.glob("*/task.json"))
    if a.only:
        ids = [i for i in ids if i in a.only.split(",")]
    bad = []
    print(f"python3 for the tests: {shutil.which('python3')}")
    for tid in ids:
        mpath = HERE / f"{tid}.json"
        if not mpath.exists():
            bad.append(f"{tid}: no manifest lab/tasks/{tid}.json")
            continue
        manifest = json.loads(mpath.read_text())
        dest = repos / tid
        if dest.exists() and a.force:
            shutil.rmtree(dest)
        head = git(dest, "rev-parse", "HEAD") if dest.exists() else build_repo(src_root / tid / "base", dest)
        same = head == manifest["source"]["commit_full"]
        print(f"{tid:<11} {head[:12]}  {'matches the manifest' if same else 'DOES NOT match the manifest'}")
        if not same:
            bad.append(f"{tid}: commit {head} != manifest {manifest['source']['commit_full']}")
        if not a.no_check:
            bad.extend(validate(src_root / tid, manifest))
    print("\n".join(["", "PROBLEMS:"] + bad) if bad else f"\nall {len(ids)} tasks OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
