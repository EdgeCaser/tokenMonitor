import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tokmon import documentary as D, analytics as A, config as cfg_mod, db, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.jsonl"


def _fake_urlopen(payload):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return cm


def test_ollama_status_available_lists_models():
    payload = {"models": [{"name": "llama3.2"}, {"name": "qwen2.5"}]}
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        st = D.ollama_status("http://127.0.0.1:11434")
    assert st["available"] is True
    assert st["models"] == ["llama3.2", "qwen2.5"]
    assert st["model"] == "llama3.2"


def test_ollama_status_unavailable_on_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        st = D.ollama_status("http://127.0.0.1:11434")
    assert st["available"] is False
    assert st["models"] == []
    assert st["model"] is None


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    projects_dir = tmp_path / "home" / ".claude" / "projects"
    proj_dir = projects_dir / "-tmp-test-proj"
    proj_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, proj_dir / "test-session-001.jsonl")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cfg_mod, "DEFAULT_PROJECTS_DIR", projects_dir)
    ingest.incremental(roots=[(projects_dir, "local")])
    return A.connect_with_views()


def test_build_brief_has_core_facts(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    assert brief.turns >= 1
    assert brief.total_usd > 0
    assert brief.dominant_model is not None
    assert brief.biggest_turn_model is not None
    assert brief.empty is False
