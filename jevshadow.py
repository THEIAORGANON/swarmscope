"""Jev shadow mode (step 3 of the escalation experiment): what would a decision model say at the same
checkpoints where the Rules Engine decides? Records only. Nothing here changes a rules decision, blocks
a Stop or talks to Sonnet.

Flow at every Stop of the main session (hook.py, after the rules gate has written its decisions):
  1. build a DecisionContext from the same facts the rules use (events, repo snapshots, advisor times)
  2. append it to $SWARMSCOPE_DIR/jev/<session>.jsonl  (kind "context", linked to the rules decision ids,
     carrying the exact request that will be sent)
  3. start a detached worker that asks Jev and appends the answer (kind "jev"). Claude Code does not wait
     on the network, only for the context to be written and the worker to start.
Every context ends with a result: ok | invalid | model_mismatch | refusal | error | timeout | no_key |
worker_failed, or missing_result once `--finalize` finds none. A missing answer is never a "skip".

Independence: Jev never sees the rules' verdicts. It does see some of the same inputs (change size, the
critical-path classification), so the two judgements are independent algorithms, not independent data.

Data sent by default: counts and booleans only. Opt in per project for more: "include_task" (the current
turn's request, first 120 chars, redacted; a runner can set SWARMSCOPE_TASK_SUMMARY instead),
"include_paths" (relative paths), "include_error_text" (redacted, 80 chars).
Never sent: file contents, diffs, commands, the transcript.

The key only ever goes to the TypeSafe endpoint below. Project config cannot change the endpoint; tests
use SWARMSCOPE_JEV_TEST_ENDPOINT (process environment only, refused unless it is a loopback address).

Enable in <project>/.claude/swarmscope-policy.json:
  {"mode": "observe", "jev": {"enabled": true}}
Key: $TYPESAFE_API_KEY, or a file "typesafe_key" in $SWARMSCOPE_DIR or ~/.claude/swarmscope (chmod 600).

CLI:
  python jevshadow.py --show <session_id>          rules vs Jev per checkpoint
  python jevshadow.py --finalize <session_id>      mark contexts with no result as missing_result
  python jevshadow.py --replay <session_id> [--current-question]
                                                   ask again (stored request by default); never replaces
                                                   the original answers
"""
import hashlib
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONTEXT_VERSION = "dc-2"
QUESTION_VERSION = "q-consult-2"
# The one policy question. Frozen per version: change the text → bump QUESTION_VERSION.
QUESTIONS = {
    "consult_advisor": {
        "type": "noul",
        "instructions": ("At this checkpoint in a coding task, would consulting a stronger advisor model be expected "
                         "to improve the likelihood of satisfying the task requirements enough to justify its "
                         "additional cost and latency? Base the judgment on the available task context, observed "
                         "changes, test results, unresolved failures, and prior consultations."),
        "criteria": {"true": "The expected improvement from consultation justifies its additional cost and latency.",
                     "false": "The expected improvement from consultation does not justify its additional cost "
                              "and latency."},
    },
}

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
WORKER_GRACE_S = 30                              # beyond timeout_s before a context counts as missing_result

DEFAULTS = {
    "enabled": False,
    "model": "jev-1.14.0",                       # pinned; never jev-latest in experiments
    "timeout_s": 10,
    "threshold": 0.5,                            # provisional; calibrate on pilot tasks, not experiment tasks
    "include_task": False,
    "include_paths": False,
    "include_error_text": False,
    "price_in_per_mtok": 0.042,                  # $/1M input tokens (output is free)
    "price_out_per_mtok": 0.0,
}


def config(pol):
    """Jev settings for this project, or None when shadow mode is off. Unknown keys (base_url…) are ignored."""
    user = (pol or {}).get("jev")
    if not isinstance(user, dict) or user.get("enabled") is not True:
        return None
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def endpoint():
    """The only place the key is sent. A test endpoint must come from the process environment and be local."""
    test = os.environ.get("SWARMSCOPE_JEV_TEST_ENDPOINT")
    if test:
        host = urllib.parse.urlparse(test).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            return test
    return ENDPOINT


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


def config_hash(cfg, questions=None):
    return _hash({"cfg": {k: cfg[k] for k in sorted(cfg)}, "q": questions or QUESTIONS, "ctx": CONTEXT_VERSION})


