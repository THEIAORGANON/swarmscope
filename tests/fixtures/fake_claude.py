#!/usr/bin/env python3
"""Fake Claude Code for runner tests: real hooks, scripted agent, init fields from lab/modes.json.
FAKE_SKILLS=<json>  report skills in init      FAKE_NO_STOP=1  never call the Stop hook
FAKE_SLEEP=<s>      sleep before finishing      FAKE_MODEL=<id> report another lead model
FAKE_WEB=main|sub   a WebFetch/WebSearch call   FAKE_VOLUNTARY_ADVISOR=1  call the advisor unasked   FAKE_PROFILE_ADVISOR=<id>  advisor without --advisor
FAKE_SUSPEND=1|noretry  Claude Code bills $0.05 more than the transcript holds; 1: after a retried request
                    (the Mac slept: StreamSuspended, as on 18.11), noretry: with no retry in the transcript
FAKE_ADVISOR_NO_RESULT=1|paid  an advisor request that never returns; paid: Claude Code still bills the advisor
FAKE_ADVISOR_HANDOFF=1|only  on a block: Bash then an advisor call in one message, never run (only: no second call)
FAKE_TAIL_AFTER_RESULT=<s>  after the result: a task notification and one more lead message, then sleep <s>
FAKE_TAIL_SUB=1     with FAKE_TAIL_AFTER_RESULT: a subagent message after the result as well (FAKE_TAIL_SUB_MODEL=<id>)
FAKE_RATE_LIMIT=1|mid|warn  rejected rate-limit event; 1: the model is never reached (as on 17.11),
                    mid: the API fails after work began, warn: the session still finishes normally"""
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

MODES = json.loads((Path(__file__).resolve().parents[2] / "lab" / "modes.json").read_text())["common"]
a = sys.argv[1:]
if "--version" in a:
    print(f"{MODES['claude_code']} (Claude Code)")
    sys.exit()
if "--help" in a:
    print("--disable-slash-commands --effort --settings --setting-sources --strict-mcp-config --output-format "
          "--permission-mode")
    sys.exit()
prompt = sys.stdin.read()
settings = json.load(open(a[a.index("--settings") + 1]))
model = os.environ.get("FAKE_MODEL") or a[a.index("--model") + 1]
# no --advisor (mode A0): an advisor only if the profile slipped one in (FAKE_PROFILE_ADVISOR)
advisor = a[a.index("--advisor") + 1] if "--advisor" in a else os.environ.get("FAKE_PROFILE_ADVISOR")
cwd = os.getcwd()
sid = str(uuid.uuid4())
cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
tp = cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
tp.parent.mkdir(parents=True, exist_ok=True)
tp.touch()


def out(o):
    print(json.dumps(o), flush=True)


def tr(o):
    with open(tp, "a") as f:
        f.write(json.dumps(o) + "\n")


def iso():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def _matches(matcher, tool):
    return not matcher or matcher == "*" or (tool is not None and re.fullmatch(matcher, tool) is not None)


