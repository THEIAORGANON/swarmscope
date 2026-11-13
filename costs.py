#!/usr/bin/env python3
"""Per-model token and cost accounting from Claude Code transcripts.

Where the numbers come from
  * main session  : ~/.claude/projects/<project>/<session>.jsonl, records with type == "assistant"
  * subagents     : <session>/subagents/agent-<id>.jsonl (same format, their own model)
  * advisor (Opus): runs server-side inside the main model's request. The Claude API reports its tokens
                    only in message.usage.iterations[] as {"type": "advisor_message", "model": ...};
                    top-level usage does not include them.

One API message is written as several transcript lines (one per content block) that share message.id,
and api-request-blob records repeat content, so we count each message.id once (the copy with the
largest output_tokens wins).

Prices are list prices in USD per million tokens (checked 2026-11-13); override them with
$SWARMSCOPE_DIR/prices.json, using the same shape as PRICES. Every dollar figure here is an estimate:
the session's real bill (Claude Code's cost.total_cost_usd) can differ because of plan, batch, fast
mode and rounding. Anything reconstructed instead of read is flagged "estimated".

CLI (probe):  python costs.py [transcript.jsonl | project dir | --latest]
"""
import glob
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BASE, pretty_model  # noqa: E402

# in / out / cache read / cache write 5m / cache write 1h, USD per 1M tokens
PRICES = {
    "opus-5-5":   {"in": 4.00, "out": 20.00, "cr": 0.20, "cw5": 5.00,  "cw1h": 8.00},
    "sonnet-5-5": {"in": 2.00, "out": 10.00, "cr": 0.10, "cw5": 2.50,  "cw1h": 4.00},
    # Haiku 5.5 is tiered: prompts over 100k tokens cost 5x
    "haiku-5-5":  {"in": 0.10, "out": 0.50,  "cr": 0.01, "cw5": 0.125, "cw1h": 0.20,
                   "long": {"over": 100_000, "in": 0.50, "out": 2.50, "cr": 0.05, "cw5": 0.625, "cw1h": 1.00}},
    "opus-5":     {"in": 5.00, "out": 25.00, "cr": 0.50, "cw5": 6.25,  "cw1h": 10.00},
    "sonnet-5":   {"in": 2.00, "out": 10.00, "cr": 0.20, "cw5": 2.50,  "cw1h": 4.00},
    "haiku-4-5":  {"in": 1.00, "out": 5.00,  "cr": 0.10, "cw5": 1.25,  "cw1h": 2.00},
    "fable-5-2":  {"in": 10.00, "out": 50.00, "cr": 0.25, "cw5": 12.50, "cw1h": 20.00},
}
PRICES_AS_OF = "2026-11-13"
# fast mode (usage.speed == "fast"): Opus 5.5 is listed at $8/$40, i.e. 2x. For other models we assume
# the same 2x on every bucket and flag the figure as estimated.
FAST_MULT = {"opus-5-5": 2.0}
FAST_DEFAULT = 2.0


def _load_prices():
    p = dict(PRICES)
    try:
        p.update(json.loads((BASE / "prices.json").read_text()))
    except Exception:
        pass
    return p


_PRICES = _load_prices()


def price_key(model):
    """claude-opus-5-5 / claude-opus-5-5-20260922 / opus → 'opus-5-5' (None if unknown)."""
    if not model:
        return None
    m = model.lower().replace("claude-", "").replace(".", "-")
    m = re.sub(r"-\d{8}$", "", m)
    m = re.sub(r"\[.*\]$", "", m)            # "opus-5-5[1m]"
    if m in _PRICES:
        return m
    for k in sorted(_PRICES, key=len, reverse=True):
        if m.startswith(k):
            return k
    return None


def _zero():
    return {"in": 0, "out": 0, "cr": 0, "cw5": 0, "cw1h": 0, "usd": 0.0, "msgs": 0, "unpriced": 0,
            "fast": 0, "fast_guess": 0}


def _tokens(u):
    """Normalise one usage dict (top-level or one iteration) → token buckets."""
    cc = u.get("cache_creation") or {}
    cw5 = cc.get("ephemeral_5m_input_tokens")
    cw1h = cc.get("ephemeral_1h_input_tokens")
    total_cw = u.get("cache_creation_input_tokens") or 0
    if cw5 is None and cw1h is None:
        cw5, cw1h = total_cw, 0
    return {"in": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
            "cr": u.get("cache_read_input_tokens") or 0, "cw5": cw5 or 0, "cw1h": cw1h or 0,
            "fast": u.get("speed") == "fast"}


def _usd(model, t):
    k = price_key(model)
    if not k:
        return None
    p = _PRICES[k]
    prompt = t["in"] + t["cr"] + t["cw5"] + t["cw1h"]
    if "long" in p and prompt > p["long"]["over"]:
        p = p["long"]
    mult = FAST_MULT.get(k, FAST_DEFAULT) if t.get("fast") else 1.0
    return mult * (t["in"] * p["in"] + t["out"] * p["out"] + t["cr"] * p["cr"]
                   + t["cw5"] * p["cw5"] + t["cw1h"] * p["cw1h"]) / 1e6