def _redact(text):
    from common import redact                    # no fallback: without redaction nothing free-text is sent
    return redact(text)


def current_request(events):
    """The user's request for the current turn (subagent hand-backs are not requests)."""
    last = None
    for e in events:
        if e.get("agent_id") or e.get("event") != "UserPromptSubmit":
            continue
        p = str(e.get("prompt") or "").strip()
        if p and not p.startswith("<agent-message"):
            last = p
    return last


# --- DecisionContext ------------------------------------------------------------------------------

def build_context(events, pol, change, adv_times, now, has_tests, cfg, history_available=True):
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
                   "critical_files": len(critical)}
        if cfg.get("include_paths"):
            changes["paths"] = [d["path"] for d in src][:10]
            changes["critical_paths"] = critical[:5]
        since = change.get("since_advisor")
        if since is not None:
            sp = rules.prod_changes(since, pol) or []
            changes["since_last_consultation"] = {"production_files": len(sp),
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
              "repeated_unresolved": (r2.get("signals") or {}).get("count", 0)}
    if cfg.get("include_error_text"):
        errors["last_error"] = _redact(f["errors"][-1]["raw"][:80]) if f["errors"] else None

    prod_times = change.get("prod_times") or src_times
    first_change = min(prod_times) if prod_times else None
    if history_available:
        advisor = {"history_available": True, "consultations_so_far": len(adv),
                   "seconds_since_last_consultation": round(now - adv[-1]) if adv else None,
                   "consulted_after_first_code_change": (any(t > first_change for t in adv)
                                                         if first_change is not None else None)}
    else:
        advisor = {"history_available": False}

    ctx = {"checkpoint_type": "before_delivery",
           "session": {"elapsed_s": round(now - t0), "turn": f["turn"], "edit_tool_calls": len(f["edits"]),
                       "shell_commands": len(f["bash"])},
           "changes": changes, "tests": tests, "errors": errors, "advisor": advisor}
    summary = os.environ.get("SWARMSCOPE_TASK_SUMMARY")
    if summary:
        ctx["task"] = {"source": "runner_summary", "text": _redact(" ".join(summary.split())[:200])}
    elif cfg.get("include_task"):
        req = current_request(events)
        ctx["task"] = {"source": "current_turn_request",
                       "text": _redact(" ".join(req.split())[:120]) if req else None}
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


def _probability(a):
    """The noul probability, or None when the answer is not a finite number in [0, 1]."""
    if not isinstance(a, dict) or a.get("type") != "noul":
        return None
    p = a.get("noul")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
        return None
    return float(p)