def _run_one(h, d, ev):
    """(output, failure) for one command hook, as Claude Code runs it: a failed start, an exit other than 0 or 2,
    a timeout or invalid JSON is a failure; a hook past its timeout is killed and its output ignored."""
    p = subprocess.Popen(h["command"], shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = p.communicate(json.dumps(d), timeout=h.get("timeout", 60))
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        with open(os.environ.get("FAKE_HOOK_KILLS", os.devnull), "a") as f:
            f.write(ev + "\n")
        return None, "timed out"
    if p.returncode == 2:
        return {"_exit2": stderr}, None
    if p.returncode != 0:
        return None, f"failed (exit {p.returncode}): {stderr.strip()[-200:]}"
    try:
        return (json.loads(stdout) if stdout.strip() else None), None
    except ValueError:
        return None, "invalid JSON output"


def hook(ev, **kw):
    """Every hook of the event whose matcher fits the tool; the first deny or block decides, else the last output.
    onFailure "block": a failed hook blocks (returned as {"_blocked": why})."""
    if "hooks" not in settings:
        return None
    d = dict(session_id=sid, hook_event_name=ev, cwd=cwd, transcript_path=str(tp), **kw)
    last = None
    for entry in settings["hooks"].get(ev, []):
        if not _matches(entry.get("matcher"), kw.get("tool_name")):
            continue
        for h in entry["hooks"]:
            res, failure = _run_one(h, d, ev)
            if failure:
                if h.get("onFailure") == "block":
                    return {"_blocked": f"{ev} hook error: {failure}; blocking because onFailure is \"block\""}
                continue
            if res and (res.get("_exit2") is not None or
                        (res.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"):
                return res
            last = res if res is not None else last
    return last


ABSENT = [t for t in os.environ.get("FAKE_ABSENT_TOOLS", "").split(",") if t]  # tools this session lacks
TOOLS = [t for t in ("Task", "Bash", "Edit", "Read", "Write") if t not in ABSENT]   # main session, as 2.1.326
TOOLS += [t for t in os.environ.get("FAKE_EXTRA_TOOLS", "").split(",") if t]          # a tool that appeared
out({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": model, "tools": TOOLS,
     "mcp_servers": [], "agents": MODES["expected_agents"], "permissionMode": a[a.index("--permission-mode") + 1],
     "claude_code_version": MODES["claude_code"], "skills": json.loads(os.environ.get("FAKE_SKILLS", "[]")),
     "slash_commands": [], "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin",
                                        "source": "cc-plugin-telemetry@builtin"}]})
if os.environ.get("FAKE_FLAKY_FILE"):           # the first session fails mid-way with an API error, limits allowed
    flag = Path(os.environ["FAKE_FLAKY_FILE"])
    if not flag.exists():
        flag.write_text("failed once")
        out({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {"status": "allowed",
             "rateLimitType": "five_hour"}})
        out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "total_cost_usd": 0.17,
             "num_turns": 7, "modelUsage": {model: {}}, "permission_denials": [], "terminal_reason": "api_error"})
        sys.exit(1)
RL = os.environ.get("FAKE_RATE_LIMIT")
if RL:                                           # a rejected rate-limit event, as Claude Code 2.1.326 writes it
    out({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
        "status": "rejected", "resetsAt": 1794933000, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits"}})
if RL in ("1", "mid"):                           # 1: never reached the model (17.11); mid: failed after work began
    out({"type": "assistant", "session_id": sid, "message": {"model": "<synthetic>", "role": "assistant",
                                                           "content": [{"type": "text", "text": "limit reached"}]}})
    out({"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
         "total_cost_usd": 0 if RL == "1" else 0.2, "num_turns": 1 if RL == "1" else 7,
         "modelUsage": {} if RL == "1" else {model: {}}, "permission_denials": [], "terminal_reason": "api_error"})
    sys.exit(1)
if "hooks" not in settings:                     # doctor --ping
    if "advisor tool" in prompt:
        tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m0", "model": model, "content": [
            {"type": "server_tool_use", "id": "srv0", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv0", "content": {"type": "advisor_redacted_result"}}],
            "usage": {"input_tokens": 100, "output_tokens": 10, "iterations": [
                {"type": "message", "input_tokens": 100, "output_tokens": 10},
                {"type": "advisor_message", "model": advisor, "input_tokens": 1000, "output_tokens": 50}]}}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "OK",
         "total_cost_usd": 0.001, "modelUsage": {model: {}}})
    sys.exit()
hook("UserPromptSubmit", prompt=prompt)
def guard_spec():
    """The read guard's spec as the runner wrote it into the settings (SWARMSCOPE_READ_GUARD=... readguard.py)."""
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        for h in entry["hooks"]:
            for tok in shlex.split(h["command"]):
                if tok.startswith("SWARMSCOPE_READ_GUARD="):
                    return json.loads(tok.split("=", 1)[1])
    return {}


TASK_OUT = {}


