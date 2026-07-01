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


def test_render_template_is_nonempty_and_has_no_em_dash(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    text = D.render_template(brief, seed=1)
    assert len(text) > 80
    assert "—" not in text
    assert f"{brief.turns}" in text


def test_render_template_is_deterministic(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    assert D.render_template(brief, seed=42) == D.render_template(brief, seed=42)


def test_narrate_falls_back_to_template_when_ollama_absent(loaded):
    with patch.object(D, "ollama_status",
                      return_value={"available": False, "url": "", "models": [], "model": None}):
        out = D.narrate(loaded, since="all", host=None, engine="auto")
    assert out["engine"] == "template"
    assert out["empty"] is False
    assert out["text"]


def test_narrate_uses_ollama_when_available(loaded):
    fake_status = {"available": True, "url": "u", "models": ["llama3.2"], "model": "llama3.2"}
    with patch.object(D, "ollama_status", return_value=fake_status), \
         patch.object(D, "render_ollama", return_value="A wry narration.") as ro:
        out = D.narrate(loaded, since="all", host=None, engine="auto")
    assert out["engine"] == "ollama"
    assert out["model"] == "llama3.2"
    assert out["text"] == "A wry narration."
    ro.assert_called_once()


def test_render_ollama_returns_none_on_http_error(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert D.render_ollama(brief, "llama3.2", "http://127.0.0.1:11434") is None
