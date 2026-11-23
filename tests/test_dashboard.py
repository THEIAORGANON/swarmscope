"""Jev on the dashboard: questions and answers from $SWARMSCOPE_DIR/jev/<session>.jsonl."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytest.importorskip("rich")
sys.path.insert(0, str(REPO))
import dashboard  # noqa: E402


def _jev(tmp_path, recs, sid="S1"):
    f = tmp_path / "jev" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return tmp_path


def test_jev_data_pairs_questions_with_answers(tmp_path):
    base = _jev(tmp_path, [
        {"kind": "context", "ctx_id": "c1", "ts": 100.0, "jev": {"threshold": 0.5}},
        {"kind": "jev", "ctx_id": "c1", "ts": 100.4, "status": "ok", "p_consult": 0.71, "would_consult": True,
         "latency_ms": 400, "cost_usd_est": 0.00003},
        {"kind": "context", "ctx_id": "c2", "ts": 200.0, "jev": {"threshold": 0.5}},
        {"kind": "jev", "ctx_id": "c2", "ts": 200.3, "status": "ok", "p_consult": 0.22, "would_consult": False},
        {"kind": "jev", "ctx_id": "c2", "ts": 900.0, "status": "ok", "p_consult": 0.9, "replay": True},
        {"kind": "context", "ctx_id": "c3", "ts": 300.0, "jev": {"threshold": 0.5}},          # still asking
        {"kind": "context", "ctx_id": "c4", "ts": 400.0},
        {"kind": "jev", "ctx_id": "c4", "ts": 401.0, "status": "missing_result", "final": True}])
    jv = dashboard.jev_data("S1", base)
    assert [(j["asked"], j["ts"], j["p"], j["would"], j["status"]) for j in jv] == [
        (100.0, 100.4, 0.71, True, "ok"), (200.0, 200.3, 0.22, False, "ok"),
        (300.0, None, None, False, "pending"), (400.0, 401.0, None, False, "missing_result")]
    lines = [x[2] for x in dashboard.jev_entries(jv)]
    assert lines == ["p 0.71 → consult opus", "p 0.22 → no need, sonnet goes on", "asking: is opus worth it now?",
                     "no answer (missing_result)"]
    assert [dashboard.log_icon("jev", t, "jev") for t in lines[:3]] == ["●", "○", "◌"]


def test_no_jev_file_no_jev(tmp_path):
    assert dashboard.jev_data("nope", tmp_path) == []


def test_demo_snapshot_shows_jev():
    env = dict(os.environ, COLUMNS="104", LINES="58")
    out = subprocess.run([sys.executable, str(REPO / "dashboard.py"), "--demo", "--snapshot", "42"], env=env,
                         capture_output=True, text=True, timeout=60).stdout
    assert "jev      ○ no need" in out and "p 0.22 < 0.50" in out
    assert any(l.strip("│ ").startswith("jev") and "●" in l and "○" in l for l in out.splitlines())   # timeline lane
    assert "○ jev     p 0.22 → no need, sonnet goes on" in out
