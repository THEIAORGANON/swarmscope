"""Jev shadow mode (step 3 of the escalation experiment): what would a decision model say at the same
checkpoints where the Rules Engine decides? Records only. Nothing here changes a rules decision, blocks
a Stop or talks to Sonnet.

Flow at every Stop of the main session (hook.py, after the rules gate has written its decisions):
  1. build a DecisionContext from the same facts the rules use (events, repo snapshots, advisor times)
  2. append it to $SWARMSCOPE_DIR/jev/<session>.jsonl  (kind "context", linked to the rules decision ids)
  3. start a detached worker that asks Jev and appends the answer (kind "jev"): Claude Code never waits
     on the network. The worker writes a result for every context: ok | error | timeout | refusal | no_key.
     A missing answer is never recorded as "skip".

The rules' own decisions are NOT sent to Jev: shadow mode compares two independent judgements.
Data sent (only when the project turns Jev on): counts, relative file paths, test/error/advisor facts,
the first 120 characters of the user's task (redacted; "include_task_goal": false turns it off).
Never sent: file contents, diffs, commands, the transcript.

Enable in <project>/.claude/swarmscope-policy.json:
  {"mode": "observe", "jev": {"enabled": true}}
Key: $TYPESAFE_API_KEY, or a file "typesafe_key" in $SWARMSCOPE_DIR or ~/.claude/swarmscope (chmod 600).

CLI:
  python jevshadow.py --show <session_id>       rules vs Jev, one line per checkpoint
  python jevshadow.py --replay <session_id>     ask Jev again for every stored context (current question)
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONTEXT_VERSION = "dc-1"
QUESTION_VERSION = "q-consult-1"
# The one policy question. Frozen per version: change the text → bump QUESTION_VERSION.
QUESTIONS = {
    "consult_advisor": {
        "type": "noul",
        "instructions": ("A coding agent has stopped and is about to hand its work back to the user. It can ask a "
                         "stronger, more expensive reviewer model for a second opinion first; a review costs "
                         "roughly as much as all of the agent's own work so far. Should it ask for the review "
                         "now? Judge from the task, the size and kind of the code changes, the test evidence, "
                         "repeated errors and earlier reviews."),
        "criteria": {"true": "A review now is likely to catch a real problem that is worth its cost.",
                     "false": "The evidence is sufficient, or a review now would add cost without benefit."},
    },
}

DEFAULTS = {
    "enabled": False,
    "model": "jev-1.14.0",                       # pinned; never jev-latest in experiments
    "base_url": "https://api.typesafe.ai",
    "path": "/v1/systemone",
    "timeout_s": 10,
    "threshold": 0.5,                            # provisional; calibrate on pilot tasks, not experiment tasks
    "include_task_goal": True,
    "price_in_per_mtok": 0.042,                  # $/1M input tokens (output is free)
    "price_out_per_mtok": 0.0,
}


def config(pol):
    """Jev settings for this project, or None when shadow mode is off."""
    user = (pol or {}).get("jev")
    if not isinstance(user, dict) or not user.get("enabled"):
        return None
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def config_hash(cfg):
    blob = json.dumps({"cfg": {k: cfg[k] for k in sorted(cfg) if k not in ("base_url", "path")},
                       "q": QUESTIONS, "ctx": CONTEXT_VERSION}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _redact(text):
    from common import redact
    return redact(text)


def task_goal(events):
    for e in events:
        if e.get("agent_id") or e.get("event") != "UserPromptSubmit":
            continue
        p = str(e.get("prompt") or "").strip()
        if p and not p.startswith("<agent-message"):
            return _redact(" ".join(p.split())[:120])
    return None


# --- DecisionContext ------------------------------------------------------------------------------

def build_context(events, pol, change, adv_times, now, has_tests, cfg):
    """Features for one checkpoint. Pure: same inputs → same context (tests rely on it)."""
    import rules
    f = rules.facts(events, pol)
    change = change or {}
    adv = sorted(t for t in (adv_times or []) if t <= now)
    t0 = min((e.get("ts", now) for e in events), default=now)

    diff = change.get("files")
    if diff is None:
        changes = {"known": False}
    else:
        prod = rules.prod_changes(diff, pol) or []
        src = rules.source_changes(diff, pol) or []
        tests = [d for d in src if rules.classify(d["path"], pol) == "test"]
        critical = [d["path"] for d in prod if rules._match(d["path"], pol["risk"]["critical"])]
        changes = {"known": True, "production_files": len(prod),
                   "production_lines": sum(d["added"] + d["deleted"] for d in prod),
                   "test_files": len(tests), "test_lines": sum(d["added"] + d["deleted"] for d in tests),
                   "paths": [d["path"] for d in src][:10], "critical_paths": critical[:5]}
        since = change.get("since_advisor")
        if since is not None:
            sp = rules.prod_changes(since, pol) or []
            changes["since_last_review"] = {"production_files": len(sp),
                                            "production_lines": sum(d["added"] + d["deleted"] for d in sp)}

    src_times = change.get("src_times") or []
    t_change = max(src_times) if src_times else None
    runs = f["tests"]
    after = [t for t in runs if t_change is not None and
             (t["ts"] > t_change or (t["ts"] == t_change and t.get("runs_last")))]
    last = (after or runs or [None])[-1]
    tests = {"project_has_tests": bool(has_tests), "runs": len(runs),
             "last_result": last["result"] if last else "none",
             "ran_after_last_change": (bool(after) if t_change is not None else None)}

    r2 = rules.r2_repeat_error(f, [], pol)
    errors = {"tool_failures": len(f["errors"]),
              "repeated_unresolved": (r2.get("signals") or {}).get("count", 0),
              "last_error": _redact(f["errors"][-1]["raw"][:80]) if f["errors"] else None}

    prod_times = change.get("prod_times") or src_times
    first_change = min(prod_times) if prod_times else None
    advisor = {"reviews_so_far": len(adv),
               "seconds_since_last_review": round(now - adv[-1]) if adv else None,
               "reviewed_after_first_code_change": (any(t > first_change for t in adv)
                                                    if first_change is not None else None)}

    ctx = {"checkpoint_type": "before_delivery",
           "session": {"elapsed_s": round(now - t0), "turn": f["turn"], "file_edits": len(f["edits"]),
                       "shell_commands": len(f["bash"])},
           "changes": changes, "tests": tests, "errors": errors, "advisor": advisor}
    if cfg.get("include_task_goal"):
        ctx["task"] = task_goal(events)
    return ctx


# --- the Jev call ----------------------------------------------------------------------------------

def load_key(base=None):
    k = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if k:
        return k
    for d in filter(None, [base, os.environ.get("SWARMSCOPE_DIR"), pathlib.Path.home() / ".claude" / "swarmscope"]):
        p = pathlib.Path(d) / "typesafe_key"
        try:
            k = p.read_text().strip()
            if k:
                return k
        except OSError:
            pass
    return None


def ask_jev(features, cfg, key, opener=None):
    """One request. Returns a result dict; never raises, never contains the key."""
    opener = opener or urllib.request.urlopen
    body = {"model": cfg["model"], "state": features, "questions": QUESTIONS}
    req = urllib.request.Request(cfg["base_url"].rstrip("/") + cfg["path"], data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {key}",
                                                         "Content-Type": "application/json"})
    res = {"question_version": QUESTION_VERSION, "requested_model": cfg["model"], "threshold": cfg["threshold"]}
    t0 = time.perf_counter()
    try:
        with opener(req, timeout=cfg["timeout_s"]) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode(errors="replace")
    except Exception as e:                                   # timeout, DNS, TLS, refused
        res["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        timed_out = "timed out" in str(e).lower() or isinstance(e, TimeoutError)
        res.update(status="timeout" if timed_out else "error", error=_clean(f"{type(e).__name__}: {e}", key))
        return res
    res["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    res["http_status"] = status
    try:
        js = json.loads(raw)
    except ValueError:
        js = None
    if status != 200 or not isinstance(js, dict):
        res.update(status="error", error=_clean(raw[:200], key))
        return res
    res["model"] = js.get("model")
    res["usage"] = js.get("usage")
    u = js.get("usage") or {}
    if isinstance(u.get("cost"), (int, float)):
        res["cost_usd"] = u["cost"]
    else:
        res["cost_usd_est"] = round((u.get("input_tokens") or 0) * cfg["price_in_per_mtok"] / 1e6 +
                                    (u.get("output_tokens") or 0) * cfg["price_out_per_mtok"] / 1e6, 10)
    a = (js.get("answers") or {}).get("consult_advisor")
    res["answer"] = a
    if not isinstance(a, dict) or a.get("type") == "refusal" or not isinstance(a.get("noul"), (int, float)):
        res["status"] = "refusal" if isinstance(a, dict) and a.get("type") == "refusal" else "error"
        if res["status"] == "error":
            res["error"] = "no consult_advisor probability in the answer"
        return res
    p = float(a["noul"])
    res.update(status="ok", p_consult=p, would_consult=p >= cfg["threshold"])
    return res


def _clean(text, key):
    t = str(text)
    if key:
        t = t.replace(key, "***")
    return _redact(" ".join(t.split()))[:200]


# --- hook entry point + worker ----------------------------------------------------------------------

def jev_file(base, sid):
    return pathlib.Path(base) / "jev" / f"{sid}.jsonl"


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:              # one short line per write: O_APPEND is atomic
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def shadow(sid, base, events, pol, change, adv_times, now, has_tests, rule_records):
    """Called by hook.py at Stop. Writes the context and starts the worker; returns the context id or None."""
    cfg = config(pol)
    if not cfg:
        return None
    path = jev_file(base, sid)
    n = sum(1 for r in _load(path) if r.get("kind") == "context")
    ctx_id = f"{sid[:8]}-c{n + 1}"
    decisions = [r for r in rule_records if r.get("kind") == "decision"]
    rec = {"kind": "context", "ctx_id": ctx_id, "ts": now, "context_version": CONTEXT_VERSION,
           "checkpoint_type": "before_delivery", "turn": decisions[0].get("turn") if decisions else None,
           "policy_version": pol.get("policy_version"), "mode": pol.get("mode"),
           "rules": [{"id": r["id"], "rule": r["rule"], "state": r["state"], "action": r.get("action")}
                     for r in decisions],
           "jev": dict({k: v for k, v in cfg.items() if k != "enabled"}, question_version=QUESTION_VERSION,
                       config_hash=config_hash(cfg)),
           "features": build_context(events, pol, change, adv_times, now, has_tests, cfg)}
    _append(path, rec)
    if os.environ.get("SWARMSCOPE_JEV_SYNC") == "1":            # tests / debugging: answer inline
        run_worker(str(path), ctx_id, cfg)
    else:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", str(path), ctx_id],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    return ctx_id


def run_worker(path, ctx_id, cfg=None, replay=False):
    path = pathlib.Path(path)
    ctx = next((r for r in _load(path) if r.get("kind") == "context" and r.get("ctx_id") == ctx_id), None)
    if ctx is None:
        return None
    if cfg is None:
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in (ctx.get("jev") or {}).items() if k in DEFAULTS})
    base = path.parent.parent
    key = load_key(str(base))
    if not key:
        res = {"question_version": QUESTION_VERSION, "requested_model": cfg["model"], "status": "no_key",
               "threshold": cfg["threshold"]}
    else:
        res = ask_jev(ctx["features"], cfg, key)
    rec = dict(res, kind="jev", ctx_id=ctx_id, ts=time.time())
    if replay:
        rec["replay"] = True
    _append(path, rec)
    return rec


# --- CLI -----------------------------------------------------------------------------------------

def _rules_verdict(ctx):
    asked = [r["rule"] for r in ctx.get("rules", []) if r.get("action") == "consult_advisor"
             and r["state"] not in ("pass", "skipped")]
    return ("consult (" + ",".join(asked) + ")") if asked else "no consult"


def show(sid, base):
    recs = _load(jev_file(base, sid))
    if not recs:
        print(f"no jev records for {sid}")
        return
    answers = {}
    for r in recs:
        if r.get("kind") == "jev":
            answers.setdefault(r["ctx_id"], []).append(r)
    for c in (r for r in recs if r.get("kind") == "context"):
        f = c["features"]
        ch = f.get("changes", {})
        facts = (f"prod {ch.get('production_files', '?')}f/{ch.get('production_lines', '?')}l · "
                 f"tests {f['tests']['last_result']} · reviews {f['advisor']['reviews_so_far']}")
        line = f"{c['ctx_id']:<14} rules: {_rules_verdict(c):<22} "
        for a in answers.get(c["ctx_id"], []) or [{"status": "pending"}]:
            tag = " (replay)" if a.get("replay") else ""
            if a["status"] == "ok":
                line += f"jev: p={a['p_consult']:.2f} → {'consult' if a['would_consult'] else 'no consult'}" \
                        f" [{a.get('latency_ms')} ms]{tag}  "
            else:
                line += f"jev: {a['status']}{tag}  "
        print(line + "\n" + " " * 15 + facts)


def main(argv):
    if len(argv) >= 3 and argv[0] == "--worker":
        run_worker(argv[1], argv[2])
        return 0
    from common import BASE
    if len(argv) == 2 and argv[0] == "--show":
        show(argv[1], BASE)
        return 0
    if len(argv) == 2 and argv[0] == "--replay":
        path = jev_file(BASE, argv[1])
        for c in [r for r in _load(path) if r.get("kind") == "context"]:
            r = run_worker(str(path), c["ctx_id"], replay=True)
            print(c["ctx_id"], r.get("status"), r.get("p_consult"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        if len(sys.argv) > 1 and sys.argv[1] == "--worker":
            sys.exit(0)                                   # a detached worker never surfaces errors
        raise
