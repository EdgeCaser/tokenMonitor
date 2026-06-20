import shutil
from pathlib import Path

import pytest

from tokmon import analytics as A
from tokmon import config as cfg_mod
from tokmon import db, ingest


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.jsonl"


def _stage_root(base: Path, name: str = "-tmp-test-proj",
                jsonl: str = "test-session-001.jsonl") -> Path:
    proj_dir = base / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    target = proj_dir / jsonl
    shutil.copy(FIXTURE, target)
    return target


@pytest.fixture
def tmp_projects(tmp_path, monkeypatch):
    home = tmp_path / "home"
    projects_dir = home / ".claude" / "projects"
    target = _stage_root(projects_dir)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")
    monkeypatch.setattr(ingest, "DEFAULT_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cfg_mod, "DEFAULT_PROJECTS_DIR", projects_dir)
    return target


def test_incremental_ingest(tmp_projects):
    stats = ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    assert stats.new_turns == 3
    assert stats.new_user_turns == 1
    assert stats.new_tool_calls == 1
    assert stats.malformed_lines == 1


def test_idempotent_reingest(tmp_projects):
    ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    stats2 = ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    assert stats2.new_turns == 0
    assert stats2.new_user_turns == 0
    assert stats2.new_tool_calls == 0
    assert stats2.malformed_lines == 0


def test_appending_triggers_only_delta(tmp_projects):
    roots = [(tmp_projects.parent.parent, "local")]
    ingest.incremental(roots=roots)
    extra = (
        '{"type":"assistant","uuid":"a_appended","sessionId":"test-session-001",'
        '"cwd":"/tmp/test-proj","timestamp":"2026-06-20T11:00:00.000Z",'
        '"isSidechain":false,'
        '"message":{"model":"claude-sonnet-4-6","content":[],'
        '"usage":{"input_tokens":7,"output_tokens":7,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
        '"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":0}}}}\n'
    )
    with open(tmp_projects, "a") as f:
        f.write(extra)
    stats = ingest.incremental(roots=roots)
    assert stats.new_turns == 1
    assert stats.new_user_turns == 0


def test_analytics_views_have_data(tmp_projects):
    ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    conn = A.connect_with_views()
    s = A.summary(conn)
    assert s["turns"] == 3
    assert s["sessions"] == 1
    assert s["total_usd"] > 0


def test_sidechain_attribution(tmp_projects):
    ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    conn = A.connect_with_views()
    side = conn.execute("SELECT COUNT(*) FROM turns WHERE is_sidechain").fetchone()[0]
    assert side == 1


def test_synthetic_zero_cost(tmp_projects):
    ingest.incremental(roots=[(tmp_projects.parent.parent, "local")])
    conn = A.connect_with_views()
    cost = conn.execute(
        "SELECT total_usd FROM v_turn_cost WHERE model = '<synthetic>'"
    ).fetchone()
    assert cost[0] == 0


def test_multi_root_host_attribution(tmp_path, monkeypatch):
    """Two roots with different hosts → host column populated correctly."""
    home_a = tmp_path / "host_a" / ".claude" / "projects"
    home_b = tmp_path / "host_b" / ".claude" / "projects"
    _stage_root(home_a, jsonl="session-a.jsonl")
    # Different session UUID so dedup doesn't collapse them: rewrite IDs.
    file_b = _stage_root(home_b, jsonl="session-b.jsonl")
    raw = file_b.read_text()
    file_b.write_text(
        raw.replace('test-session-001', 'test-session-002')
           .replace('"uuid":"u1"', '"uuid":"u1b"')
           .replace('"uuid":"a1"', '"uuid":"a1b"')
           .replace('"uuid":"a2"', '"uuid":"a2b"')
           .replace('"uuid":"a3"', '"uuid":"a3b"')
           .replace('"parentUuid":"u1"', '"parentUuid":"u1b"')
    )
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")

    stats = ingest.incremental(roots=[(home_a, "host-a"), (home_b, "host-b")])
    assert stats.new_turns == 6  # 3 per root

    conn = A.connect_with_views()
    by_host = dict(conn.execute(
        "SELECT host, COUNT(*) FROM turns GROUP BY host"
    ).fetchall())
    assert by_host == {"host-a": 3, "host-b": 3}


def test_duplicate_uuid_across_roots_deduped(tmp_path, monkeypatch):
    """If the same JSONL appears under two roots, dedup picks one host."""
    home_a = tmp_path / "host_a" / ".claude" / "projects"
    home_b = tmp_path / "host_b" / ".claude" / "projects"
    _stage_root(home_a)
    _stage_root(home_b)  # same UUIDs
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")

    stats = ingest.incremental(roots=[(home_a, "host-a"), (home_b, "host-b")])
    assert stats.new_turns == 3  # not 6 — UUIDs dedup
    conn = A.connect_with_views()
    hosts = {r[0] for r in conn.execute(
        "SELECT DISTINCT host FROM turns"
    ).fetchall()}
    # First root wins on conflict (we iterate host-a first)
    assert hosts == {"host-a"}


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = cfg_mod.Config(default_host="laptop")
    cfg_mod.add_root(cfg, tmp_path / "pi-sync", "pi")
    cfg_mod.add_root(cfg, tmp_path / "work-mac-sync", "work-mac")
    cfg_mod.save(cfg, path)
    loaded = cfg_mod.load(path)
    assert loaded.default_host == "laptop"
    assert {r.host for r in loaded.extra_roots} == {"pi", "work-mac"}


def test_config_add_is_idempotent(tmp_path):
    cfg = cfg_mod.Config()
    assert cfg_mod.add_root(cfg, tmp_path / "pi", "pi") is True
    assert cfg_mod.add_root(cfg, tmp_path / "pi", "pi") is False
    assert len(cfg.extra_roots) == 1


def test_config_remove_by_host(tmp_path):
    cfg = cfg_mod.Config()
    cfg_mod.add_root(cfg, tmp_path / "pi", "pi")
    cfg_mod.add_root(cfg, tmp_path / "wm", "work-mac")
    assert cfg_mod.remove_root(cfg, "pi") == 1
    assert {r.host for r in cfg.extra_roots} == {"work-mac"}
