import shutil
from pathlib import Path

import pytest

from tokmon import analytics as A
from tokmon import config as cfg_mod
from tokmon import db, ingest
from tokmon import pricing as pricing_mod


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


def test_billable_views_dedupe_duplicate_request_ids(loaded):
    before = A.summary(loaded)

    loaded.execute(
        """
        INSERT INTO turns
        SELECT 'a1_duplicate', parent_uuid, request_id, session_id,
               project_path, project_label, git_branch, model,
               ts + INTERVAL 1 SECOND, is_sidechain, input_tokens,
               output_tokens, cache_write_5m, cache_write_1h, cache_read,
               service_tier, stop_reason, has_thinking, thinking_chars,
               text_chars, web_search_requests, web_fetch_requests, raw_usage,
               source_file, source_offset + 1, host
        FROM turns WHERE uuid = 'a1'
        """
    )
    loaded.execute(
        """
        INSERT INTO turns
        SELECT 'a2_duplicate', parent_uuid, request_id, session_id,
               project_path, project_label, git_branch, model,
               ts + INTERVAL 1 SECOND, is_sidechain, input_tokens,
               output_tokens, cache_write_5m, cache_write_1h, cache_read,
               service_tier, stop_reason, has_thinking, thinking_chars,
               text_chars, web_search_requests, web_fetch_requests, raw_usage,
               source_file, source_offset + 1, host
        FROM turns WHERE uuid = 'a2'
        """
    )
    loaded.execute(
        """
        INSERT INTO tool_calls
        SELECT 'a2_duplicate', idx, tool_name, input_chars, input_preview
        FROM tool_calls WHERE turn_uuid = 'a2'
        """
    )

    assert loaded.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 5
    after = A.summary(loaded)
    assert after["turns"] == before["turns"]
    assert after["total_usd"] == pytest.approx(before["total_usd"])

    bash = [r for r in A.spend_by(loaded, "tool") if r[0] == "Bash"][0]
    assert bash[1] == 1  # calls
    assert bash[2] == 1  # turns_using


def test_views_join_pricing_by_turn_date(tmp_path, monkeypatch):
    home = tmp_path / "home"
    projects_dir = home / ".claude" / "projects"
    proj_dir = projects_dir / "-tmp-test-proj"
    proj_dir.mkdir(parents=True)
    fixture = proj_dir / "test-session-001.jsonl"
    shutil.copy(FIXTURE, fixture)

    pricing = tmp_path / "pricing.toml"
    pricing.write_text(
        """
[[prices]]
model = "claude-sonnet-4-6"
effective_from = "2026-01-01"
effective_to = "2026-06-21"
input = 30.0
output = 150.0

[[prices]]
model = "claude-sonnet-4-6"
effective_from = "2026-06-21"
input = 3.0
output = 15.0
"""
    )
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")
    monkeypatch.setattr(pricing_mod, "DEFAULT_PRICING_PATH", pricing)
    ingest.incremental(roots=[(projects_dir, "local")])

    conn = A.connect_with_views()
    row = conn.execute(
        """
        SELECT input_usd, output_usd, price_effective_from, price_effective_to
        FROM v_turn_cost
        WHERE uuid = 'a1'
        """
    ).fetchone()
    assert row[0] == pytest.approx(100 * 30 / 1_000_000)
    assert row[1] == pytest.approx(50 * 150 / 1_000_000)
    assert str(row[2]) == "2026-01-01"
    assert str(row[3]) == "2026-06-21"


def test_cache_efficiency_includes_models(loaded):
    rows = A.cache_efficiency(loaded)
    models = {r[0] for r in rows}
    assert "claude-sonnet-4-6" in models
    assert "claude-haiku-4-5-20251001" in models


def test_grouped_timeseries_uses_display_timezone(loaded):
    rows = A.grouped_timeseries(
        loaded,
        bucket="hour",
        stack="none",
        timezone="America/Los_Angeles",
    )
    assert rows
    # Fixture timestamps are 2026-06-20T10:00:xxZ, which is 03:00 Pacific.
    assert rows[0][0].hour == 3
    assert rows[0][1] == "total"


def test_grouped_timeseries_can_stack_by_host_and_project(loaded):
    rows = A.grouped_timeseries(
        loaded,
        bucket="day",
        stack="host_project",
        timezone="America/Los_Angeles",
    )
    assert rows
    assert any(r[1] == "local / test-proj" for r in rows)
    assert sum(float(r[4]) for r in rows) == pytest.approx(A.summary(loaded)["total_usd"])


