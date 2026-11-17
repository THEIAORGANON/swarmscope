#!/usr/bin/env python3
"""Swarmscope: manual probe of the TypeSafe Jev Decisions API (stdlib only).

Usage (key never touches the command line or shell history):
    read -s TYPESAFE_API_KEY && export TYPESAFE_API_KEY
    python3 jev_probe.py              # 1 models call + 5 decision calls
    python3 jev_probe.py --runs 10

Env overrides:
    JEV_BASE   default https://api.typesafe.ai   (decisions path: /v1/systemone)
    JEV_PATH   default /v1/systemone
    JEV_MODEL  default jev-latest  (probe only - the response tells us the pinned id)

Prints status, latency, model id, answers with probabilities, usage/cost.
Never prints the key. Writes raw responses to jev_probe_out.jsonl next to this file.
"""
import json, os, sys, time, urllib.request, urllib.error, pathlib, argparse

BASE = os.environ.get("JEV_BASE", "https://api.typesafe.ai").rstrip("/")
PATH = os.environ.get("JEV_PATH", "/v1/systemone")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
KEY = os.environ.get("TYPESAFE_API_KEY", "")
OUT = pathlib.Path(__file__).with_name("jev_probe_out.jsonl")

# A realistic before_delivery checkpoint from test2 (R3 fired, Sonnet did not call Opus).
STATE = {
    "checkpoint_type": "before_delivery",
    "task_goal": "Fix 2 failing tests in an HTTP client and add retries with exponential backoff.",
    "changes": {"production_files": 2, "production_lines": 64, "test_files": 1,
                "critical_paths_touched": ["src/auth/client.py"],
                "kind": "new retry logic + error handling in auth client"},
    "tests": {"ran_after_last_change": True, "result": "7 passed"},
    "errors": {"repeated_same_error": False, "tool_failures": 1},
    "advisor": {"calls_so_far": 0},
    "session": {"elapsed_s": 70, "assistant_turns": 7},
}
QUESTIONS = {
    "consult_advisor": {
        "type": "noul",
        "instructions": ("Before declaring this coding task done, should the agent ask a stronger "
                         "reviewer model for a second opinion? Consider risk of the change, "
                         "test evidence and whether a reviewer is likely to catch something."),
        "criteria": {"true": "A second opinion is likely to catch a real problem worth its cost.",
                     "false": "The evidence is sufficient; a review would add cost without benefit."},
    },
    "change_risk": {
        "type": "score",
        "instructions": "How risky is this change if it contains a subtle bug?",
        "criteria": ["trivial", "low", "moderate", "high"],
    },
    "agent_state": {
        "type": "choice",
        "instructions": "What best describes the agent's progress?",
        "criteria": {"progressing": "Making steady progress toward the goal.",
                     "stuck": "Repeating failing attempts without new information.",
                     "done": "Task appears complete and verified."},
    },
}


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode(errors="replace")
    except Exception as e:  # network, TLS, DNS
        status, raw = None, f"{type(e).__name__}: {e}"
    ms = (time.perf_counter() - t0) * 1000
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    return status, ms, parsed, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    global KEY
    if not KEY:
        import getpass
        KEY = getpass.getpass("TypeSafe API key (input hidden, paste and press Enter): ").strip()
    if not KEY:
        sys.exit("no key given")
    print(f"base={BASE} path={PATH} model={MODEL}\n")

    st, ms, js, raw = call("GET", f"{BASE}/v1/models")
    print(f"[models] status={st} {ms:.0f} ms")
    if js and isinstance(js.get("data"), list):
        print("  ids:", ", ".join(m.get("id", "?") for m in js["data"]))
    else:
        print("  body:", raw[:400])
    print()

    lat = []
    with OUT.open("a") as f:
        for i in range(args.runs):
            body = {"model": MODEL, "state": STATE, "questions": QUESTIONS}
            st, ms, js, raw = call("POST", BASE + PATH, body)
            f.write(json.dumps({"run": i, "status": st, "ms": round(ms), "response": js or raw}) + "\n")
            print(f"[decision {i+1}/{args.runs}] status={st} {ms:.0f} ms")
            if not js or st != 200:
                print("  body:", raw[:600]); print(); continue
            lat.append(ms)
            print("  model:", js.get("model"), "| id:", js.get("id"))
            for q, a in (js.get("answers") or {}).items():
                a = dict(a)
                print(f"  {q}: " + json.dumps(a, ensure_ascii=False))
            print("  usage:", json.dumps(js.get("usage"), ensure_ascii=False))
            extra = sorted(set(js) - {"id", "model", "answers", "usage"})
            if extra:
                print("  other top-level keys:", extra)
            print()
    if lat:
        lat.sort()
        print(f"latency ms: min {lat[0]:.0f} · median {lat[len(lat)//2]:.0f} · max {lat[-1]:.0f} (n={len(lat)})")
    print(f"raw responses appended to {OUT.name}")


if __name__ == "__main__":
    main()
