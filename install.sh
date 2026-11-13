#!/usr/bin/env bash
# swarmscope installer.  ./install.sh --dry-run   shows the settings.json diff and changes nothing.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${SWARMSCOPE_DIR:-$HOME/.claude/swarmscope}"
SETTINGS="$HOME/.claude/settings.json"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

merge_settings() {  # $1 = output path
python3 - "$SETTINGS" "$DEST" "$1" <<'PY'
import json, sys, os
src, dest, out = sys.argv[1:]
try: s = json.load(open(src))
except FileNotFoundError: s = {}
py = "python3"
hook = f'{py} "{dest}/hook.py"'
hooks = s.setdefault("hooks", {})
for ev, matcher in [("UserPromptSubmit", None), ("PreToolUse", "*"), ("PostToolUse", "*"),
                    ("PostToolUseFailure", "*"), ("SubagentStart", None), ("SubagentStop", None), ("Stop", None)]:
    groups = hooks.setdefault(ev, [])
    if any(h.get("command") == hook for g in groups for h in g.get("hooks", [])):
        continue
    g = {"hooks": [{"type": "command", "command": hook, "timeout": 5}]}
    if matcher: g = {"matcher": matcher, **g}
    groups.append(g)
if s.get("statusLine") and "swarmscope" not in json.dumps(s["statusLine"]):
    print(f"note: replacing your existing statusLine: {json.dumps(s['statusLine'])}", file=sys.stderr)
s["statusLine"] = {"type": "command", "command": f'{py} "{dest}/statusline.py"', "padding": 0}
json.dump(s, open(out, "w"), indent=2); open(out, "a").write("\n")
PY
}

TMP="$(mktemp)"; merge_settings "$TMP"
echo "── ~/.claude/settings.json diff ──"
diff -u "${SETTINGS}" "$TMP" 2>/dev/null || diff -u /dev/null "$TMP" || true
if [ $DRY = 1 ]; then rm -f "$TMP"; echo "(dry run — nothing changed)"; exit 0; fi

mkdir -p "$DEST" "$HOME/.claude/agents" "$HOME/.local/bin"
cp "$SRC"/{common.py,costs.py,hook.py,statusline.py,dashboard.py,swarmscope} "$DEST/"
chmod +x "$DEST"/*.py "$DEST/swarmscope"
ln -sf "$DEST/swarmscope" "$HOME/.local/bin/swarmscope"

for f in "$SRC"/agents/*.md; do
  t="$HOME/.claude/agents/$(basename "$f")"
  if [ -e "$t" ]; then echo "skip: $t already exists"; else cp "$f" "$t"; echo "added agent: $t"; fi
done

echo "installing rich into $DEST/.venv …"
python3 -m venv "$DEST/.venv" && "$DEST/.venv/bin/pip" install -q rich

[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
mv "$TMP" "$SETTINGS"
echo
echo "done. backup of old settings saved next to it (settings.json.bak.*)"
echo "preview:  $DEST/.venv/bin/python $DEST/dashboard.py --demo"
echo "run:      swarmscope        (make sure ~/.local/bin is on PATH)"