def task_step(n, arg):
    """Probe steps 28-34 as Claude Code >= 2.1.326 runs them: a background command writes to
    <claude tmp>/<project>/<session>/tasks/<id>.output (the fake takes the root from the guard's spec); a Read goes
    through the PreToolUse hooks and, when let through, reads the file - so a guard that misjudges it leaks."""
    if n == "28":
        root = guard_spec().get("task_output_root") or "/nonexistent"
        f = Path(root) / tp.parent.name / sid / "tasks" / "bfake0001.output"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(arg.replace("echo ", "", 1) + "\n")
        TASK_OUT["f"] = f
        inp, allowed = {"command": arg, "run_in_background": True, "description": "bg"}, True
        text = f"Command running in background with ID: bfake0001. Output is being written to: {f}. You will be notified."
    elif n == "29":
        time.sleep(1.0)                          # the probe plants its files once the tasks folder appears
        inp, allowed, text = {"command": arg}, True, ""
    else:
        f = TASK_OUT.get("f") or Path("/nonexistent/x.output")
        path = {"30": os.environ.get("FAKE_TASK_READ") or str(f), "31": str(f.parent / "neighbour.txt"),
                "32": str(f.parent / "link.output")}.get(n, arg)
        inp = {"file_path": path}
        res = hook("PreToolUse", tool_name="Read", tool_input=inp, tool_use_id=f"t{n}")
        denied = res and ((res.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
                          or "_blocked" in res or res.get("_exit2") is not None)
        if denied:
            allowed, text = False, "PreToolUse:Read hook error: " + json.dumps(res)
        else:
            try:
                allowed, text = True, Path(path).read_text()
            except OSError as e:
                allowed, text = False, f"File does not exist: {e}"
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": f"t{n}", "name": "Read" if n >= "30" else "Bash", "input": inp}]}})
    out({"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": not allowed, "content": text}]}})
    use = {"type": "assistant", "timestamp": iso(), "message": {"id": f"mt{n}", "model": model, "content": [
        {"type": "tool_use", "id": f"t{n}", "name": "Read" if n >= "30" else "Bash", "input": inp}],
        "usage": {"input_tokens": 0, "output_tokens": 0}}}
    tr(use)
    tr({"type": "user", "timestamp": iso(), "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": not allowed, "content": text}]}})