def test_metadata_reports_latest_turn(loaded):
    meta = A.metadata(loaded)
    assert meta["turns"] >= 1
    assert meta["latest_turn_ts"] is not None


def test_token_type_timeseries_splits_token_kinds(loaded):
    rows = A.token_type_timeseries(
        loaded,
        bucket="hour",
        timezone="America/Los_Angeles",
    )
    series = {r[1] for r in rows}
    assert {
        "input_tokens",
        "output_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
    }.issubset(series)
    assert sum(int(r[2]) for r in rows) == A.summary(loaded)["input_tokens"] + A.summary(loaded)["output_tokens"] + A.summary(loaded)["cache_write_5m"] + A.summary(loaded)["cache_write_1h"] + A.summary(loaded)["cache_read"]


def test_token_stats_returns_equivalences(loaded):
    stats = A.token_stats(loaded)
    assert stats["total_tokens"] > 0
    assert stats["fresh_tokens"] > 0
    assert stats["estimated_words"] > 0
    assert "phone_charges" in stats


def test_calendar_heatmap_includes_tokens(loaded):
    rows = A.calendar_heatmap(loaded, days_back=3650)
    assert rows
    assert len(rows[0]) == 4
    assert sum(int(r[3]) for r in rows) == A.summary(loaded)["input_tokens"] + A.summary(loaded)["output_tokens"] + A.summary(loaded)["cache_write_5m"] + A.summary(loaded)["cache_write_1h"] + A.summary(loaded)["cache_read"]


def test_turn_explorer_filters_by_display_day(loaded):
    rows = A.turn_explorer(
        loaded,
        day="2026-06-20",
        timezone="America/Los_Angeles",
    )
    assert rows
    assert {r[0] for r in rows} >= {"a1", "a2"}
    assert A.turn_explorer(
        loaded,
        day="2026-06-19",
        timezone="America/Los_Angeles",
    ) == []


def test_parse_since():
    assert A.parse_since("all") is None
    assert A.parse_since(None) is None
    assert A.parse_since("7d") is not None
    assert A.parse_since("24h") is not None
    with pytest.raises(ValueError):
        A.parse_since("garbage")


def test_parse_window():
    from datetime import datetime

    # all / empty -> open on both ends
    assert A.parse_window("all") == (None, None)
    assert A.parse_window(None) == (None, None)

    # relative -> lower bound only
    start, end = A.parse_window("7d")
    assert start is not None and end is None

    # absolute datetime range -> both bounds, used verbatim
    start, end = A.parse_window("2026-01-01T08:00..2026-01-01T17:00")
    assert start == datetime(2026, 1, 1, 8, 0)
    assert end == datetime(2026, 1, 1, 17, 0)

    # date-only end is inclusive of the whole day (bumped to next midnight)
    start, end = A.parse_window("2026-01-01..2026-02-15")
    assert start == datetime(2026, 1, 1, 0, 0)
    assert end == datetime(2026, 2, 16, 0, 0)

    # open-ended bounds on either side
    assert A.parse_window("2026-01-01..") == (datetime(2026, 1, 1, 0, 0), None)
    assert A.parse_window("..2026-02-15") == (None, datetime(2026, 2, 16, 0, 0))

    # a bare absolute date reads as a "since <point>" lower bound
    assert A.parse_window("2026-03-01") == (datetime(2026, 3, 1, 0, 0), None)

    # parse_since stays the lower bound of whatever window was given
    assert A.parse_since("2026-01-01..2026-02-15") == datetime(2026, 1, 1, 0, 0)

    with pytest.raises(ValueError):
        A.parse_window("garbage")


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
    """Back-to-back 5h blocks, each spreading 9 turns across the full window
    so the LAST turn lands near the reset, then resuming right at that reset:
    the classic 'hit the wall, wait for refresh' pattern → real ceiling with
    wall events.

    `_fixed_blocks` is turn-anchored (the first turn opens t=0 for that block),
    so 'late in the window' means late relative to the first turn — meaning
    the turns need to span most of the 5h window for the wall check to fire.
    """
    w = 5 * 3600
    times, values = [], []
    step = w / 16  # 9 turns spaced to cover (first .. first + w/2)
    for day in range(6):
        base = day * w  # blocks back-to-back so each resume == previous reset
        for k in range(9):
            times.append(base + int(k * step))
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
