#!/usr/bin/env python3
"""Fake Claude Code for runner tests: real hooks, scripted agent."""
import json, os, sys, subprocess, uuid, time, re
from pathlib import Path
a = sys.argv[1:]
if "--version" in a: print("9.9.9 (Claude Code, fake)"); sys.exit()
if "--help" in a: print("--advisor --effort --settings --setting-sources --strict-mcp-config --output-format --permission-mode"); sys.exit()
prompt = sys.stdin.read()
settings = json.load(open(a[a.index("--settings")+1]))
cwd = os.getcwd(); sid = str(uuid.uuid4())
cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]); enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
tp = cfg/"projects"/enc/f"{sid}.jsonl"; tp.parent.mkdir(parents=True, exist_ok=True); tp.touch()
def out(o): print(json.dumps(o), flush=True)
def tr(o): open(tp,"a").write(json.dumps(o)+"\n")
def iso():
    t=time.time(); return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t%1)*1000)
def hook(ev, **kw):
    d = dict(session_id=sid, hook_event_name=ev, cwd=cwd, transcript_path=str(tp), **kw)
    if "hooks" not in settings:
        return None
    cmd = settings["hooks"][ev][0]["hooks"][0]["command"]
    r = subprocess.run(cmd, shell=True, input=json.dumps(d), capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout.strip() else None
out({"type":"system","subtype":"init","session_id":sid,"cwd":cwd,"model":"claude-sonnet-5-5","tools":["Bash","Edit"],"mcp_servers":[],"agents":["search"],"permissionMode":"bypassPermissions"})
if "hooks" not in settings:
    out({"type":"result","subtype":"success","is_error":False,"session_id":sid,"result":"OK","total_cost_usd":0.001,"modelUsage":{"claude-sonnet-5-5":{}}}); sys.exit()
hook("UserPromptSubmit", prompt=prompt)
f = Path("src/inventory/stock.py"); f.write_text(f.read_text() + "".join(f"# line {i}\n" for i in range(60)))
hook("PreToolUse", tool_name="Bash", tool_input={"command":"cat > src/inventory/stock.py"})
hook("PostToolUse", tool_name="Bash", tool_input={"command":"cat > src/inventory/stock.py"}, tool_response={"stdout":""})
tcmd = "PYTHONPATH=src python3 -m unittest discover -s tests -t ."
hook("PostToolUse", tool_name="Bash", tool_input={"command":tcmd}, tool_response={"stdout":"","stderr":"Ran 3 tests\n\nOK"})
tr({"type":"assistant","timestamp":iso(),"message":{"id":"m1","model":"claude-sonnet-5-5","content":[{"type":"text","text":"done"}],"usage":{"input_tokens":1000,"output_tokens":100}}})
r = hook("Stop", stop_hook_active=False)
cost = 0.003
if r and r.get("decision") == "block":
    time.sleep(0.05)
    tr({"type":"assistant","timestamp":iso(),"message":{"id":"m2","model":"claude-sonnet-5-5","content":[{"type":"server_tool_use","id":"srv1","name":"advisor","input":{}},{"type":"advisor_tool_result","tool_use_id":"srv1","content":{"type":"advisor_redacted_result","encrypted_content":"x"}},{"type":"text","text":"advice applied"}],"usage":{"input_tokens":1000,"output_tokens":100,"iterations":[{"type":"message","input_tokens":1000,"output_tokens":100},{"type":"advisor_message","model":"claude-opus-5-5","input_tokens":10000,"output_tokens":500}]}}})
    cost += 0.003 + 0.04 + 0.01
    hook("Stop", stop_hook_active=True)
out({"type":"result","subtype":"success","is_error":False,"session_id":sid,"total_cost_usd":cost,"duration_ms":1234,"num_turns":3})