if "authorized test of the sandbox" in prompt:  # lab.py probe: a contained agent (FAKE_SKIP/LEAK/NET break it)
    steps = re.findall(r"^(\d+)\. (?:\(grep subagent\) )?(Bash|Read tool|Write tool|Glob tool|Grep tool|Task tool|"
                       r"Bash tool with run_in_background set to true): (.+)$", prompt, re.M)
    skip = os.environ.get("FAKE_SKIP", "")
    leaked = ""
    for n, tool, arg in steps:
        if n == skip:
            continue
        name = {"Bash": "Bash", "Read tool": "Read", "Write tool": "Write", "Glob tool": "Glob", "Grep tool": "Grep",
                "Task tool": "Task", "Bash tool with run_in_background set to true": "Bash"}[tool]
        if 28 <= int(n) <= 34:                   # background task outputs, in the main session
            task_step(n, arg)
            continue
        if int(n) >= 21:                         # step 21 starts the grep subagent, 22-27 run inside it
            if name == "Task":
                out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_use",
                     "id": "t21", "name": "Agent", "input": {"subagent_type": "grep", "description": "probe",
                                                            "prompt": "steps 22-27"}}]}})
                out({"type": "user", "parent_tool_use_id": None, "message": {"content": [{"type": "tool_result",
                     "tool_use_id": "t21", "content": "Async agent launched successfully."}]}})
                continue
            m = re.match(r'pattern "?(.*?)"?, path "(.*)"$', arg)
            inp = {"pattern": m.group(1), "path": m.group(2)}
            if name in ABSENT:                    # the subagent lacks the tool (FAKE_ABSENT_TOOLS)
                allowed, text = False, f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"
            else:
                # the real guard decides; what it lets through is read the way Claude Code reads it: '~' expanded,
                # edge whitespace trimmed (2.1.326) - so a guard that misjudges such a path leaks here
                res = hook("PreToolUse", tool_name=name, tool_input=inp, agent_type="grep", agent_id="a1")
                denied = res and ((res.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
                                  or "_blocked" in res or res.get("_exit2") is not None)
                if denied:
                    allowed = False
                    text = f"PreToolUse:{name} hook error: " + str((res.get("hookSpecificOutput") or {}).get(
                        "permissionDecisionReason") or res.get("_blocked") or res.get("_exit2"))
                else:
                    target = Path(os.path.expanduser(inp["path"].strip()))
                    target = target if target.is_absolute() else Path(cwd) / target
                    found = [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else []
                    allowed = True
                    text = "\n".join(f"{p}:{p.read_text(errors='replace').strip()}" for p in found[:20]) or "No files found"
            use = {"type": "assistant", "parent_tool_use_id": "t21", "message": {"content": [
                {"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}]}}
            result = {"type": "user", "parent_tool_use_id": "t21", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": not allowed, "content": text}]}}
            out(use)
            out(result)
            sub_tp = Path(str(tp)[:-6]) / "subagents" / "agent-a1.jsonl"      # the subagent's own transcript
            sub_tp.parent.mkdir(parents=True, exist_ok=True)
            with open(sub_tp, "a") as fh:
                fh.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")
            continue
        m = re.match(r"pattern (\S+) in path (.+)", arg)
        inp = ({"command": arg} if name == "Bash" else {"pattern": arg.replace("pattern ", "", 1)} if name == "Glob"
               else {"pattern": m.group(1), "path": m.group(2)} if name == "Grep"
               else {"file_path": arg.split(" containing")[0].replace("create ", "")})
        spill = Path(str(tp)[:-6]) / "tool-results" / "b3euojfv4.txt"     # Claude Code's spilled long output
        if n == "12":
            inp = {"file_path": os.environ.get("FAKE_SPILL_READ") or str(spill), "limit": 5}
        elif n == "14":
            inp = {"file_path": str(tp), "limit": 5}
        elif n == "15":
            inp = {"file_path": str(spill.parent / "probe-write.txt"), "content": "probe"}
        allowed = (n in ("10", "11", "12") or (n == "9" and os.environ.get("FAKE_NET"))
                   or (n == "14" and os.environ.get("FAKE_READ_TRANSCRIPT")))
        text = "ok" if n == "10" else ("<h1>Example Domain</h1>" if allowed else "Operation not permitted")
        if os.environ.get("FAKE_NO_TOOL") == n or name in ABSENT:
            text = f"<tool_use_error>Error: No such tool available: {name}.</tool_use_error>"
        if n == "11":
            spill.parent.mkdir(parents=True, exist_ok=True)
            spill.write_text(arg + "\n")
            text = f"Output too large (198.0KB). Full output saved to: {spill}"
        elif n == "12":
            text = "swarmscope-spill-line swarmscope-spill-line"
        if n == "1" and os.environ.get("FAKE_LEAK"):
            leaked = Path(arg.split()[-1]).read_text()
            text, allowed = leaked, True
        if n == "10":
            Path("inside.txt").write_text("ok\n")
        out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}]}})
        out({"type": "user", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": not allowed, "content": text}]}})
    tr({"type": "assistant", "timestamp": iso(), "message": {"id": "p1", "model": model, "content": [
        {"type": "text", "text": "1 BLOCKED " + leaked}], "usage": {"input_tokens": 10, "output_tokens": 5}}})
    hook("Stop", stop_hook_active=False)
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "1 BLOCKED " + leaked,
         "total_cost_usd": 0.00003, "modelUsage": {model: {}}})
    sys.exit()
if os.environ.get("FAKE_EVIL"):                 # the agent plants programs in its repo's git config
    m = os.environ["FAKE_EVIL"]
    for k, v in (("core.fsmonitor", f"sh -c 'touch {m}'"), ("filter.evil.clean", f"sh -c 'touch {m}; cat'"),
                 ("filter.evil.smudge", f"sh -c 'touch {m}; cat'"), ("diff.evil.textconv", f"sh -c 'touch {m}; cat \"$1\"' --"),
                 ("core.hooksPath", ".evilhooks")):
        subprocess.run(["git", "config", k, v], check=True)
    Path(".gitattributes").write_text("* filter=evil diff=evil\n")
f = Path("src/inventory/stock.py")
f.write_text(f.read_text() + "".join(f"# line {i}\n" for i in range(60)))
hook("PreToolUse", tool_name="Bash", tool_input={"command": "cat > src/inventory/stock.py"})
hook("PostToolUse", tool_name="Bash", tool_input={"command": "cat > src/inventory/stock.py"}, tool_response={"stdout": ""})
tcmd = "PYTHONPATH=src python3 -m unittest discover -s tests -t ."
hook("PostToolUse", tool_name="Bash", tool_input={"command": tcmd}, tool_response={"stdout": "", "stderr": "Ran 3 tests\n\nOK"})
tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m1", "model": model, "content": [{"type": "text", "text": "done"}],
                                                         "usage": {"input_tokens": 1000, "output_tokens": 100}}})
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
cost = 0.002 + 0.001
adv_cost = 0.0                                   # the advisor's share of cost
if os.environ.get("FAKE_WEB"):                   # main: the session calls WebFetch; sub: a subagent calls WebSearch
    web = {"type": "assistant", "timestamp": iso(), "message": {"id": "mw", "model": model, "content": [
        {"type": "tool_use", "id": "tw", "name": "WebFetch" if os.environ["FAKE_WEB"] == "main" else "WebSearch",
         "input": {"url": "https://github.com/pypa/packaging/pull/1"}}], "usage": {"input_tokens": 10, "output_tokens": 5}}}
    if os.environ["FAKE_WEB"] == "main":
        tr(web)
    else:
        sub = Path(str(tp)[:-6]) / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        web["message"]["model"] = "claude-haiku-5-5"
        (sub / "agent-web1.jsonl").write_text(json.dumps(web) + "\n")