def ask_jev(request, cfg, key, opener=None):
    """One request (`request` = the stored {"model", "state", "questions"}). Returns a result dict;
    never raises, never contains the key."""
    opener = opener or urllib.request.urlopen
    url = endpoint()
    req = urllib.request.Request(url, data=json.dumps(request).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    res = {"requested_model": request["model"], "threshold": cfg["threshold"], "endpoint": url}
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
    u = js.get("usage") if isinstance(js.get("usage"), dict) else None
    res["usage"] = u
    if u and isinstance(u.get("cost"), (int, float)) and not isinstance(u.get("cost"), bool):
        res["cost_usd"] = u["cost"]
    elif u and isinstance(u.get("input_tokens"), int):
        res["cost_usd_est"] = round(u["input_tokens"] * cfg["price_in_per_mtok"] / 1e6 +
                                    (u.get("output_tokens") or 0) * cfg["price_out_per_mtok"] / 1e6, 10)
    else:
        res["cost_usd_est"] = None                           # unknown, not free
    a = (js.get("answers") or {}).get("consult_advisor") if isinstance(js.get("answers"), dict) else None
    res["answer"] = a
    if isinstance(a, dict) and a.get("type") == "refusal":
        res["status"] = "refusal"
        return res
    p = _probability(a)
    if p is None:
        res.update(status="invalid", error="consult_advisor is not a noul probability in [0, 1]")
        return res
    res["p_consult"] = p
    if res["model"] != request["model"]:
        res.update(status="model_mismatch", error=f"asked for {request['model']}, got {res['model']}")
        return res
    res.update(status="ok", would_consult=p >= cfg["threshold"])
    return res


def _clean(text, key):
    t = str(text)
    if key:
        t = t.replace(key, "***")
    return _redact(" ".join(t.split()))[:200]


# --- journal ---------------------------------------------------------------------------------------

def jev_file(base, sid):
    return pathlib.Path(base) / "jev" / f"{sid}.jsonl"


def _append(path, rec):
    """One JSON line, one write() on an O_APPEND descriptor: concurrent writers never interleave lines."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def load(path):
    """(records, corrupt_line_count). Corrupt lines are reported, not silently dropped."""
    out, bad = [], 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        pass
    return out, bad


def _load(path):
    return load(path)[0]


# --- hook entry point + worker ----------------------------------------------------------------------

def shadow(sid, base, events, pol, change, adv_times, now, has_tests, rule_records, history_available=True):
    """Called by hook.py at Stop. Writes the context and starts the worker; returns the context id or None."""
    cfg = config(pol)
    if not cfg:
        return None
    path = jev_file(base, sid)
    ctx_id = f"{sid[:8]}-{uuid.uuid4().hex[:10]}"
    decisions = [r for r in rule_records if r.get("kind") == "decision"]
    features = build_context(events, pol, change, adv_times, now, has_tests, cfg, history_available)
    request = {"model": cfg["model"], "state": features, "questions": QUESTIONS}
    rec = {"kind": "context", "ctx_id": ctx_id, "ts": now, "context_version": CONTEXT_VERSION,
           "checkpoint_type": "before_delivery", "turn": decisions[0].get("turn") if decisions else None,
           "policy_version": pol.get("policy_version"), "mode": pol.get("mode"),
           "rules": [{"id": r["id"], "rule": r["rule"], "state": r["state"], "action": r.get("action")}
                     for r in decisions],
           "jev": dict({k: v for k, v in cfg.items() if k != "enabled"}, question_version=QUESTION_VERSION,
                       questions_hash=_hash(QUESTIONS), config_hash=config_hash(cfg), endpoint=endpoint()),
           "request": request, "request_hash": _hash(request)}
    _append(path, rec)
    if os.environ.get("SWARMSCOPE_JEV_SYNC") == "1":            # tests / debugging: answer inline
        run_worker(str(path), ctx_id)
        return ctx_id
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", str(path), ctx_id],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception as e:
        _append(path, {"kind": "jev", "ctx_id": ctx_id, "ts": time.time(), "status": "worker_failed",
                       "error": _clean(f"{type(e).__name__}: {e}", None)})
    return ctx_id


def _context(path, ctx_id):
    return next((r for r in _load(path) if r.get("kind") == "context" and r.get("ctx_id") == ctx_id), None)


def run_worker(path, ctx_id, replay=False, current_question=False):
    """Ask Jev for one stored context and append exactly one result record."""
    path = pathlib.Path(path)
    ctx = _context(path, ctx_id)
    if ctx is None:
        return None
    rec = {"kind": "jev", "ctx_id": ctx_id}
    try:
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in (ctx.get("jev") or {}).items() if k in DEFAULTS})
        request = dict(ctx["request"])
        if current_question:
            request["questions"] = QUESTIONS
        rec.update(question_version=QUESTION_VERSION if current_question else ctx["jev"].get("question_version"),
                   request_hash=_hash(request), same_request=_hash(request) == ctx.get("request_hash"))
        key = load_key(str(path.parent.parent))
        if not key:
            rec.update(status="no_key", requested_model=request["model"], threshold=cfg["threshold"])
        else:
            rec.update(ask_jev(request, cfg, key))
    except Exception as e:
        rec.update(status="worker_failed", error=_clean(f"{type(e).__name__}: {e}", None))
    rec["ts"] = time.time()
    if replay:
        rec["replay"] = True
    _append(path, rec)
    return rec


def finalize(path, now=None):
    """Mark every context that has no original result and is older than its timeout + grace as missing_result.
    Idempotent. The runner calls it after waiting for the workers."""
    now = now or time.time()
    recs = _load(path)
    answered = {r["ctx_id"] for r in recs if r.get("kind") == "jev" and not r.get("replay")}
    out = []
    for c in (r for r in recs if r.get("kind") == "context"):
        limit = (c.get("jev") or {}).get("timeout_s", DEFAULTS["timeout_s"]) + WORKER_GRACE_S
        if c["ctx_id"] not in answered and now - c.get("ts", now) >= limit:
            rec = {"kind": "jev", "ctx_id": c["ctx_id"], "ts": now, "status": "missing_result", "final": True}
            _append(path, rec)
            out.append(rec)
    return out


def pending(path):
    recs = _load(path)
    answered = {r["ctx_id"] for r in recs if r.get("kind") == "jev" and not r.get("replay")}
    return [c["ctx_id"] for c in recs if c.get("kind") == "context" and c["ctx_id"] not in answered]


# --- CLI -----------------------------------------------------------------------------------------

APPLIED = {"requested": "requested from Sonnet", "advised": "shown to the user", "triggered": "logged only",
           "deferred": "deferred (background agents)", "limited": "not applied (block limit)"}


def describe_rules(ctx, decisions):
    """Three separate facts: which consult rules fired, whether that was applied, whether the advisor was
    then called (outcome/followup from the decision log)."""
    fired = [r for r in ctx.get("rules", []) if r.get("action") == "consult_advisor"
             and r["state"] not in ("pass", "skipped")]
    if not fired:
        return "no consult rule fired"
    results = {d["id"]: d for d in decisions if d.get("kind") in ("outcome", "followup")}
    parts = []
    for r in fired:
        res = results.get(r["id"])
        called = {"observed": "advisor called", "not_observed": "advisor not called",
                  "unverified": "advisor call unverified"}.get((res or {}).get("outcome"), "result pending")
        parts.append(f"{r['rule']} fired · {APPLIED.get(r['state'], r['state'])} · {called}")
    return "; ".join(parts)


def show(sid, base):
    path = jev_file(base, sid)
    recs, bad = load(path)
    if not recs:
        print(f"no jev records for {sid}")
        return
    decisions = _load(pathlib.Path(base) / "decisions" / f"{sid}.jsonl")
    orig, replays = {}, {}
    for r in recs:
        if r.get("kind") == "jev":
            (replays.setdefault(r["ctx_id"], []) if r.get("replay") else orig.setdefault(r["ctx_id"], [])).append(r)
    for c in (r for r in recs if r.get("kind") == "context"):
        f = c["features"] if "features" in c else c["request"]["state"]
        ch, adv = f.get("changes", {}), f.get("advisor", {})
        print(f"{c['ctx_id']}  turn {c.get('turn')}  {c['jev'].get('question_version')}")
        print(f"  rules: {describe_rules(c, decisions)}")
        a = (orig.get(c["ctx_id"]) or [None])[0]
        if a is None:
            print("  jev:   pending (no result yet)")
        elif a["status"] == "ok":
            print(f"  jev:   p={a['p_consult']:.2f} → {'consult' if a['would_consult'] else 'no consult'} "
                  f"at {a['threshold']} [{a.get('latency_ms')} ms]")
        else:
            print(f"  jev:   {a['status']}" + (f" ({a['error']})" if a.get("error") else ""))
        for rp in replays.get(c["ctx_id"], []):
            same = "same request" if rp.get("same_request") else rp.get("question_version")
            p = f"p={rp['p_consult']:.2f}" if isinstance(rp.get("p_consult"), float) else rp["status"]
            print(f"  replay ({same}): {p}")
        print(f"  facts: prod {ch.get('production_files', '?')} files / {ch.get('production_lines', '?')} lines · "
              f"tests {f['tests']['last_result']} · consultations so far "
              f"{adv.get('consultations_so_far', 'unknown') if adv.get('history_available') else 'unknown'}")
    if bad:
        print(f"WARNING: {bad} corrupt line(s) in {path}")


def main(argv):
    if len(argv) >= 3 and argv[0] == "--worker":
        run_worker(argv[1], argv[2])
        return 0
    from common import BASE
    if len(argv) == 2 and argv[0] == "--show":
        show(argv[1], BASE)
        return 0
    if len(argv) == 2 and argv[0] == "--finalize":
        for r in finalize(jev_file(BASE, argv[1])):
            print(r["ctx_id"], r["status"])
        print("pending:", pending(jev_file(BASE, argv[1])) or "none")
        return 0
    if len(argv) >= 2 and argv[0] == "--replay":
        path = jev_file(BASE, argv[1])
        for c in [r for r in _load(path) if r.get("kind") == "context"]:
            r = run_worker(str(path), c["ctx_id"], replay=True, current_question="--current-question" in argv)
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
