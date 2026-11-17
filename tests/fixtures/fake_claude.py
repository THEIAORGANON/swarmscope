#!/usr/bin/env python3
"""Fake Claude Code for runner tests: real hooks, scripted agent, init fields from lab/modes.json.
FAKE_SKILLS=<json>  report skills in init      FAKE_NO_STOP=1  never call the Stop hook
FAKE_SLEEP=<s>      sleep before finishing      FAKE_MODEL=<id> report another lead model"""
import json
import os
import re
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
advisor = a[a.index("--advisor") + 1]
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


def hook(ev, **kw):
    if "hooks" not in settings:
        return None
    d = dict(session_id=sid, hook_event_name=ev, cwd=cwd, transcript_path=str(tp), **kw)
    cmd = settings["hooks"][ev][0]["hooks"][0]["command"]
    r = subprocess.run(cmd, shell=True, input=json.dumps(d), capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout.strip() else None


out({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": model, "tools": ["Bash", "Edit"],
     "mcp_servers": [], "agents": MODES["expected_agents"], "permissionMode": a[a.index("--permission-mode") + 1],
     "claude_code_version": MODES["claude_code"], "skills": json.loads(os.environ.get("FAKE_SKILLS", "[]")),
     "slash_commands": [], "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin",
                                        "source": "cc-plugin-telemetry@builtin"}]})
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
if "authorized test of the sandbox" in prompt:  # lab.py probe: a contained agent (FAKE_SKIP/LEAK/NET break it)
    steps = re.findall(r"^(\d+)\. (Bash|Read tool|Write tool): (.+)$", prompt, re.M)
    skip = os.environ.get("FAKE_SKIP", "")
    leaked = ""
    for n, tool, arg in steps:
        if n == skip:
            continue
        name = {"Bash": "Bash", "Read tool": "Read", "Write tool": "Write"}[tool]
        inp = {"command": arg} if name == "Bash" else {"file_path": arg.split(" containing")[0].replace("create ", "")}
        allowed = n == "10" or (n == "9" and os.environ.get("FAKE_NET"))
        text = "ok" if n == "10" else ("<h1>Example Domain</h1>" if allowed else "Operation not permitted")
        if n == "1" and os.environ.get("FAKE_LEAK"):
            leaked = Path(arg.split()[-1]).read_text()
            text, allowed = leaked, True
        if n == "10":
            Path("inside.txt").write_text("ok\n")
        out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": inp}]}})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                                                       "is_error": not allowed, "content": text}]}})
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
if not os.environ.get("FAKE_NO_STOP"):
    r = hook("Stop", stop_hook_active=False)
    if r and r.get("decision") == "block":
        time.sleep(0.05)
        tr({"type": "assistant", "timestamp": iso(), "message": {"id": "m2", "model": model, "content": [
            {"type": "server_tool_use", "id": "srv1", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": {"type": "advisor_redacted_result", "encrypted_content": "x"}},
            {"type": "text", "text": "advice applied"}],
            "usage": {"input_tokens": 1000, "output_tokens": 100, "iterations": [
                {"type": "message", "input_tokens": 1000, "output_tokens": 100},
                {"type": "advisor_message", "model": advisor, "input_tokens": 10000, "output_tokens": 500}]}}})
        cost += 0.003 + 0.04 + 0.01
        hook("Stop", stop_hook_active=True)
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": cost,
     "duration_ms": 1234, "num_turns": 3, "modelUsage": {model: {}, **({advisor: {}} if cost > 0.01 else {})}})