if os.environ.get("FAKE_GUARDED_READ"):          # a Read through the PreToolUse hooks, as Claude Code does it
    inp = {"file_path": os.environ["FAKE_GUARDED_READ"]}
    tr({"type": "assistant", "timestamp": iso(), "message": {"id": "mgr", "model": model, "content": [
        {"type": "tool_use", "id": "tgr", "name": "Read", "input": inp}], "usage": {"input_tokens": 10, "output_tokens": 5}}})
    res = hook("PreToolUse", tool_name="Read", tool_input=inp, tool_use_id="tgr")
    refused = res and (res.get("_blocked") or res.get("_exit2") is not None or
                       (res.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny")
    try:
        text = json.dumps(res) if refused else Path(inp["file_path"]).read_text()
    except OSError as e:
        text = f"read failed: {e}"
    tr({"type": "user", "timestamp": iso(), "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tgr", "is_error": bool(refused), "content": text}]}})
    with open(os.environ.get("FAKE_GUARDED_LOG", os.devnull), "a") as f:
        f.write(json.dumps({"refused": bool(refused), "res": res}) + "\n")
if os.environ.get("FAKE_READ_OUTSIDE"):          # a Read that succeeds on a file outside the working copy
    tr({"type": "assistant", "timestamp": iso(), "message": {"id": "mro", "model": model, "content": [
        {"type": "tool_use", "id": "tro", "name": "Read", "input": {"file_path": os.environ["FAKE_READ_OUTSIDE"]}}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}})
    tr({"type": "user", "timestamp": iso(), "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tro", "is_error": False, "content": "x"}]}})
if os.environ.get("FAKE_VOLUNTARY_ADVISOR") and advisor:    # the agent consults the advisor on its own
    tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m1a", "model": model, "content": [
        {"type": "server_tool_use", "id": "srv9", "name": "advisor", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "srv9", "content": {"type": "advisor_redacted_result"}}],
        "usage": {"input_tokens": 1000, "output_tokens": 100, "iterations": [
            {"type": "message", "input_tokens": 1000, "output_tokens": 100},
            {"type": "advisor_message", "model": advisor, "input_tokens": 10000, "output_tokens": 500}]}}})
    cost += 0.003 + 0.04 + 0.01
    adv_cost += 0.05
if os.environ.get("FAKE_ADVISOR_NO_RESULT") and advisor:    # 18.11, run 3206e00905: request, no result, no usage
    tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m1n", "model": model, "content": [
        {"type": "server_tool_use", "id": "srv8", "name": "advisor", "input": {}}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "iterations": [
            {"type": "message", "input_tokens": 10, "output_tokens": 5}]}}})
