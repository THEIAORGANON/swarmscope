#!/usr/bin/env python3
"""Swarmscope lab: minimal experiment runner (modes A and B on one task).

One attempt = a fresh copy of the task's base state, a fresh Claude Code session, the same observation
infrastructure in every mode, hidden tests after the session, and one run.json with everything measured.

  python lab/lab.py setup                      create the lab folder and its isolated Claude Code config
  python lab/lab.py doctor [--ping]            check the CLI, flags, isolation; --ping makes one tiny paid call
  python lab/lab.py run   --task T --mode A    one attempt
  python lab/lab.py batch --task T --modes A,B --repeats 1 --seed 1    shuffled order, one attempt each
  python lab/lab.py summary                    table of all runs

Lab folder: $SWARMSCOPE_LAB or ~/Downloads/swarmscope-lab
  config/        CLAUDE_CONFIG_DIR for every run (log in once; no CLAUDE.md, no hooks, no MCP)
  w/<run_id>/    the agent's working copy (run ids are random and never encode the mode)
  runs/<run_id>/ everything else: policy.json, settings.json, swarmscope logs, stream, run.json

What is NOT isolated (known limitation until runs move into a container): the agent has Bash, so it can
read files outside its working copy, including runs/, the hook command line and the hidden tests' folder.
"""
import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import io
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAB_DIR = Path(__file__).resolve().parent
RUNNER_VERSION = "lab-1"
CODE_FILES = ["hook.py", "common.py", "rules.py", "repostate.py", "jevshadow.py", "costs.py"]
HOOK_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart",
               "SubagentStop", "Stop"]
WITH_MATCHER = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
# never handed to the agent's process: experiment config, keys, and anything that would change the model
DROP_ENV = re.compile(r"^(SWARMSCOPE_|TYPESAFE_|CLAUDECODE$|CLAUDE_CODE_ENTRYPOINT$|CLAUDE_CODE_EFFORT_LEVEL$|"
                      r"ANTHROPIC_MODEL$|ANTHROPIC_DEFAULT_|ANTHROPIC_SMALL_FAST_MODEL$|CLAUDE_CONFIG_DIR$)")
# marketplace catalogs Claude Code downloads on first start: metadata, not installed plugins
MARKETPLACE_CATALOG = ("known_marketplaces.json:", "marketplaces/")
REQUIRED_FLAGS = ["--disable-slash-commands", "--effort", "--settings", "--setting-sources", "--strict-mcp-config",
                  "--output-format", "--permission-mode"]
GIT_ENV = {"GIT_AUTHOR_NAME": "lab", "GIT_AUTHOR_EMAIL": "lab@localhost", "GIT_COMMITTER_NAME": "lab",
           "GIT_COMMITTER_EMAIL": "lab@localhost", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
           # the runner's own git calls ignore the user's global/system config (signing, hooks, pagers)
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def lab_root():
    return Path(os.environ.get("SWARMSCOPE_LAB") or Path.home() / "Downloads" / "swarmscope-lab").expanduser()


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def file_sha(p):
    try:
        return sha256(Path(p).read_bytes())
    except OSError:
        return None


def jl(path):
    out, bad = [], 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return None, 0
    return out, bad


def sh(cmd, cwd=None, env=None, timeout=60, input=None):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=input,
                           shell=isinstance(cmd, str))
        return {"code": p.returncode, "out": p.stdout, "err": p.stderr, "s": round(time.time() - t0, 2)}
    except subprocess.TimeoutExpired as e:
        return {"code": None, "out": e.stdout or "", "err": "timeout", "s": round(time.time() - t0, 2)}
    except OSError as e:
        return {"code": None, "out": "", "err": str(e), "s": round(time.time() - t0, 2)}


def claude_bin():
    return os.environ.get("SWARMSCOPE_CLAUDE_BIN") or shutil.which("claude") or "claude"


def load_modes():
    raw = (LAB_DIR / "modes.json").read_bytes()
    return json.loads(raw), sha256(raw)


def load_task(path):
    p = Path(path)
    if not p.exists():
        p = LAB_DIR / "tasks" / f"{path}.json"
    raw = p.read_bytes()
    t = json.loads(raw)
    t["_file"], t["_sha256"] = str(p.resolve()), sha256(raw)
    return t


def expand(s):
    return str(Path(os.path.expanduser(s)).resolve())


# --- task base state --------------------------------------------------------------------------------

def excluded(name, patterns):
    name = name.rstrip("/")
    return any(name == x.rstrip("/") or name.startswith(x.rstrip("/") + "/") for x in patterns)


