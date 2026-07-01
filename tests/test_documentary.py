import json
from unittest.mock import patch, MagicMock

from tokmon import documentary as D


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