SUSPEND = os.environ.get("FAKE_SUSPEND")
if SUSPEND == "1":                               # 18.11, run 3ef45c38c8: the record Claude Code 2.1.326 writes
    tr({"type": "system", "subtype": "api_error", "level": "error", "error": {"message": "Connection error.",
        "formatted": "Connection lost while your computer was asleep", "connection": {"code": "StreamSuspended",
        "message": "Stream watchdog detected system suspend; aborting to retry on a fresh connection"}},
        "retryInMs": 605, "retryAttempt": 1, "maxRetries": 10, "source": "connection_retry", "timestamp": iso()})
if not os.environ.get("FAKE_NO_STOP"):
    r = hook("Stop", stop_hook_active=False)
    if r and r.get("decision") == "block":
        time.sleep(0.05)
        handoff = os.environ.get("FAKE_ADVISOR_HANDOFF")   # bj-diag-1 (21.11): Bash -> advisor in one message, never run
        if handoff:
            tr({"type": "assistant", "timestamp": iso(), "message": {"id": "mh", "model": model, "content": [
                {"type": "tool_use", "id": "tu9", "name": "Bash", "input": {"command": "true"}},
                {"type": "server_tool_use", "id": "srv0", "name": "advisor", "input": {}}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "iterations": [
                    {"type": "message", "input_tokens": 10, "output_tokens": 5}]}}})
            tr({"type": "user", "timestamp": iso(), "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu9", "content": "ok"}]}})
            cost += 0.0001
    if r and r.get("decision") == "block" and os.environ.get("FAKE_ADVISOR_HANDOFF") != "only":
        tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m2", "model": model, "content": [
            {"type": "server_tool_use", "id": "srv1", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result", "encrypted_content": "x"}},
            {"type": "text", "text": "advice applied"}],
            "usage": {"input_tokens": 1000, "output_tokens": 100, "iterations": [
                {"type": "message", "input_tokens": 1000, "output_tokens": 100},
                {"type": "advisor_message", "model": advisor, "input_tokens": 10000, "output_tokens": 500}]}}})
        cost += 0.003 + 0.04 + 0.01
        adv_cost += 0.05
    if r and r.get("decision") == "block":
        hook("Stop", stop_hook_active=True)
if SUSPEND:                                      # the aborted attempt's usage is billed to the lead model only
    cost += 0.05
# per-model costUSD as Claude Code reports it
usage = {model: {"costUSD": round(cost - adv_cost, 7)}, **({advisor: {"costUSD": adv_cost}} if adv_cost and advisor else {})}
if os.environ.get("FAKE_ADVISOR_NO_RESULT") == "paid":
    usage[advisor] = {"costUSD": 0.02}
    cost += 0.02
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": cost,
     "duration_ms": 1234, "num_turns": 3,
     "permission_denials": [{"tool_name": "Bash", "tool_use_id": "x", "tool_input": {}}] if os.environ.get("FAKE_DENY") else [], "modelUsage": usage})
if os.environ.get("FAKE_TAIL_AFTER_RESULT"):
    # 2ec1703088 (21.11): after the result a background task notification wakes the model, which works on until
    # the session budget kills it; Claude Code's total does not cover that tail
    out({"type": "system", "subtype": "task_notification", "session_id": sid})
    tail_msg = {"type": "assistant", "timestamp": iso(), "message": {"id": "mtail", "model": model, "content": [
        {"type": "tool_use", "id": "tt1", "name": "Bash", "input": {"command": "true"}}],
        "usage": {"input_tokens": 20000, "output_tokens": 800, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}}
    tr(tail_msg)
    out(tail_msg)
    if os.environ.get("FAKE_TAIL_SUB"):           # a subagent works on after the result too (ChatGPT, 21.11)
        sub_msg = {"type": "assistant", "timestamp": iso(), "parent_tool_use_id": "tsub", "message": {
            "id": "msub", "model": os.environ.get("FAKE_TAIL_SUB_MODEL", model), "content": [{"type": "text", "text": "sub"}],
            "usage": {"input_tokens": 30000, "output_tokens": 1500, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}}
        sp = Path(str(tp)[:-6]) / "subagents" / "agent-s1.jsonl"
        sp.parent.mkdir(parents=True, exist_ok=True)
        with open(sp, "a") as fh:
            fh.write(json.dumps(sub_msg) + "\n")
        out(sub_msg)
    time.sleep(float(os.environ["FAKE_TAIL_AFTER_RESULT"]))