def export_base(task, work):
    """Fresh repo with one deterministic commit holding the task's base tree minus excluded paths.
    No remote, no history: the agent cannot reach the source repo through git."""
    src = expand(task["source"]["repo"])
    genv = dict(os.environ, **GIT_ENV)
    full = sh(["git", "-C", src, "rev-parse", task["source"]["commit"] + "^{commit}"], env=genv)
    if full["code"] != 0:
        raise SystemExit(f"base commit not found in {src}: {full['err'].strip()}")
    commit = full["out"].strip()
    if task["source"].get("commit_full") and task["source"]["commit_full"] != commit:
        raise SystemExit("task manifest commit_full does not match the source repo")
    tar = subprocess.run(["git", "-C", src, "archive", "--format=tar", commit], capture_output=True, check=True,
                         env=genv).stdout
    work.mkdir(parents=True)
    dropped = []
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        members = []
        for m in tf.getmembers():
            if excluded(m.name, task["source"].get("exclude", [])):
                dropped.append(m.name)
            elif m.isfile() or m.isdir():
                members.append(m)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(work, members=members, filter="data")
        else:
            tf.extractall(work, members=members)
    env = dict(os.environ, **GIT_ENV)
    # commit.gpgsign=false is also written into the working copy: a signing prompt would hang the agent's own
    # commits the same way in every mode
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "commit.gpgsign", "false"],
                ["git", "config", "tag.gpgsign", "false"], ["git", "add", "-A"],
                ["git", "commit", "-q", "--no-verify", "-m", f"{task['task_id']}: base"]):
        r = sh(cmd, cwd=work, env=env)
        if r["code"] != 0:
            raise SystemExit(f"{' '.join(cmd)} failed: {r['err']}")
    head = sh(["git", "rev-parse", "HEAD"], cwd=work)["out"].strip()
    tree = sh(["git", "rev-parse", "HEAD^{tree}"], cwd=work)["out"].strip()
    return {"source_repo": src, "source_commit": commit, "excluded": sorted(dropped), "work_commit": head,
            "work_tree": tree}


# --- isolation checks -------------------------------------------------------------------------------

def instruction_files_above(work):
    """CLAUDE.md files Claude Code may load from the working copy and every folder above it."""
    found = []
    d = Path(work).resolve()
    for p in [d] + list(d.parents):
        names = ["CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"]
        if p == d:          # project settings only come from the project itself (and --setting-sources user skips them)
            names += [".claude/settings.json", ".claude/settings.local.json"]
        for name in names:
            f = p / name
            if f.exists():
                found.append(str(f))
    return found


def config_inventory(cfg):
    inv = {"dir": str(cfg), "exists": cfg.is_dir()}
    if not cfg.is_dir():
        return inv
    s = cfg / "settings.json"
    try:
        inv["settings"] = json.loads(s.read_text()) if s.exists() else None
    except ValueError:
        inv["settings"] = "invalid json"
    inv["settings_sha256"] = file_sha(s)
    for sub in ("agents", "commands", "skills", "plugins", "output-styles"):
        d = cfg / sub
        inv[sub] = sorted(f"{p.relative_to(d)}:{file_sha(p)[:12]}" for p in d.rglob("*") if p.is_file()) \
            if d.is_dir() else []
    inv["claude_md"] = [str(p) for p in (cfg / "CLAUDE.md", cfg / "CLAUDE.local.md") if p.exists()]
    return inv


def preflight(work, cfg):
    problems = []
    inv = config_inventory(cfg)
    if not inv["exists"]:
        problems.append(f"config dir missing: {cfg} (run: lab.py setup)")
        return {"ok": False, "problems": problems, "config": inv}
    if inv["claude_md"]:
        problems.append(f"instructions in config dir: {inv['claude_md']}")
    st = inv.get("settings")
    if st == "invalid json":
        problems.append("config settings.json is not valid JSON")
    elif isinstance(st, dict):
        for k in ("hooks", "statusLine", "env", "outputStyle", "model", "advisorModel", "effortLevel",
                  "enabledPlugins"):
            if k in st:
                problems.append(f"config settings.json sets '{k}': remove it (the runner sets everything)")
    for sub in ("commands", "output-styles"):
        if inv[sub]:
            problems.append(f"config dir has {sub}: {inv[sub][:3]}")
    # skills and plugins: Claude Code syncs account skills and a plugin store into the config dir by itself.
    # Runs pass --disable-slash-commands; what counts is what the session reports as loaded (init message),
    # checked after launch. The files are recorded in the inventory.
    above = instruction_files_above(work)
    if above:
        problems.append(f"instruction/settings files in or above the working copy: {above}")
    for name in ("CLAUDE.md", ".claude", ".swarmscope", "PROMPT.txt"):
        if (Path(work) / name).exists():
            problems.append(f"working copy contains {name}")
    proj = cfg / "projects"
    if proj.is_dir():
        enc = re.sub(r"[^A-Za-z0-9]", "-", str(Path(work).resolve()))
        if (proj / enc).exists():
            problems.append(f"config already has a project folder for this working copy: {proj / enc}")
    return {"ok": not problems, "problems": problems, "config": inv, "instruction_files_above": above}