def _add(bucket, model, t, count=1):
    for k in ("in", "out", "cr", "cw5", "cw1h"):
        bucket[k] += t[k]
    bucket["msgs"] += count
    if t.get("fast"):
        bucket["fast"] += 1
        if price_key(model) not in FAST_MULT:
            bucket["fast_guess"] += 1
    usd = _usd(model, t)
    if usd is None:
        bucket["unpriced"] += 1
    else:
        bucket["usd"] += usd


def _messages(path):
    """Unique assistant messages of one transcript: [(msg_id, model, usage, content_types, has_advisor_call)]."""
    best, order = {}, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage")
                model = msg.get("model")
                if not isinstance(u, dict) or not model or model == "<synthetic>":
                    continue
                mid = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                adv = any(isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "advisor"
                          for b in content)
                if mid not in best:
                    order.append(mid)
                    best[mid] = {"model": model, "usage": u, "adv": adv, "iters": bool(u.get("iterations"))}
                else:
                    cur = best[mid]
                    cur["adv"] = cur["adv"] or adv
                    # keep the most complete copy: prefer one with iterations, then the larger output
                    if (bool(u.get("iterations")), u.get("output_tokens") or 0) >= \
                       (cur["iters"], cur["usage"].get("output_tokens") or 0):
                        cur["usage"], cur["iters"] = u, bool(u.get("iterations"))
    except OSError:
        return []
    return [(m, best[m]["model"], best[m]["usage"], best[m]["adv"]) for m in order]


def _subagent_files(transcript_path):
    if not transcript_path or not transcript_path.endswith(".jsonl"):
        return {}
    out = {}
    for p in glob.glob(transcript_path[:-6] + "/subagents/agent-*.jsonl"):
        aid = os.path.basename(p)[len("agent-"):-len(".jsonl")]
        out[aid] = p
    return out


_cache = {}


def session_costs(transcript_path, advisor_model=None):
    """Token/cost breakdown for one session.

    Returns {
      "lead":    bucket + {"model"},          main model, executor iterations only
      "advisor": bucket + {"model", "source"}  source: "iterations" (read) | "estimate" | "none"
      "subs":    {agent_id: bucket + {"model"}},
      "total_usd", "estimated" (bool), "notes" [str], "seen": diagnostics
    }"""
    if not transcript_path:
        return None
    files = [transcript_path] + sorted(_subagent_files(transcript_path).values())
    try:
        key = tuple((p, os.path.getsize(p)) for p in files)
    except OSError:
        return None
    if _cache.get(transcript_path, (None,))[0] == key:
        return _cache[transcript_path][1]

    lead = dict(_zero(), model=None)
    adv = dict(_zero(), model=None, source="none")
    seen = {"messages": 0, "with_iterations": 0, "iteration_types": {}, "advisor_call_msgs": 0,
            "advisor_iterations": 0, "usage_keys": set()}
    notes = []
    adv_ctx = []                                 # context sizes at advisor calls without iteration data

    for mid, model, u, has_call in _messages(transcript_path):
        seen["messages"] += 1
        seen["usage_keys"].update(u.keys())
        lead["model"] = lead["model"] or model
        its = u.get("iterations")
        if has_call:
            seen["advisor_call_msgs"] += 1
        if isinstance(its, list) and its:
            seen["with_iterations"] += 1
            first = True
            for it in its:
                if "speed" not in it and "speed" in u:
                    it = dict(it, speed=u["speed"])
                typ = it.get("type", "?")
                seen["iteration_types"][typ] = seen["iteration_types"].get(typ, 0) + 1
                if typ == "advisor_message":
                    seen["advisor_iterations"] += 1
                    adv["model"] = adv["model"] or it.get("model")
                    _add(adv, it.get("model") or advisor_model, _tokens(it))
                else:
                    _add(lead, model, _tokens(it), 1 if first else 0)
                    first = False
        else:
            t = _tokens(u)
            _add(lead, model, t)
            if has_call:
                adv_ctx.append(t["in"] + t["cr"] + t["cw5"] + t["cw1h"])

    if seen["advisor_iterations"]:
        adv["source"] = "iterations"
    elif adv_ctx:
        # The transcript shows advisor calls but no advisor usage. Lower bound: the advisor reads roughly
        # the executor's context at the call; we price that as uncached input and add no output tokens.
        adv["source"] = "estimate"
        adv["model"] = advisor_model
        for ctx in adv_ctx:
            _add(adv, advisor_model or "opus-5-5", {"in": ctx, "out": 0, "cr": 0, "cw5": 0, "cw1h": 0})
        notes.append("advisor usage not in transcript: lower-bound estimate (input only)")

    subs = {}
    for aid, p in _subagent_files(transcript_path).items():
        b = dict(_zero(), model=None)
        for mid, model, u, _ in _messages(p):
            b["model"] = b["model"] or model
            its = u.get("iterations")
            for i, it in enumerate(its if isinstance(its, list) and its else [u]):
                _add(b, it.get("model") or model, _tokens(it), 1 if i == 0 else 0)
        if b["msgs"]:
            subs[aid] = b

    buckets = [lead, adv] + list(subs.values())
    nfast = sum(b["fast"] for b in buckets)
    fast_guess = sum(b["fast_guess"] for b in buckets)
    if nfast:
        notes.append(f"{nfast} fast-mode usage records priced at 2x" + (" (assumed for non-Opus models)" if fast_guess else ""))
    unpriced = lead["unpriced"] + adv["unpriced"] + sum(s["unpriced"] for s in subs.values())
    if unpriced:
        notes.append(f"{unpriced} messages from models without a price: not counted in $")
    total = lead["usd"] + adv["usd"] + sum(s["usd"] for s in subs.values())
    seen["usage_keys"] = sorted(seen["usage_keys"])
    res = {"lead": lead, "advisor": adv, "subs": subs, "total_usd": total,
           "estimated": adv["source"] == "estimate" or bool(unpriced) or bool(fast_guess), "notes": notes, "seen": seen,
           "prices_as_of": PRICES_AS_OF}
    _cache[transcript_path] = (key, res)
    return res


