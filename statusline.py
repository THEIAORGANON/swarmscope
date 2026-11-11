#!/usr/bin/env python3
"""Claude Code status line:  sonnet 5.5 · lead │ haiku [swarming] │ subagents [2/3] │ opus [on call · 1] │ tools [42]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import META, advisor_activity, advisor_name, build_state, ensure_dirs, load_events  # noqa: E402


def c(hexcol, s, bold=False):
    h = hexcol.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m{s}\033[0m"


ORANGE, BLUE, GREEN, PURPLE, DIM = "#e8925a", "#7fb2f0", "#8fd19e", "#b59cf0", "#6b7280"


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    sid = d.get("session_id") or "unknown"
    model = ((d.get("model") or {}).get("display_name") or "claude").lower()

    # remember model + transcript for the dashboard
    try:
        ensure_dirs()
        (META / f"{sid}.json").write_text(json.dumps({
            "model": model, "transcript_path": d.get("transcript_path"),
            "cwd": (d.get("workspace") or {}).get("current_dir") or d.get("cwd"),
            "cost": (d.get("cost") or {}).get("total_cost_usd"),
        }))
    except Exception:
        pass

    st = build_state(load_events(sid))
    subs = st["subagents"].values()
    total = len(st["sub_order"])
    running = sum(1 for s in subs if s["status"] == "running")

    sep = c(DIM, " │ ")
    parts = [c(ORANGE, model, True) + c(DIM, " · lead")]

    swarm = "working" if running else ("idle" if total else "standby")
    parts.append(c(DIM, "swarm ") + c(BLUE, f"[{swarm}]", bool(running)))
    parts.append(c(DIM, "subagents ") + c(BLUE, f"[{running}/{total}]" if running else f"[{total}]"))

    adv = advisor_name()
    if adv:
        a = advisor_activity(d.get("transcript_path") or st["transcript"])
        label = f"[on call · {a['calls']}]" if a["calls"] else "[on call]"
        parts.append(c(DIM, f"{adv} ") + c(PURPLE, label))
    else:
        parts.append(c(DIM, "advisor ") + c(DIM, "[off]"))

    parts.append(c(DIM, "tools ") + c(GREEN, f"[{st['tool_total'] + st['sub_tool_total']}]"))
    if st["repeat_error"]:
        parts.append(c(PURPLE, "same error x%d" % st["repeat_error"][2], True))
    print(sep.join(parts))


main()