def not_clean(init):
    """What the session loaded beyond Claude Code itself: MCP servers, skills, non-builtin plugins.
    Builtin plugins (path "builtin", source "...@builtin") ship with Claude Code and are the same everywhere."""
    out = {}
    for k in ("mcp_servers", "skills"):
        if init.get(k):
            out[k] = init[k]
    foreign = [p for p in init.get("plugins") or []
               if not (isinstance(p, dict) and (p.get("path") == "builtin" or str(p.get("source", "")).endswith("@builtin")))]
    if foreign:
        out["plugins"] = foreign
    return out


def agent_env(cfg):
    env, dropped = {}, []
    for k, v in os.environ.items():
        if DROP_ENV.match(k):
            dropped.append(k)
        else:
            env[k] = v
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    env["DISABLE_AUTOUPDATER"] = "1"
    return env, sorted(dropped)


# --- one attempt ------------------------------------------------------------------------------------

def write_settings(run_dir, sw, policy_file):
    cmd = " ".join([f"SWARMSCOPE_DIR={shlex.quote(str(sw))}", f"SWARMSCOPE_POLICY_FILE={shlex.quote(str(policy_file))}",
                    shlex.quote(sys.executable), shlex.quote(str(REPO / "hook.py"))])
    hooks = {}
    for ev in HOOK_EVENTS:
        h = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if ev in WITH_MATCHER:
            h["matcher"] = "*"
        hooks[ev] = [h]
    p = run_dir / "settings.json"
    p.write_text(json.dumps({"hooks": hooks}, indent=2))
    return p


def build_policy(modes, mode):
    common, m = modes["common"], modes["modes"][mode]
    pol = {"mode": m["policy_mode"], "jev": dict(common["jev"])}
    pol.update(m.get("policy", {}))
    return pol


def claude_cmd(modes, settings):
    c = modes["common"]
    cmd = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose", "--model", c["model"],
           "--advisor", c["advisor"], "--effort", c["effort"], "--settings", str(settings),
           "--setting-sources", "user", "--strict-mcp-config", "--disable-slash-commands",
           "--permission-mode", c["permission_mode"]]
    return cmd + list(c.get("extra_args", []))


def run_session(cmd, work, env, prompt, stream_path, err_path, timeout_s):
    t0 = time.time()
    with open(stream_path, "w") as out, open(err_path, "w") as err:
        p = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                             start_new_session=True, text=True)
        p.stdin.write(prompt)
        p.stdin.close()
        timed_out = False
        try:
            code = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGTERM)
                code = p.wait(timeout=15)
            except Exception:
                os.killpg(p.pid, signal.SIGKILL)
                code = p.wait()
    return {"exit_code": code, "timed_out": timed_out, "wall_s": round(time.time() - t0, 2)}


def parse_stream(path):
    recs, bad = jl(path)
    recs = recs or []
    init = next((r for r in recs if r.get("type") == "system" and r.get("subtype") == "init"), None)
    result = next((r for r in reversed(recs) if r.get("type") == "result"), None)
    return init, result, bad


def advisor_calls(transcript):
    """Every advisor call in the transcript and what came back: returned | failed | no_result."""
    calls, results = {}, {}
    recs, _ = jl(transcript)
    seen_msgs = set()
    for r in recs or []:
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") or {}
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "server_tool_use" and b.get("name") == "advisor" and b.get("id") not in calls:
                calls[b["id"]] = {"id": b["id"], "ts": r.get("timestamp"), "message_id": msg.get("id")}
            elif b.get("type") == "advisor_tool_result" and b.get("tool_use_id") not in results:
                c = b.get("content")
                ctype = c.get("type") if isinstance(c, dict) else type(c).__name__
                rec = {"content_type": ctype}
                if isinstance(c, dict) and "error" in str(ctype):
                    rec.update(status="failed", error_code=c.get("error_code"))
                else:
                    rec["status"] = "returned"
                results[b["tool_use_id"]] = rec
        seen_msgs.add(msg.get("id"))
    out = []
    for cid, c in calls.items():
        c.update(results.get(cid) or {"status": "no_result"})
        out.append(c)
    return out


