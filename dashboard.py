#!/usr/bin/env python3
"""Swarmscope: live terminal dashboard for a Claude Code session (Sonnet lead · Haiku swarm · Opus advisor).

    dashboard.py                follow the most recent session
    dashboard.py --session ID   follow a specific session
    dashboard.py --demo         scripted demo session, to preview the look
    dashboard.py --intro        show the start-up logo first
"""
import argparse
import datetime as dt
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (advisor_activity, advisor_name, build_state, latest_session_id,  # noqa: E402
                    load_events, load_meta, session_file, subagent_model, pretty_model)

from rich import box  # noqa: E402
from rich.align import Align  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

ORANGE, BLUE, GREEN, PURPLE, RED = "#e8925a", "#7fb2f0", "#8fd19e", "#b59cf0", "#e06c75"
DIM, FAINT, FG, WHITE = "#7d8590", "#3d434d", "#d7dae0", "#ffffff"
SPIN = "◐◓◑◒"
GLYPHS = "ｱｲｳｴｵｶｷｸｹｺ#%&@$01<>/\\=+*"
KIND_COLOR = {"lead": ORANGE, "sub": BLUE, "adv": PURPLE, "err": RED, "user": FG,
              "gate": PURPLE, "gate_miss": RED}
LEFT_W = 26
FPS = 10


# --- small helpers -----------------------------------------------------------

ANIM = {}


def ease(key, target, k=0.25):
    """Smoothly approach target across frames (bars grow, counters roll)."""
    v = ANIM.get(key, target)
    v += (target - v) * k
    if abs(target - v) < 0.02:
        v = target
    ANIM[key] = v
    return v


def fade(color, age, dur=1.5, start=WHITE):
    """Fresh things start bright and settle into their colour."""
    return mix(start, color, min(1.0, max(0.0, age / dur)))


def sparkline(values, width):
    chars = "▁▂▃▄▅▆▇█"
    mx = max(values) if values and max(values) > 0 else 1
    return "".join(" " if v == 0 else chars[min(7, int(v / mx * 7.99))] for v in values[-width:])

def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def pulse(frame, speed=0.35):
    return (math.sin(frame * speed) + 1) / 2


