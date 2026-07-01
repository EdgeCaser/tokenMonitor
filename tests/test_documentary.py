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


def test_narrate_short_circuits_on_empty_brief(loaded):
    from types import SimpleNamespace
    with patch.object(D, "build_brief", return_value=SimpleNamespace(empty=True)):
        out = D.narrate(loaded, since="all", host=None, engine="auto")
    assert out == {"text": "", "engine": "template", "model": None, "empty": True}


def test_narrate_template_engine_skips_ollama(loaded):
    with patch.object(D, "ollama_status") as st:
        out = D.narrate(loaded, since="all", host=None, engine="template")
    assert out["engine"] == "template"
    st.assert_not_called()


def test_api_documentary_returns_template_payload(loaded):
    loaded.close()  # release the write connection so the endpoint can open read-only
    from tokmon import server
    result = server.api_documentary(since="all", host=None, engine="template")
    assert result["engine"] == "template"
    assert result["empty"] is False
    assert isinstance(result["text"], str) and result["text"]


def test_api_capabilities_reports_ollama(loaded):
    loaded.close()
    from tokmon import server
    with patch.object(D, "ollama_status",
                      return_value={"available": False, "url": "u", "models": [], "model": None}):
        result = server.api_capabilities()
    assert "ollama" in result
    assert result["ollama"]["available"] is False


def test_render_ollama_sends_bounded_options(loaded):
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"message": {"content": "A narration."}}'

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    brief = D.build_brief(loaded, since="all", host=None)
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = D.render_ollama(brief, "m", "http://x")
    assert out == "A narration."
    assert captured["body"]["options"]["num_predict"] == 500
    assert captured["body"]["options"]["num_ctx"] == 4096
    assert captured["body"]["keep_alive"] == "5m"
    # unrelated payload keys must survive the bounding
    assert captured["body"]["model"] == "m"
    assert captured["body"]["stream"] is False
    assert len(captured["body"]["messages"]) == 2


def test_unload_model_posts_keep_alive_zero():
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"done": true}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok = D.unload_model("llama3.2:3b", "http://x")
    assert ok is True
    assert captured["url"].endswith("/api/generate")
    assert captured["body"]["model"] == "llama3.2:3b"
    assert captured["body"]["keep_alive"] == 0


def test_unload_model_returns_false_and_never_raises_on_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert D.unload_model("llama3.2:3b", "http://x") is False


def test_seed_is_stable_across_processes():
    # crc32-based seed is deterministic (builtin hash() is per-process randomized).
    assert D._seed("all", None, 42) == 480698661
    assert D._seed("all", None, 42) == D._seed("all", None, 42)
    assert D._seed("7d", "desktop", 5) != D._seed("7d", "desktop", 6)


def test_narrate_empty_window_returns_gentle_payload(loaded):
    # Real build_brief path (not mocked): a host with no turns yields the empty payload.
    out = D.narrate(loaded, since="all", host="no-such-host", engine="template")
    assert out == {"text": "", "engine": "template", "model": None, "empty": True}


def test_api_documentary_default_engine_uses_doc_engine(loaded):
    loaded.close()
    from tokmon import server
    # No engine query param -> falls back to configured TOKMON_DOC_ENGINE.
    with patch.object(D, "DOC_ENGINE", "template"), \
         patch.object(D, "ollama_status") as st:
        result = server.api_documentary(since="all", host=None, engine=None)
    assert result["engine"] == "template"
    st.assert_not_called()