def epoch(ts):
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def wait_jev(jev_path, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        recs, _ = jl(jev_path)
        if recs is None:
            return []
        answered = {r["ctx_id"] for r in recs if r.get("kind") == "jev" and not r.get("replay")}
        pend = [r["ctx_id"] for r in recs if r.get("kind") == "context" and r["ctx_id"] not in answered]
        if not pend or time.time() > deadline:
            return pend
        time.sleep(1)


def run_tests(cmd, cwd, timeout=300):
    r = sh(cmd, cwd=cwd, timeout=timeout)
    text = (r["out"] or "") + (r["err"] or "")
    ran = re.findall(r"Ran (\d+) tests?", text)
    total = int(ran[-1]) if ran else None
    fails = sum(int(n) for k, n in re.findall(r"(failures|errors)=(\d+)", text.split("Ran ")[-1])) if ran else None
    passed = (total - fails) if total is not None and fails is not None else None
    return {"cmd": cmd, "exit_code": r["code"], "ok": r["code"] == 0, "tests": total, "passed": passed,
            "seconds": r["s"], "tail": text[-1500:]}


def final_diff(work, base_commit, run_dir):
    sys.path.insert(0, str(REPO))
    import rules
    env = dict(os.environ, **GIT_ENV)
    sh(["git", "add", "-A"], cwd=work, env=env)
    num = sh(["git", "-c", "core.quotepath=off", "diff", "--cached", "--numstat", base_commit], cwd=work, env=env)["out"]
    patch = sh(["git", "-c", "core.quotepath=off", "diff", "--cached", "--binary", base_commit], cwd=work, env=env)["out"]
    (run_dir / "final.diff").write_text(patch)
    files, totals = [], {"production": 0, "test": 0, "ignored": 0}
    for line in num.splitlines():
        a, d, path = line.split("\t", 2)
        lines = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        cls = rules.classify(path, rules.DEFAULTS)
        key = {"prod": "production", "test": "test"}.get(cls, "ignored")
        totals[key] += lines
        files.append({"path": path, "added": a, "deleted": d, "class": cls})
    return {"files": files, "lines": totals, "patch_sha256": sha256(patch), "patch": "final.diff"}


def summarize_decisions(path, expected_mode):
    recs, bad = jl(path)
    if recs is None:
        return {"present": False}
    dec = [r for r in recs if r.get("kind") == "decision"]
    stops = sorted({r.get("stop_ts") for r in dec})
    modes = sorted({(r.get("gate") or {}).get("mode") for r in dec} - {None})
    by_rule = {}
    for r in dec:
        by_rule.setdefault(r["rule"], {}).setdefault(r["state"], 0)
        by_rule[r["rule"]][r["state"]] += 1
    closing = [{k: r.get(k) for k in ("kind", "id", "rule", "action", "after_state", "outcome", "cause")}
               for r in recs if r.get("kind") in ("outcome", "followup")]
    requested = [r for r in dec if r["state"] == "requested"]
    return {"present": True, "bad_lines": bad, "stops": len(stops), "modes_seen": modes,
            "mode_ok": modes == [expected_mode] if dec else None, "policy_versions": sorted({r.get("policy_version") for r in dec}),
            "by_rule": by_rule, "blocked_stops": len({r.get("stop_ts") for r in requested}),
            "consult_requests": [{"id": r["id"], "rule": r["rule"], "ts": r["ts"], "turn": r.get("turn")}
                                 for r in requested if r.get("action") == "consult_advisor"],
            "decisions": [{k: r.get(k) for k in ("id", "rule", "state", "action", "turn", "ts", "reason")} for r in dec],
            "closing": closing}


def summarize_jev(path):
    recs, bad = jl(path)
    if recs is None:
        return {"present": False}
    ctx = [r for r in recs if r.get("kind") == "context"]
    ans = {r["ctx_id"]: r for r in recs if r.get("kind") == "jev" and not r.get("replay")}
    rows = []
    for c in ctx:
        a = ans.get(c["ctx_id"]) or {}
        rows.append({"ctx_id": c["ctx_id"], "turn": c.get("turn"), "ts": c.get("ts"),
                     "rules": [(x["rule"], x["state"]) for x in c.get("rules", [])], "status": a.get("status", "pending"),
                     "p_consult": a.get("p_consult"), "would_consult": a.get("would_consult"),
                     "latency_ms": a.get("latency_ms"), "cost_usd_est": a.get("cost_usd_est"), "model": a.get("model")})
    costs = [r["cost_usd_est"] for r in rows if r["cost_usd_est"] is not None]
    lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    cfg = (ctx[0].get("jev") if ctx else None) or {}
    return {"present": True, "bad_lines": bad, "contexts": len(ctx), "answers": rows,
            "missing": [r["ctx_id"] for r in rows if r["status"] in ("pending", "missing_result")],
            "cost_usd_est": sum(costs) if costs else None, "latency_ms_total": sum(lat) if lat else None,
            "question_version": cfg.get("question_version"), "model": cfg.get("model"), "threshold": cfg.get("threshold")}


def overhead(path):
    recs, _ = jl(path)
    if not recs:
        return None
    s = lambda k: round(sum(r.get(k) or 0 for r in recs), 1)  # noqa: E731
    return {"records": len(recs), "snap_ms": s("snap_ms"), "gate_ms": s("gate_ms"), "jev_shadow_ms": s("jev_shadow_ms"),
            "stop_total_ms": s("total_ms")}


def attribute_calls(calls, requests):
    """A call after a consult request (and before the next request) answers it; other calls are voluntary."""
    reqs = sorted(requests, key=lambda r: r["ts"])
    used, out = set(), []
    for c in sorted(calls, key=lambda c: epoch(c["ts"]) or 0):
        t = epoch(c["ts"]) or 0
        match = None
        for r in reqs:
            if r["id"] not in used and r["ts"] <= t:
                match = r
        if match:
            used.add(match["id"])
        out.append(dict(c, answers_request=match["id"] if match else None, voluntary=match is None))
    return out, [r["id"] for r in reqs if r["id"] not in used]


def code_versions():
    files = {f: file_sha(REPO / f) for f in CODE_FILES}
    head = sh(["git", "-C", str(REPO), "rev-parse", "HEAD"])
    dirty = sh(["git", "-C", str(REPO), "status", "--porcelain"])
    return {"repo": str(REPO), "git_head": head["out"].strip() if head["code"] == 0 else None,
            "git_dirty": bool(dirty["out"].strip()) if dirty["code"] == 0 else None,
            "files_sha256": files, "bundle_sha256": sha256(json.dumps(files, sort_keys=True))}


def attempt(task, mode, plan=None):
    modes, modes_sha = load_modes()
    if mode not in modes["modes"]:
        raise SystemExit(f"unknown mode {mode}; known: {', '.join(modes['modes'])}")
    lab = lab_root()
    cfg = lab / "config"
    run_id = uuid.uuid4().hex[:10]
    run_dir, work, sw = lab / "runs" / run_id, lab / "w" / run_id, lab / "runs" / run_id / "sw"
    sw.mkdir(parents=True)
    rj = {"runner_version": RUNNER_VERSION, "run_id": run_id, "task_id": task["task_id"], "mode": mode,
          "plan": plan, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "integrity": []}
    problems = rj["integrity"]

    def save():
        (run_dir / "run.json").write_text(json.dumps(rj, indent=2, ensure_ascii=False, default=str))

    try:
        rj["base"] = export_base(task, work)
    except (SystemExit, Exception) as e:
        rj["completion_status"] = "not_started"
        problems.append(f"base: {e}")
        save()
        print(f"{run_id}: could not prepare the base state: {e}")
        return rj
    rj["task"] = {"file": task["_file"], "sha256": task["_sha256"], "prompt_sha256": sha256(task["prompt"])}
    pol = build_policy(modes, mode)
    policy_file = run_dir / "policy.json"
    policy_file.write_text(json.dumps(pol, indent=2))
    settings = write_settings(run_dir, sw, policy_file)
    env, dropped = agent_env(cfg)
    cmd = claude_cmd(modes, settings)
    ver = sh([claude_bin(), "--version"], env=env)
    rj["versions"] = {"claude_code": (ver["out"] or ver["err"]).strip(), "claude_bin": claude_bin(),
                      "python": sys.version.split()[0], "python_exe": sys.executable, "swarmscope": code_versions(),
                      "modes_file_sha256": modes_sha, "modes_version": modes.get("version")}
    rj["config"] = {"model": modes["common"]["model"], "advisor": modes["common"]["advisor"],
                    "effort": modes["common"]["effort"], "permission_mode": modes["common"]["permission_mode"],
                    "policy": pol, "policy_sha256": file_sha(policy_file), "settings_sha256": file_sha(settings),
                    "argv": cmd, "env_dropped": dropped, "timeout_s": modes["common"]["timeout_s"]}
    pf = preflight(work, cfg)
    rj["preflight"] = pf
    if not pf["ok"]:
        rj["completion_status"] = "not_started"
        problems += [f"preflight: {p}" for p in pf["problems"]]
        save()
        print(f"{run_id}: preflight failed:\n  " + "\n  ".join(pf["problems"]))
        return rj
    save()
    print(f"{run_id}: {task['task_id']} mode {mode} → session running (limit {modes['common']['timeout_s']} s)")
    sess = run_session(cmd, work, env, task["prompt"], run_dir / "stream.jsonl", run_dir / "stderr.txt",
                       modes["common"]["timeout_s"])
    rj["session"] = sess
    init, result, bad = parse_stream(run_dir / "stream.jsonl")
    sid = (init or {}).get("session_id") or (result or {}).get("session_id")
    rj["session"].update(session_id=sid, stream_bad_lines=bad)
    if init:
        rj["loaded"] = {k: init.get(k) for k in ("model", "cwd", "tools", "mcp_servers", "agents", "slash_commands",
                                               "skills", "plugins", "permissionMode", "apiKeySource", "claude_code_version",
                                               "output_style", "memory_paths") if k in init}
        for k, v in not_clean(init).items():
            problems.append(f"{k} loaded in the session: {str(v)[:300]}")
        if init.get("cwd") and Path(init["cwd"]).resolve() != work.resolve():
            problems.append(f"session cwd {init['cwd']} is not the working copy")
    else:
        problems.append("no init message in stream (loaded config unverified)")
    if result:
        rj["result"] = {k: result.get(k) for k in ("subtype", "is_error", "total_cost_usd", "duration_ms",
                                                   "duration_api_ms", "num_turns", "usage", "modelUsage")}
    else:
        problems.append("no result message in stream")
    rj["completion_status"] = ("timeout" if sess["timed_out"] else (result or {}).get("subtype") or
                               f"exit_{sess['exit_code']}") + ("" if not (result or {}).get("is_error") else ":is_error")

    # observation logs: wait for Jev workers, then close open decisions
    hook_env = dict(os.environ, SWARMSCOPE_DIR=str(sw), SWARMSCOPE_POLICY_FILE=str(policy_file))
    jev_timeout = (pol.get("jev") or {}).get("timeout_s", 10) + 35
    t_wait = time.time()
    still = wait_jev(sw / "jev" / f"{sid}.jsonl", jev_timeout) if sid else []
    rj["session"]["jev_wait_s"] = round(time.time() - t_wait, 1)
    if sid:
        f1 = sh([sys.executable, str(REPO / "jevshadow.py"), "--finalize", sid], cwd=str(REPO), env=hook_env)
        f2 = sh([sys.executable, str(REPO / "rules.py"), "--finalize", sid, str(work)], cwd=str(REPO), env=hook_env)
        rj["finalize"] = {"jev": (f1["out"] + f1["err"]).strip()[-500:], "rules": (f2["out"] + f2["err"]).strip()[-800:]}
        if still:
            problems.append(f"jev results still pending after {jev_timeout} s: {still}")

    events, ebad = jl(sw / "events" / f"{sid}.jsonl") if sid else (None, 0)
    tp = next((e.get("transcript_path") for e in events or [] if e.get("transcript_path")), None)
    if not tp and sid:
        hits = list((cfg / "projects").glob(f"*/{sid}.jsonl"))
        tp = str(hits[0]) if hits else None
    if events is None:
        problems.append("no hook events (hooks did not run)")
    rj["observation"] = {"events": len(events or []), "events_bad_lines": ebad, "transcript": tp,
                         "transcript_ok": bool(tp and os.path.exists(tp)), "log_dir": str(sw)}
    if not rj["observation"]["transcript_ok"]:
        problems.append("transcript not found")
    stops = sum(1 for e in events or [] if e.get("event") == "Stop")
    rj["observation"]["stop_events"] = stops

    dec = summarize_decisions(sw / "decisions" / f"{sid}.jsonl", pol["mode"]) if sid else {"present": False}
    rj["decisions"] = dec
    if stops and not dec.get("present"):
        problems.append("Stop events but no decision log (policy did not load?)")
    if dec.get("present") and dec.get("mode_ok") is False:
        problems.append(f"policy mode in log {dec['modes_seen']} != {pol['mode']}")
    if dec.get("present") and dec.get("stops") != stops:
        problems.append(f"decision log covers {dec.get('stops')} Stops, hooks saw {stops}")
    jev = summarize_jev(sw / "jev" / f"{sid}.jsonl") if sid else {"present": False}
    rj["jev"] = jev
    if (pol.get("jev") or {}).get("enabled"):
        if not jev.get("present") and stops:
            problems.append("jev enabled but no jev log")
        elif jev.get("missing"):
            problems.append(f"jev contexts without a result: {jev['missing']}")

    # costs and the advisor
    if rj["observation"]["transcript_ok"]:
        sys.path.insert(0, str(REPO))
        import costs
        c = costs.session_costs(tp, advisor_model=modes["common"]["advisor"])
        rj["costs"] = {"claude_code_total_usd": (result or {}).get("total_cost_usd"),
                       "by_model": {"lead": {k: c["lead"].get(k) for k in ("model", "usd", "in", "out", "cr", "cw5", "cw1h")},
                                    "advisor": {k: c["advisor"].get(k) for k in ("model", "usd", "source", "in", "out", "cr")},
                                    "subagents": {a: {"model": b.get("model"), "usd": b.get("usd")} for a, b in c["subs"].items()}},
                       "computed_total_usd": c["total_usd"], "estimated": c["estimated"], "notes": c["notes"],
                       "prices_as_of": c.get("prices_as_of"), "jev_usd_est": jev.get("cost_usd_est")}
        cc = rj["costs"]["claude_code_total_usd"]
        if cc is not None and abs(cc - c["total_usd"]) > 0.01:
            problems.append(f"cost mismatch: claude code {cc:.4f} vs costs.py {c['total_usd']:.4f}")
        calls, unanswered = attribute_calls(advisor_calls(tp), dec.get("consult_requests") or [])
        rj["advisor"] = {"requested": len(dec.get("consult_requests") or []), "called": len(calls),
                         "returned": sum(1 for x in calls if x["status"] == "returned"),
                         "failed": sum(1 for x in calls if x["status"] == "failed"),
                         "no_result": sum(1 for x in calls if x["status"] == "no_result"),
                         "voluntary": sum(1 for x in calls if x["voluntary"]),
                         "requests_without_call": unanswered, "calls": calls}
    save()

    # result: final diff and tests (hidden tests run on a copy, never inside the agent's working copy)
    rj["diff"] = final_diff(work, rj["base"]["work_commit"], run_dir)
    final = run_dir / "final"
    shutil.copytree(work, final, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    tests = {}
    if task.get("public_tests"):
        tests["public"] = run_tests(task["public_tests"], final)
    h = task.get("hidden_tests")
    if h:
        hdir = expand(h["dir"])
        tests["hidden"] = run_tests(h["cmd"].replace("{hidden}", shlex.quote(hdir)), final)
        tests["hidden"]["dir_files_sha256"] = {p.name: file_sha(p) for p in sorted(Path(hdir).glob("*")) if p.is_file()}
    rj["tests"] = tests
    rj["success"] = bool(tests.get("hidden", {}).get("ok"))
    rj["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save()
    print(line(rj))
    if problems:
        print("  integrity: " + "; ".join(problems))
    return rj


# --- commands ---------------------------------------------------------------------------------------

def line(rj):
    h = (rj.get("tests") or {}).get("hidden") or {}
    adv = rj.get("advisor") or {}
    cost = (rj.get("costs") or {}).get("claude_code_total_usd")
    return (f"{rj['run_id']}  {rj['task_id']:<8} {rj['mode']:<3} {rj.get('completion_status', '?'):<10} "
            f"hidden {h.get('passed', '-')}/{h.get('tests', '-')}  "
            f"${cost if cost is not None else float('nan'):.3f}  opus {adv.get('called', '-')} "
            f"(req {adv.get('requested', '-')}, vol {adv.get('voluntary', '-')})  "
            f"{(rj.get('session') or {}).get('wall_s', '-')} s  "
            f"{'OK' if not rj.get('integrity') else 'INTEGRITY ' + str(len(rj['integrity']))}")


def cmd_setup(_):
    lab = lab_root()
    cfg = lab / "config"
    for d in (cfg, lab / "runs", lab / "w"):
        d.mkdir(parents=True, exist_ok=True)
    s = cfg / "settings.json"
    if not s.exists():
        s.write_text("{}\n")
    agents = REPO / "agents"
    if agents.is_dir():
        (cfg / "agents").mkdir(exist_ok=True)
        for f in agents.glob("*.md"):
            shutil.copy2(f, cfg / "agents" / f.name)
    print(f"lab: {lab}\nconfig (CLAUDE_CONFIG_DIR): {cfg}  agents: {sorted(p.name for p in (cfg / 'agents').glob('*.md'))}")
    print("\nLog in once with this config (opens Claude Code; run /login if asked, then /exit):\n"
          f"  CLAUDE_CONFIG_DIR={shlex.quote(str(cfg))} claude\nThen: python lab/lab.py doctor --ping")


def cmd_doctor(a):
    lab = lab_root()
    cfg = lab / "config"
    env, dropped = agent_env(cfg)
    ok = True
    v = sh([claude_bin(), "--version"], env=env)
    print(f"claude       {claude_bin()}  {(v['out'] or v['err']).strip()}")
    hlp = sh([claude_bin(), "--help"], env=env)
    text = hlp["out"] + hlp["err"]
    missing = [f for f in REQUIRED_FLAGS if f not in text]
    print(f"flags        {'all in --help' if not missing else 'MISSING from --help ' + ' '.join(missing)}"
          + "  (--advisor is hidden from --help; --ping checks the real command line)")
    ok &= not missing
    probe = lab / "w" / f"_doctor-{uuid.uuid4().hex[:6]}"
    probe.mkdir(parents=True, exist_ok=True)
    pf = preflight(probe, cfg)
    print(f"isolation    {'ok' if pf['ok'] else 'PROBLEMS'}")
    for p in pf["problems"]:
        print(f"  - {p}")
    ok &= pf["ok"]
    if Path.home().joinpath(".claude", "CLAUDE.md").exists():
        print(f"  note: {Path.home() / '.claude' / 'CLAUDE.md'} exists (not loaded with a separate config dir, but "
              "it is an ancestor .claude/CLAUDE.md of the lab; preflight flags it if Claude Code could see it)")
    print(f"env dropped  {', '.join(dropped) or 'none'}")
    key = os.environ.get("TYPESAFE_API_KEY") or (Path.home() / ".claude" / "swarmscope" / "typesafe_key").exists()
    print(f"jev key      {'found' if key else 'MISSING (jev answers will be no_key)'}")
    modes, _ = load_modes()
    print(f"modes        {modes.get('version')}: " + ", ".join(f"{k}={v['policy_mode']}" for k, v in modes["modes"].items())
          + f" · model {modes['common']['model']} · advisor {modes['common']['advisor']} · effort {modes['common']['effort']}")
    if a.ping:
        # the exact command line of a run (model, advisor, effort, settings, sources, MCP, permissions),
        # with an empty settings file instead of the hooks
        empty = lab / "w" / "_doctor_settings.json"
        empty.write_text("{}")
        r = sh(claude_cmd(modes, empty), cwd=probe, env=env, timeout=180,
               input="Reply with the single word OK. Do not use any tools.")
        empty.unlink()
        recs = []
        for ln in (r["out"] or "").splitlines():
            try:
                recs.append(json.loads(ln))
            except ValueError:
                pass
        init = next((x for x in recs if x.get("type") == "system" and x.get("subtype") == "init"), None)
        res = next((x for x in reversed(recs) if x.get("type") == "result"), None)
        if not res:
            print(f"ping         FAILED (exit {r['code']}): {(r['err'] or r['out']).strip()[:400]}")
            ok = False
        else:
            print(f"ping         {str(res.get('result', '')).strip()[:40]!r}  ${res.get('total_cost_usd') or 0:.5f}"
                  f"  models: {', '.join(sorted((res.get('modelUsage') or {}).keys())) or '?'}")
            ok &= not res.get("is_error")
        if init:
            adv = {k: v for k, v in init.items() if "advis" in k.lower() or "effort" in k.lower()}
            print(f"loaded       model {init.get('model')} · permission {init.get('permissionMode')} · "
                  f"mcp {init.get('mcp_servers')} · agents {init.get('agents')}")
            builtin = [p.get("name") for p in init.get("plugins") or [] if isinstance(p, dict)]
            print(f"             skills: {init.get('skills')} · slash_commands: {init.get('slash_commands')}")
            print(f"             plugins (builtin are fine): {builtin}")
            print(f"             memory: {init.get('memory_paths')} · output_style: {init.get('output_style')}")
            extra = not_clean(init)
            if extra:
                print(f"             NOT CLEAN: {str(extra)[:400]}")
                ok = False
            print(f"             advisor/effort fields in init: {adv or 'none reported'}")
            print(f"             init keys: {', '.join(sorted(init.keys()))}")
    shutil.rmtree(probe, ignore_errors=True)
    print("ready" if ok else "not ready")
    return 0 if ok else 1


def cmd_run(a):
    rj = attempt(load_task(a.task), a.mode)
    return 0 if rj.get("completion_status") != "not_started" else 1


def cmd_batch(a):
    task = load_task(a.task)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    order = [(m, r + 1) for m in modes for r in range(a.repeats)]
    random.Random(a.seed).shuffle(order)
    batch_id = time.strftime("%Y%m%d-%H%M%S")
    print(f"batch {batch_id} seed {a.seed}: " + ", ".join(f"{m}#{r}" for m, r in order))
    done = []
    for i, (m, r) in enumerate(order):
        rj = attempt(task, m, plan={"batch_id": batch_id, "seed": a.seed, "position": i, "repeat": r,
                                    "order": [f"{x}#{y}" for x, y in order]})
        done.append(rj["run_id"])
        if rj.get("completion_status") == "not_started":
            print("stopping the batch: attempt not started (see its run.json)")
            break
    (lab_root() / "runs" / f"batch-{batch_id}.json").write_text(json.dumps(
        {"batch_id": batch_id, "seed": a.seed, "task": task["task_id"], "order": order, "runs": done}, indent=2))
    return 0


def cmd_summary(_):
    runs = sorted((lab_root() / "runs").glob("*/run.json"), key=lambda p: p.stat().st_mtime)
    for p in runs:
        try:
            print(line(json.loads(p.read_text())))
        except Exception as e:
            print(f"{p.parent.name}: unreadable ({e})")
    if not runs:
        print("no runs yet")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    d = sub.add_parser("doctor")
    d.add_argument("--ping", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--task", required=True)
    r.add_argument("--mode", required=True)
    b = sub.add_parser("batch")
    b.add_argument("--task", required=True)
    b.add_argument("--modes", default="A,B")
    b.add_argument("--repeats", type=int, default=1)
    b.add_argument("--seed", type=int, required=True)
    sub.add_parser("summary")
    a = ap.parse_args(argv)
    return {"setup": cmd_setup, "doctor": cmd_doctor, "run": cmd_run, "batch": cmd_batch,
            "summary": cmd_summary}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