def blink(frame, period=4):
    """Hard on/off — survives 256-colour terminals (Terminal.app) where soft fades round away."""
    return (frame // period) % 2 == 0


BG = "#1e2229"


def bar(frac, width, color, frame=None):
    """Filled bar; with frame given, a highlight sweeps across the filled part (shimmer)."""
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    t = Text()
    h = frame % (n + 8) if (frame is not None and n) else -1
    for i in range(n):
        if i == h:
            t.append("█", style=mix(color, WHITE, 0.65))
        elif abs(i - h) == 1:
            t.append("█", style=mix(color, WHITE, 0.3))
        else:
            t.append("█", style=color)
    t.append("░" * (width - n), style=FAINT)
    return t


def decrypt(text, progress, rnd):
    """Text 'decoding' from random glyphs, left to right."""
    if progress >= 1:
        return text
    k = int(len(text) * progress)
    return text[:k] + "".join(c if c == " " else rnd.choice(GLYPHS) for c in text[k:])


# --- particle connectors -------------------------------------------------------

def vline(width, frame, active, color):
    t = Text(" " * (width // 2))
    if active:
        t.append("·•●•"[frame % 4], style=color)
    else:
        t.append("│", style=FAINT)
    return t


def fan(width, centers, frame, mode, active, colors):
    """Two rows joining a trunk at the middle with branches at `centers`.
    mode 'out': trunk on top, arrows down to branches, particles run outward.
    mode 'in' : branches on top, trunk below, particles run inward."""
    mid = width // 2
    lo, hi = min(centers + [mid]), max(centers + [mid])
    line = [" "] * width
    for x in range(lo, hi + 1):
        line[x] = "─"
    for i, c in enumerate(centers):
        line[c] = "╭" if c == lo else ("╮" if c == hi else "┬") if mode == "out" else \
                  ("╰" if c == lo else ("╯" if c == hi else "┴"))
    if mid in centers:
        line[mid] = "┼"
    else:
        line[mid] = "┴" if mode == "out" else "┬"
    if len(centers) == 1 and centers[0] == mid:
        line[mid] = "│"
    styles = [FAINT] * width
    if active and hi > lo:
        for side, end in ((-1, lo), (1, hi)):
            span = abs(end - mid)
            if span < 2:
                continue
            step = (frame * 2) % (span + 6)
            for trail, ch, col in ((0, "●", BLUE), (1, "•", mix(BLUE, FAINT, 0.3)), (2, "·", mix(BLUE, FAINT, 0.6))):
                d = step - trail
                if 0 < d < span:
                    x = mid + side * d if mode == "out" else end - side * d
                    line[x], styles[x] = ch, col
    row = Text()
    for ch, st in zip(line, styles):
        row.append(ch, style=st)
    arrows = [" "] * width
    for c in centers:
        arrows[c] = "▼" if mode == "out" else "│"
    arow = Text()
    for x, ch in enumerate(arrows):
        col = colors.get(x, FAINT)
        arow.append(ch, style=col)
    trunk = vline(width, frame, active, BLUE)
    return [trunk, row, arow] if mode == "out" else [arow, row, trunk]


# --- sections --------------------------------------------------------------------

def header(lead, adv, frame=0, width=104):
    t = Text(justify="center")
    pos = (frame * 0.6) % 34 - 6
    for i, ch in enumerate("SWARMSCOPE"):
        t.append(ch, style=f"bold {mix(FG, ORANGE if i < 5 else PURPLE, max(0, 1 - abs(i - pos) / 2.2))}")
    for col, name, verb in ((ORANGE, lead.upper(), "BUILDS"), (BLUE, "HAIKU", "SWARMS"),
                            (PURPLE, (adv or "").upper(), "ADVISES")):
        if not name:
            continue
        t.append("  ·  ", style=FAINT)
        t.append(name + " ", style=f"bold {col}")
        t.append(verb, style=f"bold {FG}")
    legend = Text(justify="center")
    for col, label in ((ORANGE, f"{lead} · lead"), (BLUE, "haiku · swarm"),
                       (GREEN, "tools"), (PURPLE, f"{adv or 'advisor'} · advisor")):
        legend.append("■ ", style=col)
        legend.append(label + "    ", style=DIM)
    rule = Text(no_wrap=True, overflow="crop")
    p1 = (frame * 3) % (width + 30) - 15
    p2 = width - ((frame * 2) % (width + 30) - 15)
    for x in range(width):
        g = max(0, 1 - abs(x - p1) / 9)
        b = max(0, 1 - abs(x - p2) / 7)
        col = mix(FAINT, ORANGE, g) if g >= b else mix(FAINT, BLUE, b)
        rule.append("═", style=col)
    return Group(t, rule, legend)


def lead_panel(lead, st, frame, adv_flash, now=0):
    t = Text(justify="center")
    t.append(f"{lead} · main session\n", style=f"bold {ORANGE}")
    if st["working"]:
        t.append(f"{SPIN[frame % 4]} working", style=ORANGE)
    else:
        t.append("● idle", style=DIM)
    t.append(f"   turn {st['turns']}   tools {round(ease('lead_tools', st['tool_total'], 0.3))}\n", style=FG)
    err_age = now - st["err_marks"][-1] if st["err_marks"] else 99
    if err_age < 2:
        t.append(f"✕ {st['log'][-1][2] if st['log'] else 'tool failed'}"[:48], style=f"bold {RED}")
    elif adv_flash:
        t.append("◆ applying advisor guidance", style=f"bold {PURPLE}")
    else:
        t.append(st["last_tool"][:48] or "edits + runs tests", style=DIM)
    border = mix(ORANGE, "#fff1e6", pulse(frame)) if st["working"] else mix(ORANGE, FAINT, 0.4)
    if err_age < 2:
        border = mix(RED, WHITE, pulse(frame, 1.2) * 0.5)
    return Panel(t, box=box.HEAVY if err_age < 2 else box.SQUARE, border_style=border, padding=(0, 2))


def tools_panel(st):
    g = Table.grid(padding=(0, 2))
    g.add_column(style=FG, no_wrap=True, min_width=6)
    g.add_column(no_wrap=True)
    g.add_column(justify="right", no_wrap=True)
    top = st["tools"].most_common(3)
    mx = max([n for _, n in top] or [1])
    total = max(st["tool_total"], 1)
    for name, n in top:
        frac = ease(f"tool:{name}", n / mx)
        g.add_row(name, bar(frac, 20, GREEN), Text(f"{round(ease(f'toolc:{name}', n, 0.3)):>3}  {n / total:>4.0%}", style=GREEN))
    if not top:
        g.add_row(Text("no tool calls yet", style=DIM), Text(""), Text(""))
    title = Text.assemble((" TOOLS", f"bold {GREEN}"), (" · main thread ", GREEN))
    sub = Text.assemble(("calls ", DIM), (str(round(ease("calls", st["tool_total"], 0.3))), f"bold {GREEN}"),
                        ("   in subagents ", DIM), (str(round(ease("subcalls", st["sub_tool_total"], 0.3))), f"bold {BLUE}"))
    return Panel(Group(g, sub), title=title, title_align="left", box=box.SQUARE,
                 border_style=GREEN, padding=(0, 1))


def subagent_block(st, frame, width, now):
    order = st["sub_order"]
    if not order:
        return [Align.center(Text("no subagents yet · haiku standing by", style=DIM))], False, False
    shown = order[-3:]
    n = len(shown)
    cw = (width - (n - 1)) // n
    centers = [i * (cw + 1) + cw // 2 for i in range(n)]
    mx = max([st["subagents"][k]["tools"] for k in shown] + [1])
    grid = Table.grid(padding=0)
    for i in range(n):
        grid.add_column(width=cw)
        if i < n - 1:
            grid.add_column(width=1)
    cells, colors_out, colors_in = [], {}, {}
    any_running, recent_done = False, False
    for i, k in enumerate(shown):
        s = st["subagents"][k]
        running = s["status"] == "running"
        any_running |= running
        recent_done |= (not running and s["end"] and now - s["end"] < 4)
        colors_out[centers[i]] = BLUE if running else FAINT
        colors_in[centers[i]] = GREEN if (not running) else FAINT
        inner = cw - 4
        age = now - s["start"]
        done_age = now - s["end"] if s["end"] else 99
        t = Text(justify="center")
        t.append(decrypt(s["type"], age / 0.8, random) + "\n", style=f"bold {fade(FG, age, 0.8, BLUE)}")
        t.append("Haiku 5.5\n", style=BLUE)
        t.append_text(bar(ease(f"sub:{k}", s["tools"] / mx), max(4, inner - 5), BLUE, frame if running else None))
        t.append(f" {s['tools']:>3}\n", style=FG)
        t.append(((s["desc"] or s["last"] or "…")[:inner]) + "\n", style=FG)
        if running:
            t.append(f"{SPIN[frame % 4]} running {now - s['start']:.0f}s", style=BLUE)
        else:
            t.append(f"✓ done {s['end'] - s['start']:.0f}s", style=f"bold {fade(GREEN, done_age, 1.5)}" if done_age < 1.5 else GREEN)
        on = blink(frame + i * 2, 4)
        border = (mix(BLUE, WHITE, 0.75) if on else BLUE) if running else FAINT
        bx = (box.HEAVY if on else box.SQUARE) if running else box.SQUARE
        if age < 1.0:
            border, bx = fade(BLUE, age, 1.0), box.HEAVY
        elif done_age < 1.8:
            border = WHITE if blink(frame, 2) else GREEN
            bx = box.HEAVY
        cells.append(Panel(t, box=bx, border_style=border, padding=(0, 0)))
        if i < n - 1:
            cells.append(Text(""))
    grid.add_row(*cells)
    rows = fan(width, centers, frame, "out", any_running, colors_out)
    rows.append(grid)
    rows += fan(width, centers, frame, "in", recent_done or (not any_running and st["working"]), colors_in)
    extra = len(order) - n
    if extra > 0:
        rows.append(Text(f"+{extra} earlier", style=DIM, justify="right"))
    return rows, any_running, recent_done


def back_panel(lead, st):
    subs = list(st["subagents"].values())
    all_done = bool(subs) and all(s["status"] == "done" for s in subs)
    col = ORANGE if all_done else FAINT
    t = Text(justify="center")
    t.append(f"back to {lead} · lead", style=f"bold {col}")
    t.append("  ·  review + verify", style=FG if all_done else FAINT)
    return Panel(t, box=box.SQUARE, border_style=col, padding=(0, 1))


def advisor_panel(adv, a, st, height, frame, now, adv_ts, rnd, gates=None):
    flash = adv_ts and now - adv_ts < 2.5
    lines = []   # (priority, Text) — lower priority dropped first when short on space

    def L(prio, *parts):
        lines.append((prio, Text.assemble(*parts)))

    name = (adv or "advisor").upper()
    L(9, (name, f"bold {PURPLE}"))
    L(9, ("advisor · " + ("on call" if adv else "off"), PURPLE))
    gates = gates or {}

    def gm(name):
        state, ts = gates.get(name, ("idle", None))
        return gate_mark(state, ts, now, frame), state

    strip = []
    for name in ("PLAN", "ERR×2", "DONE"):
        (ic, sty), _ = gm(name)
        strip += [(ic + " ", sty), ("ERR2" if name == "ERR×2" else name, sty if ic != "◇" else FAINT), (" ", "")]
    L(9, *strip)
    L(3, (""))

    def gate_line(name):
        (ic, sty), state = gm(name)
        label_style = {"idle": FG, "missed": mix(RED, FAINT, 0.3)}.get(state, sty)
        L(8, (ic + " ", sty), (GATE_TEXT[name], label_style))

    gate_line("PLAN")
    for s in ("reads the whole", "session, every", "tool call and", "every result"):
        L(1, ("┆ " + s, DIM))
    L(2, ("┆", DIM))
    L(8, ("┆ last advice:", DIM))
    advice = a["last"] or ("—" if adv else "set --advisor opus")
    prog = min(1.0, (now - adv_ts) / 1.4) if adv_ts else 1.0
    advice = decrypt(advice, prog, rnd)
    words, cur, wrapped = advice.split(), "", []
    for w in words:
        if len(cur) + len(w) + 1 > 19:
            wrapped.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        wrapped.append(cur)
    for w in wrapped[:3]:
        L(8, ("┆ » ", PURPLE), (w, f"bold {mix(PURPLE, WHITE, 0.5) if prog < 1 else PURPLE}"))
    L(2, ("┆", DIM))
    L(7, ("┆ calls      ", DIM), (str(a["calls"]), f"bold {PURPLE}"))
    L(2, ("┆", DIM))
    L(1, ("┆ silent on every", DIM))
    L(1, ("┆ routine turn", DIM))
    L(2, ("┆", DIM))
    rep = st["repeat_error"]
    gate_line("ERR×2")
    if rep:
        L(6, ("┆ ", DIM), (f"{rep[1][0]}: {rep[1][1]}"[:19], PURPLE))
    L(2, ("┆", DIM))
    for s in ("never writes", "code itself.", "lead applies", "the advice."):
        L(0, ("┆ " + s, DIM))
    L(2, ("┆", DIM))
    gate_line("DONE")

    budget = height - 2
    keep = list(range(len(lines)))
    for p in range(0, 10):
        if len(keep) <= budget:
            break
        keep = [i for i in keep if lines[i][0] > p]
    body = Text("\n").join(lines[i][1] for i in keep)
    border = (WHITE if blink(frame, 2) else PURPLE) if flash else PURPLE
    return Panel(body, box=box.HEAVY if flash else box.SQUARE, border_style=border,
                 padding=(0, 1), height=height, width=LEFT_W)


def timeline_panel(st, a_times, width, now, window, frame, roomy=False):
    label_w = 8
    cols = max(10, width - 4 - label_w - 1)
    t0 = now - window

    def cell(ts):
        return int((ts - t0) / window * cols)

    def lane(name, color, spans=(), marks=(), mark_ch="█", diamonds=(), errs=()):
        chars = [("─", FAINT)] * cols
        for s, e in spans:
            a, b = max(0, cell(s)), min(cols - 1, cell(e if e else now))
            for x in range(a, b + 1):
                chars[x] = ("━", mix(color, FAINT, 0.55))
        for ts in marks:
            x = cell(ts)
            if 0 <= x < cols:
                chars[x] = (mark_ch, fade(color, now - ts, 1.5))
        for ts in errs:
            x = cell(ts)
            if 0 <= x < cols:
                chars[x] = ("✕", RED)
        for ts in diamonds:
            x = cell(ts)
            if 0 <= x < cols:
                fresh = now - ts < 2.5
                chars[x] = ("◆", (WHITE if blink(frame, 2) else PURPLE) if fresh else PURPLE)
        chars[-1] = ("▏", mix(FG, FAINT, pulse(frame, 0.6)))
        t = Text(f"{name[:label_w]:<{label_w}} ", style=color)
        for ch, col in chars:
            t.append(ch, style=col)
        return t

    rows = [lane("sonnet", ORANGE, st["work_spans"], [m[0] for m in st["lead_marks"]], "▮",
                 errs=st["err_marks"])]
    rows.append(lane("opus", PURPLE, diamonds=a_times))
    if roomy:
        sep = Text(f"{'swarm':<{label_w}} ", style=DIM)
        sep.append("┈" * cols, style=FAINT)
        rows.append(sep)
    for k in st["sub_order"][-3:]:
        s = st["subagents"][k]
        rows.append(lane(s["type"], BLUE, [(s["start"], s["end"])], s["marks"], "▮"))
    axis = Text(" " * (label_w + 1))
    ticks = ["─"] * cols
    for q in range(5):
        ticks[min(cols - 1, q * (cols - 1) // 4)] = "┴"
    axis.append("".join(ticks), style=FAINT)
    if roomy:
        rows.append(Text(""))
    lab = Text(" " * (label_w + 1))
    left, right = f"-{window:.0f}s", "now"
    lab.append(left + " " * max(1, cols - len(left) - len(right)) + right, style=DIM)
    return Panel(Group(*rows, axis, lab), title=Text(" timeline ", style=FG), title_align="left",
                 box=box.SQUARE, border_style=FAINT, padding=(0, 1))


def code_panel(st, now, height, frame):
    inner_h = height - 2
    if not st["edits"]:
        body = Text("waiting for the first edit…", style=DIM)
        return Panel(body, title=Text(" ✎ live code ", style=f"bold {ORANGE}"), title_align="left",
                     box=box.SQUARE, border_style=FAINT, padding=(0, 1), height=height)
    ts, path, old, new, tool = st["edits"][-1]
    reveal = int((now - ts) * 170)
    typing = reveal < len(new)
    typed = new[:reveal]
    old_lines = [ln for ln in old.splitlines() if ln.strip()][:3]
    new_lines = typed.splitlines() or [""]
    budget = inner_h - len(old_lines)
    if len(new_lines) > budget:
        old_lines, budget = [], inner_h
    tail = new_lines[-budget:]
    first = max(1, len(new_lines) - len(tail) + 1)
    cursor = "▌" if (typing and frame % 2) else " "
    g = Group(
        *[Text.assemble(("   - ", RED), (ln, f"{mix(RED, FAINT, 0.35)} strike"), no_wrap=True, overflow="crop")
          for ln in old_lines],
        Syntax("\n".join(tail) + cursor, Syntax.guess_lexer(path, new), theme="monokai",
               background_color="default", word_wrap=False, line_numbers=True, start_line=first,
               highlight_lines={first + len(tail) - 1} if typing else None),
    )
    plus = len(new.splitlines())
    minus = len(old.splitlines()) if old else 0
    title = Text.assemble((" ✎ ", f"bold {ORANGE}"), (os.path.basename(path), f"bold {FG}"), ("  ", ""),
                          (f"+{plus}", GREEN), (" ", ""), (f"−{minus}", RED), (" ", ""))
    applied_age = now - ts - len(new) / 170
    sub = Text(f" {tool} · typing… " if typing else f" ✓ {tool} · applied ",
               style=ORANGE if typing else (f"bold {GREEN}" if applied_age < 1.5 else DIM))
    border = mix(ORANGE, "#fff1e6", pulse(frame)) if typing else \
        ((WHITE if blink(frame, 2) else GREEN) if applied_age < 1.5 else FAINT)
    return Panel(g, title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 box=box.SQUARE, border_style=border, padding=(0, 1), height=height)


def log_icon(tag, text, kind):
    if kind == "adv":
        return "◆"
    if kind == "err":
        return "✕"
    if kind == "gate":
        return "⚑"
    if kind == "user":
        return "›"
    if kind == "sub":
        return "✓" if text.startswith("done") else "↳"
    if text.startswith(("Edit", "Write", "MultiEdit")):
        return "✎"
    if text.startswith("Bash"):
        return "$"
    return "·"


def log_panel(st, height, now=0, gates=None, launch=None):
    log = list(st["log"])
    for name, (state, ts) in (gates or {}).items():
        if state == "passed" and ts:
            log.append((ts, "check", f"advisor called {GATE_WORDS[name]}", "gate"))
        elif state == "missed" and ts:
            log.append((ts, "check", f"advisor not called {GATE_WORDS[name]}", "gate_miss"))
    log.sort(key=lambda x: x[0])
    # Replay: key events from the last ~10 s before launch flash one after another right at start,
    # so a recording begins with motion. Only the highlight timing changes, not the data.
    replay = {}
    if launch:
        prior = [e for e in log if launch - 10 <= e[0] < launch and
                 (e[3] in ("adv", "gate", "gate_miss", "err") or (e[3] == "sub" and e[2].startswith("done")))]
        for i, e in enumerate(prior):
            replay[id(e)] = launch + 0.3 + i * 0.7
    g = Table.grid(padding=(0, 1), expand=True)
    g.add_column(style=DIM, no_wrap=True, width=8)
    g.add_column(no_wrap=True, width=1)
    g.add_column(no_wrap=True, width=7)
    g.add_column(style=FG, no_wrap=True, overflow="ellipsis", ratio=1)
    for entry in log[-(height - 2):]:
        ts, tag, text, kind = entry
        age = now - replay.get(id(entry), ts)
        if age < 0:
            age = 99
        col = KIND_COLOR.get(kind, FG)
        icon = log_icon(tag, text, "gate" if kind == "gate_miss" else kind)
        key = kind in ("adv", "gate", "gate_miss", "err") or (kind == "sub" and text.startswith("done"))
        base = col if key else FG
        if key and age < 1.6:
            inv = f"bold {BG} on {col}" if blink(int(now * FPS), 2) else f"bold {BG} on {WHITE}"
            g.add_row(Text(dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), style=inv), Text(icon, style=inv),
                      Text(tag[:7], style=inv), Text(text, style=inv))
            continue
        g.add_row(dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                  Text(icon, style=f"bold {fade(col, age, 2)}"),
                  Text(tag[:7], style=f"bold {fade(col, age, 2)}"),
                  Text(text, style=f"bold {fade(base, age, 2)}" if (age < 2 or key) else base))
    if not log:
        g.add_row("--:--:--", Text("·", style=DIM), Text("waiting", style=DIM), Text("for the first hook event…", style=DIM))
    return Panel(g, title=Text(" session log ", style=FG), title_align="left",
                 box=box.SQUARE, border_style=FAINT, padding=(0, 1), height=height)


def footer(adv, st, a, now=0, width=104):
    running = sum(1 for s in st["subagents"].values() if s["status"] == "running")
    total = len(st["sub_order"])
    t = Text()
    t.append("swarm ", style=DIM)
    t.append("[working]" if running else "[standby]", style=f"bold {BLUE}" if running else BLUE)
    t.append("    subagents ", style=DIM)
    t.append(f"[{running}/{total}]", style=BLUE)
    t.append(f"    {adv or 'advisor'} ", style=DIM)
    t.append(f"[on call · {a['calls']}]" if adv else "[off]", style=PURPLE)
    t.append("    tools ", style=DIM)
    t.append(f"[{st['tool_total'] + st['sub_tool_total']}]", style=GREEN)
    n = min(30, width - len(t.plain) - 14)
    if n < 6:
        return t
    buckets = [0] * n
    span = 60
    stamps = [m[0] for m in st["lead_marks"]] + [x for s in st["subagents"].values() for x in s["marks"]]
    for ts in stamps:
        b = int((ts - (now - span)) / span * n)
        if 0 <= b < n:
            buckets[b] += 1
    t.append("    activity ", style=DIM)
    line = sparkline(buckets, n)
    for i, ch in enumerate(line):
        t.append("▁" if ch == " " else ch, style=FAINT if ch == " " else mix(BLUE, ORANGE, i / max(1, n - 1)))
    return t


GAP = 4


def compute_gates(st, a_times, now):
    """Advisor gates inferred from events: idle / waiting / passed / missed, with the time it happened.
    (Observed, not enforced — enforcement comes with the P0 hooks.)"""
    edits = [e[0] for e in st["edits"]] or [m[0] for m in st["lead_marks"] if m[1] in ("Edit", "Write", "MultiEdit")]
    first_edit, last_edit = (edits[0], edits[-1]) if edits else (None, None)
    adv = sorted(a_times)
    g = {}
    before = [t for t in adv if first_edit is None or t < first_edit]
    if before:
        g["PLAN"] = ("passed", before[0])
    elif first_edit:
        g["PLAN"] = ("missed", first_edit)
    else:
        g["PLAN"] = ("idle", None)
    rep_ = st["repeat_error"]
    if rep_:
        after = [t for t in adv if t >= rep_[0]]
        g["ERR×2"] = ("passed", after[0]) if after else ("waiting", rep_[0])
    else:
        g["ERR×2"] = ("idle", None)
    if last_edit:
        after = [t for t in adv if t > last_edit]
        stopped = st["work_spans"] and st["work_spans"][-1][1] and st["work_spans"][-1][1] > last_edit
        g["DONE"] = ("passed", after[0]) if after else (("missed", st["work_spans"][-1][1]) if stopped else ("waiting", last_edit))
    else:
        g["DONE"] = ("idle", None)
    return g


GATE_TEXT = {"PLAN": "before a plan", "ERR×2": "same error x2", "DONE": "before done"}
GATE_WORDS = {"PLAN": "before the plan", "ERR×2": "after error x2", "DONE": "before done"}


def gate_mark(state, ts, now, frame):
    """Icon + style for a gate: grey ◇ idle, pulsing ◆ waiting / just passed, ✓ passed, ✕ missed."""
    age = now - ts if ts else 99
    if state == "waiting":
        return "◆", (f"bold {WHITE}" if blink(frame, 3) else f"bold {PURPLE}")
    if state == "passed":
        if age < 1.6:
            return "✓", (f"bold {BG} on {PURPLE}" if blink(frame, 2) else f"bold {BG} on {WHITE}")
        if age < 3:
            return "✓", f"bold {WHITE}"
        return "✓", f"bold {PURPLE}"
    if state == "missed":
        return "✕", mix(RED, FAINT, 0.3)
    return "◇", FAINT


def beams(h, rows, frame, active):
    """Dotted arrows from the advisor column to the right side; particles run when advisor speaks."""
    out = []
    for r in range(h):
        t = Text()
        if r in rows:
            if active:
                p = frame % (GAP + 2)
                for x in range(GAP - 1):
                    if x == p:
                        t.append("●", style=mix(PURPLE, WHITE, 0.4))
                    elif x == p - 1:
                        t.append("•", style=PURPLE)
                    else:
                        t.append("·", style=mix(PURPLE, FAINT, 0.5))
                t.append("▸", style=f"bold {PURPLE}")
            else:
                t.append("┈" * (GAP - 1), style=FAINT)
                t.append("▸", style=mix(PURPLE, FAINT, 0.5))
        else:
            t.append(" " * GAP)
        out.append(t)
    return Text("\n").join(out)


def render(console, lead, adv, st, a, frame, now, adv_ts, window, rnd):
    width = console.size.width
    right_w = max(44, width - LEFT_W - GAP)
    adv_flash = bool(adv_ts and now - adv_ts < 4)
    gates = compute_gates(st, a["times"], now)
    sub_rows, any_running, recent_done = subagent_block(st, frame, right_w, now)
    right = Group(
        lead_panel(lead, st, frame, adv_flash, now),
        vline(right_w, frame, st["working"] and not any_running, ORANGE),
        tools_panel(st),
        Align.center(Text.assemble(("delegate to subagents · ", FG), ("haiku 5.5", f"bold {BLUE}"))),
        *sub_rows,
        back_panel(lead, st),
    )
    h = len(console.render_lines(right, console.options.update(width=right_w), pad=False))
    body = Table.grid(padding=0)
    body.add_column(width=LEFT_W)
    body.add_column(width=GAP)
    body.add_column(width=right_w)
    body.add_row(advisor_panel(adv, a, st, h, frame, now, adv_ts, rnd, gates),
                 beams(h, {2, h - 2}, frame, bool(adv_ts and now - adv_ts < 3)), right)

    bottom_h = 10
    bottom = Table.grid(padding=0, expand=True)
    bottom.add_column(ratio=3)
    bottom.add_column(width=1)
    bottom.add_column(ratio=2)
    bottom.add_row(code_panel(st, now, bottom_h, frame), Text(""), log_panel(st, bottom_h, now, gates))
    return Group(header(lead, adv, frame, width), body, timeline_panel(st, a["times"], width, now, window, frame),
                 bottom, footer(adv, st, a, now, width))



# --- table layout (default): readable first, animation only marks change ------------

def project_name(title):
    if title and " — " in title:
        return title.split(" — ")[0]
    return os.path.basename(os.getcwd())


def top_bar(project, st, now, width, frame):
    t = Text(no_wrap=True, overflow="crop")
    t.append("◉ ", style=f"bold {mix(GREEN, WHITE, pulse(frame, 0.3) * 0.5) if st['working'] else DIM}")
    # project name and "swarmscope" are already in the window title — no repeat here
    elapsed = max(0, now - st["started"])
    t.append(f"turn {st['turns']}  ·  {int(elapsed // 60):02d}:{int(elapsed % 60):02d}", style=DIM)
    live = "● working" if st["working"] else "○ idle"
    t.append(" " * max(1, width - len(t.plain) - len(live)))
    t.append(live, style=ORANGE if st["working"] else DIM)
    return t


def activity(marks, now, window, n):
    b = [0] * n
    for ts in marks:
        i = int((ts - (now - window)) / window * n)
        if 0 <= i < n:
            b[i] += 1
    return b


def spark_text(buckets, color, now_frame):
    chars = "▁▂▃▄▅▆▇█"
    mx = max(buckets) if max(buckets) > 0 else 1
    t = Text(no_wrap=True)
    for i, v in enumerate(buckets):
        if v == 0:
            t.append("·", style=FAINT)
        else:
            t.append(chars[min(7, int(v / mx * 7.99))], style=color if i < len(buckets) - 2 else mix(color, WHITE, 0.4))
    return t


def agents_panel(lead, adv, st, a, gates, frame, now, window, width, sub_model=None, transcript=None):
    tbl = Table(box=None, padding=(0, 1), expand=True, show_edge=False, header_style=f"{DIM}")
    tbl.add_column("AGENT", no_wrap=True, width=7)
    tbl.add_column("STATE", no_wrap=True, width=10)
    tbl.add_column("ACTIVITY", no_wrap=True, width=12)
    tbl.add_column("CALLS", no_wrap=True, justify="right", width=5)
    tbl.add_column("TIME", no_wrap=True, justify="right", width=4)
    tbl.add_column("NOW", no_wrap=True, overflow="ellipsis", ratio=1)

    def row(name, color, model, state, state_style, marks, calls, tsec, now_txt, now_style, fresh_age):
        name_style = f"bold {fade(color, fresh_age, 1.2)}" if fresh_age < 1.2 else f"bold {color}"
        tbl.add_row(Text(name, style=name_style), Text(state, style=state_style),
                    spark_text(activity(marks, now, window, 12), color, frame),
                    Text(str(calls), style=FG), Text(tsec, style=DIM), Text(now_txt, style=now_style))

    # sonnet
    lm = [m[0] for m in st["lead_marks"]]
    err_age = now - st["err_marks"][-1] if st["err_marks"] else 99
    if err_age < 2:
        sstate, sstyle = "✕ error", f"bold {RED}" if blink(frame, 2) else RED
    elif st["working"]:
        sstate, sstyle = f"{SPIN[frame % 4]} working", ORANGE
    else:
        sstate, sstyle = "○ idle", DIM
    span = st["work_spans"][-1] if st["work_spans"] else None
    tsec = f"{(span[1] or now) - span[0]:.0f}s" if span else "—"
    row("sonnet", ORANGE, lead, sstate, sstyle, lm, st["tool_total"], tsec,
        st["last_tool"] or "—", FG, now - lm[-1] if lm else 99)

    # opus
    adv_age = now - a["times"][-1] if a["times"] else 99
    pending = a.get("pending")
    waiting = any(v[0] == "waiting" for v in gates.values())
    if not adv:
        ostate, ostyle = "off", DIM
    elif pending or adv_age < 2.5:
        ostate, ostyle = "◆ advising", (f"bold {WHITE}" if blink(frame, 2) else f"bold {PURPLE}")
    elif waiting:
        ostate, ostyle = "◇ pending", (f"bold {PURPLE}" if blink(frame, 4) else PURPLE)
    else:
        ostate, ostyle = "on call", PURPLE
    pend = [n for n, v in gates.items() if v[0] == "waiting"]
    durs = a.get("durations") or []
    if pending:
        otxt, osty = f"thinking {now - a['times'][-1]:.0f}s…", PURPLE
    elif a.get("redacted"):
        otxt, osty = f"{a['calls']} consults · advice encrypted, Ctrl+O in Claude Code", PURPLE
    elif pend and adv_age >= 2.5:
        otxt, osty = "needed: " + {"PLAN": "before plan", "ERR×2": "after error x2", "DONE": "before done"}[pend[0]], PURPLE
    elif a["last"]:
        otxt, osty = "» " + a["last"], PURPLE
    else:
        otxt, osty = "silent on routine turns", DIM
    row("opus", PURPLE, adv or "—", ostate, ostyle, a["times"], a["calls"],
        f"{sum(durs) / len(durs):.0f}s" if durs else "—", otxt, osty, adv_age)

    # haiku swarm
    for k in st["sub_order"][-5:]:
        sa = st["subagents"][k]
        run = sa["status"] == "running"
        done_age = now - sa["end"] if sa["end"] else 99
        if run:
            state, sty = f"{SPIN[frame % 4]} running", BLUE
        else:
            state, sty = "✓ done", (f"bold {WHITE}" if done_age < 1.5 and blink(frame, 2) else GREEN)
        el = (now if run else sa["end"]) - sa["start"]
        now_txt = sa["desc"] or sa["last"] or "—"
        if sa.get("errors"):
            now_txt = f"✕{sa['errors']} · " + now_txt
        row(sa["type"], BLUE, "", state, sty, sa["marks"], sa["tools"], f"{el:.0f}s",
            now_txt, (RED if sa.get("errors") else FG) if run else (mix(RED, DIM, 0.4) if sa.get("errors") else DIM),
            now - sa["marks"][-1] if sa["marks"] else now - sa["start"])
    if not st["sub_order"]:
        tbl.add_row(Text("swarm", style=DIM), Text("standby", style=DIM),
                    Text(""), Text(""), Text(""), Text("no subagents yet", style=DIM))
    # totals
    tbl.add_section()
    all_marks = lm + [x for sa in st["subagents"].values() for x in sa["marks"]]
    nerr = len(st["err_marks"]) + st.get("sub_errors", 0)
    elapsed = max(0, now - st["started"])
    tbl.add_row(Text("total", style=f"bold {FG}"),
                Text(f"{nerr} error{'s' if nerr != 1 else ''}", style=RED if nerr else DIM),
                spark_text(activity(all_marks, now, window, 12), GREEN, frame),
                Text(str(st["tool_total"] + st["sub_tool_total"]), style=f"bold {FG}"),
                Text(f"{elapsed:.0f}s", style=DIM),
                Text(f"{len(st['sub_order'])} sub · {a['calls']} advice · {len(st['edits'])} edits", style=DIM))
    running = sum(1 for x in st["subagents"].values() if x["status"] == "running")
    models = sorted({m for m in (sub_model or pretty_model(subagent_model(transcript, k)) for k in st["sub_order"]) if m})
    swarm = " / ".join(models) if models else ("subagents" if st["sub_order"] else "no subagents")
    title = Text.assemble((" agents ", FG), (lead, ORANGE), (" · ", DIM), (adv or "no advisor", PURPLE),
                          (" · ", DIM), (swarm + " ", BLUE), (f"({running} running) ", BLUE if running else DIM))
    return Panel(tbl, title=title, title_align="left", box=box.SQUARE, border_style=FAINT, padding=(0, 1))


def gates_panel(adv, a, gates, frame, now, rnd):
    t = Text(no_wrap=True)
    for name in ("PLAN", "ERR×2", "DONE"):
        state, ts = gates[name]
        ic, sty = gate_mark(state, ts, now, frame)
        label = {"PLAN": "before plan", "ERR×2": "error x2", "DONE": "before done"}[name]
        t.append(f" {ic} ", style=sty)
        t.append(label, style=(sty if ic != "◇" else DIM))
        t.append("    ")
    adv_ts = a["times"][-1] if a["times"] else None
    prog = min(1.0, (now - adv_ts) / 1.4) if adv_ts else 1.0
    if a.get("redacted") or a.get("pending"):
        # real sessions: Claude Code keeps the advice encrypted in the transcript
        durs = a.get("durations") or []
        msg = ("consulting now…" if a.get("pending") else
               f"advice is encrypted in the transcript · read it in Claude Code (Ctrl+O)")
        line = Text.assemble((" » ", PURPLE), (msg, f"bold {PURPLE}"), no_wrap=True, overflow="ellipsis")
        after = (a.get("after") or ["—"])[-1]
        hist = Text.assemble(("   then sonnet: ", DIM), (after, FG), no_wrap=True, overflow="ellipsis")
    else:
        advice = a["last"] or ("not called yet" if adv else "advisor is off — run with --advisor opus")
        line = Text.assemble((" » ", PURPLE), (decrypt(advice, prog, rnd), f"bold {PURPLE}" if a["last"] else DIM),
                             no_wrap=True, overflow="ellipsis")
        earlier = a.get("all", [])[:-1]
        hist = Text.assemble(("   earlier: ", DIM), (" · ".join(earlier[-2:]) if earlier else "—", DIM),
                             no_wrap=True, overflow="ellipsis")
    fresh = adv_ts and now - adv_ts < 2.5
    border = (WHITE if blink(frame, 2) else PURPLE) if fresh else mix(PURPLE, FAINT, 0.45)
    durs = a.get("durations") or []
    title = Text.assemble((" advisor checkpoints ", PURPLE), (f"{adv or 'off'} ", DIM), (f"· {a['calls']} calls ", DIM),
                          (f"· avg {sum(durs) / len(durs):.0f}s " if durs else "", DIM))
    return Panel(Group(t, line, hist), title=title, title_align="left", box=box.SQUARE, border_style=border, padding=(0, 0))


def render_table(console, lead, adv, st, a, frame, now, window, rnd, project, launch=None,
                 sub_model=None, transcript=None):
    width = console.size.width
    height = console.size.height
    gates = compute_gates(st, a["times"], now)
    agents = agents_panel(lead, adv, st, a, gates, frame, now, window, width, sub_model, transcript)
    gp = gates_panel(adv, a, gates, frame, now, rnd)
    tl = timeline_panel(st, a["times"], width, now, window, frame, roomy=True)
    opts = console.options.update(width=width)
    used = 1 + 1 + sum(len(console.render_lines(x, opts, pad=False)) for x in (agents, gp, tl))
    rest = max(14, height - used - 1)
    log_h = max(6, int(rest * 0.52))
    code_h = max(8, rest - log_h)
    return Group(
        Text(""),                       # top line intentionally empty
        agents,
        gp,
        tl,
        code_panel(st, now, code_h, frame),
        log_panel(st, log_h, now, gates, launch),
        footer(adv, st, a, now, width),
    )

# --- intro -----------------------------------------------------------------------

FONT = {
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "W": ["█   █", "█   █", "█ █ █", "█ █ █", " █ █ "],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "P": ["████ ", "█   █", "████ ", "█    ", "█    "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
}


def intro_frame(console, p, rnd):
    word = "SWARMSCOPE"
    rows = ["".join(FONT[c][r] + " " for c in word).rstrip() for r in range(5)]
    w = len(rows[0])
    stops = [ORANGE, PURPLE, BLUE]
    out = []
    reveal = p * 1.6 * w
    for r in rows:
        t = Text()
        for x, ch in enumerate(r):
            g = x / max(1, w - 1)
            col = mix(stops[0], stops[1], g * 2) if g < 0.5 else mix(stops[1], stops[2], (g - 0.5) * 2)
            if x > reveal:
                t.append(" ")
            elif x > reveal - 6 and ch != " ":
                t.append(rnd.choice("░▒▓"), style=mix(col, WHITE, 0.5))
            else:
                t.append(ch, style=col)
        out.append(t)
    sub = "sonnet builds  ·  haiku swarms  ·  opus advises"
    k = int(max(0, p - 0.55) / 0.35 * len(sub))
    out.append(Text(""))
    out.append(Text(sub[:k], style=DIM))
    pad = max(0, (console.size.height - 8) // 2)
    return Group(*([Text("")] * pad), *[Align.center(t) for t in out])


# --- demo data ---------------------------------------------------------------------

DEMO_LOOP = 45
CONFTEST = '''import pytest
from auth.client import AuthClient


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    return "test-token"


@pytest.fixture
def client(token):
    return AuthClient(base_url="http://localhost:8080", token=token)
'''
GET_OLD = '''    def get(self, path):
        resp = self.session.get(self.base_url + path)
        resp.raise_for_status()
        return resp.json()'''
GET_NEW = '''    def get(self, path, retries=3, backoff=0.5):
        for attempt in range(retries):
            try:
                resp = self.session.get(self.base_url + path, timeout=5)
                resp.raise_for_status()
                return resp.json()
            except (ConnectionError, Timeout) as exc:
                if attempt == retries - 1:
                    raise AuthError(f"GET {path} failed") from exc
                time.sleep(backoff * 2 ** attempt)'''
IMPORTS_NEW = '''import time

import requests
from requests.exceptions import ConnectionError, Timeout

from .errors import AuthError'''


def demo_events(t0, now):
    el = (now - t0) % DEMO_LOOP
    base = now - el
    rnd = random.Random(7)
    S = [(0.0, {"event": "UserPromptSubmit", "prompt": "fix flaky auth tests and add retry to the client"})]
    for i, (tool, arg) in enumerate([("Read", "src/auth/client.py"), ("Grep", "retry"), ("Read", "tests/test_auth.py")]):
        S.append((1 + i * 0.7, {"event": "PreToolUse", "tool_name": tool, "arg": arg}))
    for j, (typ, desc, start, dur, calls) in enumerate([
            ("search", "maps the repo", 4, 9, 7), ("grep", "finds call sites", 5, 14, 12), ("audit", "runs in a worktree", 6, 24, 20)]):
        S.append((start - 0.1, {"event": "PreToolUse", "tool_name": "Agent", "subagent_type": typ, "description": desc}))
        S.append((start, {"event": "SubagentStart", "agent_id": f"a{j}", "agent_type": typ}))
        for k in range(calls):
            S.append((start + (k + 1) * dur / (calls + 1),
                      {"event": "PreToolUse", "tool_name": rnd.choice(["Read", "Grep", "Glob", "Bash"]), "agent_id": f"a{j}", "arg": "…"}))
        S.append((start + dur, {"event": "SubagentStop", "agent_id": f"a{j}", "agent_type": typ}))
    S += [
        (13, {"event": "PreToolUse", "tool_name": "Bash", "arg": "pytest tests/test_auth.py"}),
        (13.6, {"event": "PostToolUse", "tool_name": "Bash", "err": "fixture 'token' not found"}),
        (15, {"event": "PreToolUse", "tool_name": "Bash", "arg": "pytest tests/test_auth.py"}),
        (15.6, {"event": "PostToolUse", "tool_name": "Bash", "err": "fixture 'token' not found"}),
        (20, {"event": "PreToolUse", "tool_name": "Write", "arg": "tests/conftest.py", "old": "", "new": CONFTEST}),
        (23.5, {"event": "PreToolUse", "tool_name": "Read", "arg": "src/auth/client.py"}),
        (25, {"event": "PreToolUse", "tool_name": "Edit", "arg": "src/auth/client.py", "old": GET_OLD, "new": GET_NEW}),
        (31, {"event": "PreToolUse", "tool_name": "Edit", "arg": "src/auth/client.py", "old": "import requests", "new": IMPORTS_NEW}),
        (34, {"event": "PreToolUse", "tool_name": "Bash", "arg": "pytest tests/test_auth.py -q"}),
        (36, {"event": "PreToolUse", "tool_name": "Bash", "arg": "ruff check src/auth"}),
        (37, {"event": "PreToolUse", "tool_name": "Read", "arg": "src/auth/client.py"}),
        (42, {"event": "Stop"}),
    ]
    out = [dict(e, ts=base + off) for off, e in sorted(S, key=lambda x: x[0]) if off <= el]
    times, allv = [], []
    if el >= 17:
        times.append(base + 17)
        allv.append("wrong fixture, not the code")
    if el >= 39:
        times.append(base + 39)
        allv.append("retry only idempotent GETs. ship it")
    return out, {"calls": len(times), "last": allv[-1] if allv else "", "times": times, "all": allv}


# --- main --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--intro", action="store_true", help="show the start-up logo")
    ap.add_argument("--start", type=float, default=19.5,
                    help="demo: second of the scripted session to start from (default 19.5 = code is being written)")
    ap.add_argument("--window", type=float, default=None, help="timeline window in seconds (default 45 demo / 120 live)")
    ap.add_argument("--title", default=os.environ.get("SWARMSCOPE_TITLE"),
                    help="window/tab title, default: 'swarmscope — <current folder>'")
    ap.add_argument("--layout", choices=["table", "classic"], default="table",
                    help="table = agents table (default), classic = the original flowchart look")
    ap.add_argument("--snapshot", type=float, help="render once at t seconds (demo) and exit")
    args = ap.parse_args()
    console = Console()
    window = args.window or (DEMO_LOOP if args.demo else 120)
    t0 = time.time() - (args.start if args.demo else 0)
    rnd = random.Random()

    title = args.title or ("swarmscope" if args.demo else f"swarmscope — {os.path.basename(os.getcwd())}")
    if title and args.snapshot is None:
        # OSC 0 sets both window and tab title (Terminal.app, iTerm2, Ghostty, tmux with set-titles)
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()

    cache = {"key": None, "st": None}
    launch = {"t": time.time()}
    adv_seen = {"calls": None, "ts": None}

    def snapshot_data(now):
        if args.demo:
            events, a = demo_events(t0, now)
            return "sonnet 5.5", "opus 5.5", build_state(events), a
        sid = args.session or latest_session_id()
        key = None
        if sid:
            try:
                key = (sid, session_file(sid).stat().st_size)
            except OSError:
                key = (sid, 0)
        if key != cache["key"] or cache["st"] is None:
            cache["key"], cache["st"] = key, build_state(load_events(sid)) if sid else build_state([])
        st = cache["st"]
        meta = load_meta(sid) if sid else {}
        cache["tp"] = meta.get("transcript_path") or st["transcript"]
        adv = advisor_name()
        a = advisor_activity(meta.get("transcript_path") or st["transcript"]) if adv else {"calls": 0, "last": "", "times": []}
        return meta.get("model") or "lead", adv, st, a

    def frame_renderable(frame, now):
        lead, adv, st, a = snapshot_data(now)
        if adv_seen["calls"] is None:
            adv_seen["calls"] = a["calls"]
            adv_seen["ts"] = a["times"][-1] if (args.snapshot is not None and a["times"]) else None
        elif a["calls"] > adv_seen["calls"]:
            adv_seen["ts"] = now
        if a["calls"] < (adv_seen["calls"] or 0):   # demo loop restarted
            adv_seen["ts"] = None
        adv_seen["calls"] = a["calls"]
        if args.layout == "table":
            return render_table(console, lead, adv, st, a, frame, now, window, rnd, project_name(args.title),
                                launch["t"] if args.demo else None,
                                "haiku 5.5" if args.demo else None, cache.get("tp"))
        return render(console, lead, adv, st, a, frame, now, adv_seen["ts"], window, rnd)

    if args.snapshot is not None:
        console.print(frame_renderable(3, t0 + args.snapshot))
        return

    with Live(console=console, screen=True, auto_refresh=False) as live:
        try:
            if args.intro:
                start = time.time()
                while (p := (time.time() - start) / 1.8) < 1.15:
                    live.update(intro_frame(console, min(1, p), rnd), refresh=True)
                    time.sleep(1 / 30)
                t0 = time.time() - (args.start if args.demo else 0)
            launch["t"] = time.time()
            frame = 0
            while True:
                live.update(frame_renderable(frame, time.time()), refresh=True)
                frame += 1
                time.sleep(1 / FPS)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