def fmt_usd(x, approx=False):
    if x is None:
        return "—"
    s = "<$0.001" if 0 < x < 0.001 else f"${x:.3f}" if x < 0.01 else f"${x:.2f}"
    return ("~" if approx else "") + s


def fmt_tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


# --- probe CLI ---------------------------------------------------------------------

def _find_transcript(arg):
    home = pathlib.Path.home() / ".claude" / "projects"
    if arg in (None, "--latest"):
        files = glob.glob(str(home / "*" / "*.jsonl"))
        return max(files, key=os.path.getmtime) if files else None
    p = pathlib.Path(os.path.expanduser(arg))
    if p.is_file():
        return str(p)
    if p.is_dir():
        # a project folder → its transcript dir (Claude Code replaces non-alphanumerics with '-')
        enc = re.sub(r"[^a-zA-Z0-9]", "-", str(p.resolve()))
        files = glob.glob(str(home / enc / "*.jsonl")) or glob.glob(str(p / "*.jsonl"))
        return max(files, key=os.path.getmtime) if files else None
    return None


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    tp = _find_transcript(arg)
    if not tp:
        print("transcript not found. usage: costs.py [transcript.jsonl | project dir | --latest]")
        sys.exit(1)
    from common import advisor_activity
    a = advisor_activity(tp)
    c = session_costs(tp, "claude-opus-5-5")
    s = c["seen"]
    print(f"transcript   {os.path.basename(tp)}")
    print(f"prices       list prices as of {c['prices_as_of']}, USD / 1M tokens")
    print()

    print(f"  {'':<8} {'model':<10} {'msg':>3}  {'in':>5} {'out':>5} {'c.read':>6} {'c.write':>7} {'usd':>8}")

    def line(name, b, approx=False):
        print(f"  {name:<8} {pretty_model(b['model']) or '?':<10} {b['msgs']:>3}  {fmt_tok(b['in']):>5} "
              f"{fmt_tok(b['out']):>5} {fmt_tok(b['cr']):>6} {fmt_tok(b['cw5'] + b['cw1h']):>7} {fmt_usd(b['usd'], approx):>8}")

    line("lead", c["lead"])
    line("advisor", c["advisor"], c["advisor"]["source"] == "estimate")
    for aid, b in c["subs"].items():
        line(aid[:8], b)
    print(f"  {'total':<8} {'':<10} {'':>3}  {'':>5} {'':>5} {'':>6} {'':>7} {fmt_usd(c['total_usd'], c['estimated']):>8}")
    print()
    print("diagnostics (paste this block back into the chat)")
    print(f"  unique assistant messages      {s['messages']}")
    print(f"  advisor calls                  {a['calls']} in {s['advisor_call_msgs']} messages")
    print(f"  messages with usage.iterations {s['with_iterations']}")
    print(f"  iteration types                {s['iteration_types'] or '—'}")
    print(f"  advisor usage source           {c['advisor']['source']}")
    print(f"  usage keys seen                {', '.join(s['usage_keys'])}")
    print(f"  subagent transcripts           {len(c['subs'])}")
    for n in c["notes"]:
        print(f"  note: {n}")
    # Claude Code's own number, if the statusline saved it for this session
    sid = pathlib.Path(tp).stem
    try:
        cc = json.loads((BASE / "meta" / f"{sid}.json").read_text()).get("cost")
        print(f"  claude code total_cost_usd     {fmt_usd(cc)}")
    except Exception:
        print("  claude code total_cost_usd     (no statusline meta for this session)")


if __name__ == "__main__":
    main()
