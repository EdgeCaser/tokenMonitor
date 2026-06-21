import shutil
from pathlib import Path

import pytest

from tokmon import analytics as A
from tokmon import config as cfg_mod
from tokmon import db, ingest


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.jsonl"


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    projects_dir = home / ".claude" / "projects"
    proj_dir = projects_dir / "-tmp-test-proj"
    proj_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, proj_dir / "test-session-001.jsonl")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cfg_mod, "DEFAULT_PROJECTS_DIR", projects_dir)
    ingest.incremental(roots=[(projects_dir, "local")])
    return A.connect_with_views()


def test_top_turns_orders_by_cost(loaded):
    rows = A.top_turns(loaded, metric="cost", n=10)
    assert len(rows) >= 1
    # Sonnet turn (a1) should outrank Haiku (a2) at these token volumes
    assert rows[0][4] == "claude-sonnet-4-6"


def test_tool_rollup(loaded):
    rows = A.spend_by(loaded, "tool")
    assert any(r[0] == "Bash" for r in rows)


def test_cache_efficiency_includes_models(loaded):
    rows = A.cache_efficiency(loaded)
    models = {r[0] for r in rows}
    assert "claude-sonnet-4-6" in models
    assert "claude-haiku-4-5-20251001" in models


def test_parse_since():
    assert A.parse_since("all") is None
    assert A.parse_since(None) is None
    assert A.parse_since("7d") is not None
    assert A.parse_since("24h") is not None
    with pytest.raises(ValueError):
        A.parse_since("garbage")


# --- quota inference --------------------------------------------------------

def test_sliding_window_peak():
    # Four unit hits spaced one hour apart; a 2h-wide window holds at most 3
    # (the boundary point exactly 2h back is still inside since eviction is >W).
    times = [0, 3600, 7200, 10800]
    values = [1, 1, 1, 1]
    assert A._sliding_window_peak(times, values, 7200) == 3
    # A window narrower than the spacing holds a single hit.
    assert A._sliding_window_peak(times, values, 1800) == 1


def test_fixed_blocks_partition():
    # Two bursts separated by more than the window → two reset-anchored blocks.
    w = 5 * 3600
    times = [0, 60, 120, w + 10, w + 70]
    values = [1, 1, 1, 1, 1]
    blocks = A._fixed_blocks(times, values, w)
    assert len(blocks) == 2
    assert blocks[0]["n_turns"] == 3 and blocks[0]["total"] == 3
    assert blocks[1]["n_turns"] == 2 and blocks[1]["total"] == 2
    # The first block knows when activity resumed.
    assert blocks[0]["next_turn"] == w + 10


def test_ceiling_detected_when_peaks_cluster_and_resume_at_reset():
    """Back-to-back 5h blocks, each pushing usage to ~9 in the window's second
    half and resuming right at the next reset: the classic 'hit the wall, wait
    for refresh' pattern → a real ceiling with wall events."""
    w = 5 * 3600
    times, values = [], []
    for day in range(6):
        base = day * w  # blocks back-to-back so each resume == previous reset
        for k in range(9):
            times.append(base + int(w * 0.5) + k * 120)  # late in the window
            values.append(1.0)
    blocks = A._fixed_blocks(times, values, w)
    res = A._ceiling_from_blocks(blocks, w)
    assert res["confidence"] in ("high", "medium")
    assert res["ceiling_estimate"] is not None
    assert res["n_wall_events"] >= 2
    assert 8.0 <= res["ceiling_estimate"] <= 10.0


def test_no_ceiling_when_usage_is_scattered():
    """Wildly varying block sizes with no resume-at-reset → lower bound only."""
    w = 5 * 3600
    times, values = [], []
    sizes = [1, 7, 2, 20, 3, 1, 9]   # no clustering near the peak (20)
    cursor = 0
    for s in sizes:
        for k in range(s):
            times.append(cursor + k * 60)
            values.append(1.0)
        cursor += 3 * w  # long idle gap so nothing looks like a reset-resume
    blocks = A._fixed_blocks(times, values, w)
    res = A._ceiling_from_blocks(blocks, w)
    assert res["confidence"] == "none"
    assert res["ceiling_estimate"] is None
    assert res["lower_bound"] == 20.0   # peak is still a valid lower bound


def test_quota_inference_end_to_end(loaded):
    q = A.quota_inference(loaded, metric="usd")
    assert q["metric"] == "usd"
    assert q["data_range"]["n_turns"] >= 1
    assert "5h" in q["windows"] and "weekly" in q["windows"]
    assert q["windows"]["5h"]["lower_bound"] > 0
    # session pseudo-window is present and explicitly flagged as non-enforced
    assert "session" in q["windows"]
    assert q["windows"]["session"]["confidence"] == "none"
    # token metric also works end-to-end
    qt = A.quota_inference(loaded, metric="tokens")
    assert qt["windows"]["5h"]["lower_bound"] > 0
    with pytest.raises(ValueError):
        A.quota_inference(loaded, metric="bogus")
