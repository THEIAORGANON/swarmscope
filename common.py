"""Shared state for swarmscope: event log location + reducer that turns hook events into a snapshot."""
import json
import os
import pathlib
import time
from collections import Counter

BASE = pathlib.Path(os.environ.get("SWARMSCOPE_DIR", pathlib.Path.home() / ".claude" / "swarmscope"))
EVENTS = BASE / "events"
META = BASE / "meta"

SPAWN_TOOLS = {"Task", "Agent"}


def ensure_dirs():
    EVENTS.mkdir(parents=True, exist_ok=True)
    META.mkdir(parents=True, exist_ok=True)


def session_file(sid):
    return EVENTS / f"{sid}.jsonl"


def latest_session_id():
    try:
        files = sorted(EVENTS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except FileNotFoundError:
        return None
    return files[-1].stem if files else None


def load_events(sid):
    out = []
    try:
        with open(session_file(sid), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def load_meta(sid):
    try:
        return json.loads((META / f"{sid}.json").read_text())
    except Exception:
        return {}


def advisor_name():
    """Advisor model: env from the launcher, else advisorModel in settings, else None."""
    env = os.environ.get("SWARMSCOPE_ADVISOR")
    if env:
        return None if env == "off" else env
    for p in (pathlib.Path.cwd() / ".claude" / "settings.local.json",
              pathlib.Path.cwd() / ".claude" / "settings.json",
              pathlib.Path.home() / ".claude" / "settings.json"):
        try:
            v = json.loads(p.read_text()).get("advisorModel")
            if v:
                return v
        except Exception:
            pass
    return None


# --- advisor calls live in the transcript, not in hooks (it is a server-side tool) ---
_adv_cache = {}


def _text_of(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("text", "advice", "content", "result"):
            if k in obj:
                t = _text_of(obj[k])
                if t:
                    return t
    if isinstance(obj, list):
        for x in obj:
            t = _text_of(x)
            if t:
                return t
    return ""


def _epoch(ts):
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def advisor_activity(transcript_path):
    """Advisor calls from the session transcript (hooks never see the advisor: it runs server-side).
    Claude Code stores the advice itself encrypted (advisor_redacted_result), so the text is not readable;
    we keep call times, durations and what the main model wrote right after each consult.
    Returns {"calls", "times", "done_times", "durations", "after", "last", "all", "redacted", "pending"}."""
    empty = {"calls": 0, "last": "", "times": [], "all": [], "done_times": [], "durations": [],
             "after": [], "redacted": False, "pending": False}
    if not transcript_path:
        return empty
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return empty
    cached = _adv_cache.get(transcript_path)
    if cached and cached[0] == size:
        return cached[1]
    res = {k: (list(v) if isinstance(v, list) else v) for k, v in empty.items()}
    seen, open_calls, want_after = set(), {}, False
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                ts = _epoch(rec.get("timestamp"))
                content = ((rec.get("message") or {}).get("content")) or []
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype, name = b.get("type", ""), b.get("name", "")
                    if btype == "server_tool_use" and name == "advisor":
                        if b.get("id") in seen:
                            continue
                        seen.add(b.get("id"))
                        res["calls"] += 1
                        if ts:
                            res["times"].append(ts)
                        open_calls[b.get("id")] = ts
                    elif btype == "advisor_tool_result":
                        start = open_calls.pop(b.get("tool_use_id"), None)
                        if ts:
                            res["done_times"].append(ts)
                            if start:
                                res["durations"].append(ts - start)
                        c = b.get("content")
                        if isinstance(c, dict) and "redacted" in str(c.get("type", "")):
                            res["redacted"] = True
                        else:
                            t = _text_of(c).strip()
                            if t:
                                res["all"].append(t)
                                res["last"] = t
                        want_after = True
                    elif btype == "text" and want_after and b.get("text", "").strip():
                        res["after"].append(" ".join(b["text"].split()))
                        want_after = False
    except OSError:
        pass
    res["pending"] = bool(open_calls)
    _adv_cache[transcript_path] = (size, res)
    return res


_model_cache = {}


def subagent_model(transcript_path, agent_id):
    """Model of a subagent, read from its own transcript (<session>/subagents/agent-<id>.jsonl)."""
    if not transcript_path or not agent_id:
        return None
    path = transcript_path[:-6] + f"/subagents/agent-{agent_id}.jsonl" if transcript_path.endswith(".jsonl") else None
    if not path:
        return None
    if path in _model_cache and _model_cache[path]:
        return _model_cache[path]
    model = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"model"' in line and '"assistant"' in line:
                    try:
                        m = (json.loads(line).get("message") or {}).get("model")
                    except ValueError:
                        continue
                    if m:
                        model = m
                        break
    except OSError:
        pass
    _model_cache[path] = model
    return model


def pretty_model(m):
    """claude-haiku-5-5 → haiku 5.5"""
    if not m:
        return None
    parts = m.replace("claude-", "").split("-")
    if len(parts) >= 3 and parts[1].isdigit():
        return f"{parts[0]} {parts[1]}.{parts[2]}"
    return m


# --- reducer ---------------------------------------------------------------

def _short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_state(events):
    st = {
        "tools": Counter(),          # main-thread tool usage
        "tool_total": 0,
        "sub_tool_total": 0,
        "subagents": {},             # id -> dict
        "sub_order": [],
        "log": [],                   # (ts, tag, text, kind)
        "errors": Counter(),
        "repeat_error": None,
        "turns": 0,
        "working": False,
        "last_tool": "",
        "transcript": None,
        "started": events[0]["ts"] if events else time.time(),
        "lead_marks": [],            # (ts, tool) main-thread tool calls
        "err_marks": [],             # ts
        "work_spans": [],            # [start, end|None]
        "edits": [],                 # (ts, file, old, new, tool)
        "sub_errors": 0,
    }
    have_substart = any(e.get("event") == "SubagentStart" for e in events)
    # newer Claude Code puts agent_id on hook events fired inside a subagent; if we ever see one,
    # trust it and stop guessing. Otherwise fall back to "newest running subagent".
    tagged = any(e.get("agent_id") and e.get("event") == "PreToolUse" for e in events)
    pending_desc = []                # Task descriptions waiting for a SubagentStart

    def log(ts, tag, text, kind):
        st["log"].append((ts, tag, text, kind))

    def running_ids():
        return [i for i in st["sub_order"] if st["subagents"][i]["status"] == "running"]

    def new_sub(key, ts, atype, desc=""):
        st["subagents"][key] = {"type": atype or "agent", "desc": desc, "status": "running",
                                "start": ts, "end": None, "tools": 0, "last": "", "marks": [], "errors": 0,
                                "err_sigs": set()}
        st["sub_order"].append(key)
        log(ts, atype or "agent", f"started · {_short(desc, 34)}" if desc else "started", "sub")

    for e in events:
        ev, ts = e.get("event"), e.get("ts", 0)
        if e.get("transcript_path") and not e.get("agent_id"):
            st["transcript"] = e["transcript_path"]

        if ev == "UserPromptSubmit" and str(e.get("prompt", "")).lstrip().startswith("<agent-message"):
            log(ts, "handback", "subagent report delivered to sonnet", "sub")
        elif ev == "UserPromptSubmit":
            st["turns"] += 1
            st["working"] = True
            st["work_spans"].append([ts, None])
            log(ts, "you", _short(e.get("prompt", "prompt"), 44), "user")
        elif ev == "Stop":
            st["working"] = False
            if st["work_spans"] and st["work_spans"][-1][1] is None:
                st["work_spans"][-1][1] = ts
            log(ts, "sonnet", "turn finished", "lead")
        elif ev == "SubagentStart":
            desc = ""
            for i, (t, d) in enumerate(pending_desc):
                if t == e.get("agent_type") or not t:
                    desc = pending_desc.pop(i)[1]
                    break
            new_sub(e.get("agent_id") or f"sub{len(st['sub_order'])}", ts, e.get("agent_type"), desc)
        elif ev == "SubagentStop":
            key = e.get("agent_id")
            if key and key not in st["subagents"]:
                key = None                      # internal agent we never saw start: ignore
            elif not key:
                r = running_ids()
                key = r[0] if r else None
            if key:
                s = st["subagents"][key]
                s["status"], s["end"] = "done", ts
                log(ts, s["type"], f"done · {s['tools']} tool calls · {ts - s['start']:.0f}s", "sub")
        elif ev in ("PreToolUse",):
            tool = e.get("tool_name", "?")
            if tool in SPAWN_TOOLS:
                atype, desc = e.get("subagent_type", ""), e.get("description", "")
                if have_substart:
                    pending_desc.append((atype, desc))
                else:
                    new_sub(e.get("tool_use_id") or f"task{len(st['sub_order'])}", ts, atype, desc)
                st["tools"][tool] += 1
                st["tool_total"] += 1
                continue
            target = e.get("agent_id")
            if not target and not tagged:
                r = running_ids()
                # main thread is blocked while foreground subagents run → attribute to newest running
                target = r[-1] if r else None
            if target and target in st["subagents"]:
                s = st["subagents"][target]
                s["tools"] += 1
                s["marks"].append(ts)
                s["last"] = f"{tool} {e.get('arg', '')}".strip()
                st["sub_tool_total"] += 1
            else:
                st["tools"][tool] += 1
                st["tool_total"] += 1
                # Bash: the full command (first line) when the hook recorded it, not the 80-char arg
                shown = " ; ".join((e.get("cmd") or "").splitlines())[:300] if tool == "Bash" else ""
                st["last_tool"] = f"{tool} {shown or e.get('arg', '')}".strip()
                st["lead_marks"].append((ts, tool))
                if e.get("new") is not None:
                    st["edits"].append((ts, e.get("arg", "?"), e.get("old", ""), e["new"], tool))
                if tool in ("Edit", "Write", "MultiEdit", "Bash"):
                    log(ts, "sonnet", _short(f"{tool} {e.get('arg', '')}", 44), "lead")
        elif ev == "PostToolUse" and not have_substart and e.get("tool_name") in SPAWN_TOOLS:
            key = e.get("tool_use_id")
            if key in st["subagents"]:
                s = st["subagents"][key]
                s["status"], s["end"] = "done", ts
                log(ts, s["type"], f"done · {s['tools']} tool calls", "sub")
        if e.get("err") and e.get("agent_id") in st["subagents"]:
            # subagent errors: counted per agent, logged once per distinct message, never count as "error x2" for the lead
            s = st["subagents"][e["agent_id"]]
            s["errors"] += 1
            st["sub_errors"] += 1
            sig = (e.get("tool_name"), e["err"][:40])
            if sig not in s["err_sigs"]:
                s["err_sigs"].add(sig)
                log(ts, s["type"], _short(f"error {e.get('tool_name')}: {e['err']}", 44), "err")
        elif e.get("err"):
            sig = (e.get("tool_name"), e["err"][:60])
            st["errors"][sig] += 1
            st["err_marks"].append(ts)
            n = st["errors"][sig]
            log(ts, "error", _short(f"{e.get('tool_name')}: {e['err']}", 44), "err")
            if n >= 2:
                st["repeat_error"] = (ts, sig, n)
                log(ts, "advisor", f"same error x{n} → consult advisor", "adv")
    return st
