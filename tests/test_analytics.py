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
