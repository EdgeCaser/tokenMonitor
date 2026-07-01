# Attenborough Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Documentary tab that narrates the selected time window as a David-Attenborough-style nature documentary, generated locally.

**Architecture:** A self-contained `tokmon/documentary.py` core turns analytics facts into a "brief", then renders narration either through Ollama (when reachable) or a built-in template engine. Two new read-only server endpoints expose narration and capability status. A new dashboard tab renders the script and, when Ollama is absent, shows a non-blocking upgrade banner.

**Tech Stack:** Python 3.11+, DuckDB (via existing `tokmon.analytics`), FastAPI, vanilla JS + Tailwind (existing dashboard). Ollama HTTP called with the Python standard library.

## Global Constraints

- Python >= 3.11.
- No new Python dependencies. Use the standard library (`urllib`, `json`) for the Ollama HTTP calls.
- Generated narration must never contain an em dash (`—`). This applies to both the template banks and the Ollama system prompt, and is enforced with a post-process replace.
- Nothing leaves the machine. Ollama is reached only at `http://127.0.0.1:11434` by default.
- Follow existing patterns: analytics functions in `tokmon/analytics.py`, route style in `tokmon/server.py` (`@app.get`, `_conn()`), and the frontend `api()`, `since()`, `hostQ()`, `setActive()`, `load()` helpers in `web/index.html`.
- Env overrides: `TOKMON_OLLAMA_URL` (default `http://127.0.0.1:11434`), `TOKMON_OLLAMA_MODEL` (default: first model Ollama reports), `TOKMON_DOC_ENGINE` (`auto` default, or `ollama`, or `template`).

---

### Task 1: Ollama capability probe

**Files:**
- Create: `tokmon/documentary.py`
- Test: `tests/test_documentary.py`

**Interfaces:**
- Produces: `ollama_status(url: str | None = None) -> dict` returning
  `{"available": bool, "url": str, "models": list[str], "model": str | None}`.
- Produces module constants: `OLLAMA_URL`, `OLLAMA_MODEL`, `DOC_ENGINE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_documentary.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tokmon.documentary'`

- [ ] **Step 3: Write minimal implementation**

```python
# tokmon/documentary.py
"""Attenborough Mode: turn analytics facts into a nature-documentary narration.

Fully local. Uses Ollama when reachable, else a built-in template engine.
"""
from __future__ import annotations

import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("TOKMON_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("TOKMON_OLLAMA_MODEL")
DOC_ENGINE = os.environ.get("TOKMON_DOC_ENGINE", "auto")


def ollama_status(url: str | None = None) -> dict:
    """Probe the local Ollama server. Never raises."""
    base = (url or OLLAMA_URL).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in data.get("models", [])]
    except Exception:
        return {"available": False, "url": base, "models": [], "model": None}
    model = OLLAMA_MODEL if OLLAMA_MODEL in models else (models[0] if models else None)
    return {"available": bool(models), "url": base, "models": models, "model": model}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: add Ollama capability probe for Attenborough Mode"
```

---

### Task 2: Build the documentary brief

**Files:**
- Modify: `tokmon/documentary.py`
- Test: `tests/test_documentary.py`

**Interfaces:**
- Consumes: `tokmon.analytics` functions `summary`, `spend_by`, `top_turns`, `cache_savings`, `burn_rate`, `monthly_forecast` (existing signatures).
- Produces: dataclass `DocBrief` and `build_brief(conn, since="all", host=None, tz="America/Los_Angeles") -> DocBrief`. `DocBrief.empty` is True when `turns == 0`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_documentary.py
import shutil
from pathlib import Path
import pytest
from tokmon import analytics as A, config as cfg_mod, db, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.jsonl"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_documentary.py::test_build_brief_has_core_facts -v`
Expected: FAIL with `AttributeError: module 'tokmon.documentary' has no attribute 'build_brief'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to tokmon/documentary.py
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import analytics as A

DISPLAY_TZ = "America/Los_Angeles"


@dataclass
class DocBrief:
    since: str
    host: str | None
    turns: int
    sessions: int
    projects: int
    total_usd: float
    dominant_model: str | None
    dominant_model_usd: float
    busiest_project: str | None
    busiest_project_usd: float
    busiest_project_turns: int
    biggest_turn_model: str | None
    biggest_turn_project: str | None
    biggest_turn_usd: float
    biggest_turn_hour: int | None
    top_tool: str | None
    top_tool_calls: int
    cache_saved_usd: float
    cache_savings_pct: float
    burn_per_hour_usd: float
    projected_eom_usd: float
    month_to_date_usd: float

    @property
    def empty(self) -> bool:
        return self.turns == 0


def build_brief(conn, since: str = "all", host: str | None = None,
                tz: str = DISPLAY_TZ) -> DocBrief:
    s = A.summary(conn, since=since, host=host)
    models = A.spend_by(conn, "model", since=since, host=host, limit=1)
    projects = A.spend_by(conn, "project", since=since, host=host, limit=1)
    tools = A.spend_by(conn, "tool", since=since, host=host, limit=1)
    biggest = A.top_turns(conn, metric="cost", n=1, since=since, host=host)
    cache = A.cache_savings(conn, since=since, host=host)
    burn = A.burn_rate(conn, window_minutes=60, host=host)
    forecast = A.monthly_forecast(conn, host=host)

    proj = projects[0] if projects else None
    tool = tools[0] if tools else None
    bt = biggest[0] if biggest else None
    bt_hour = None
    if bt is not None and isinstance(bt[1], datetime):
        local = bt[1].replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
        bt_hour = local.hour

    return DocBrief(
        since=since, host=host,
        turns=int(s["turns"]), sessions=int(s["sessions"]),
        projects=int(s["projects"]), total_usd=float(s["total_usd"]),
        dominant_model=(models[0][0] if models else None),
        dominant_model_usd=(float(models[0][3]) if models else 0.0),
        busiest_project=(proj[0] if proj else None),
        busiest_project_usd=(float(proj[4]) if proj else 0.0),
        busiest_project_turns=(int(proj[2]) if proj else 0),
        biggest_turn_model=(bt[4] if bt else None),
        biggest_turn_project=(bt[2] if bt else None),
        biggest_turn_usd=(float(bt[9]) if bt else 0.0),
        biggest_turn_hour=bt_hour,
        top_tool=(tool[0] if tool else None),
        top_tool_calls=(int(tool[1]) if tool else 0),
        cache_saved_usd=float(cache["counterfactual_extra_usd"]),
        cache_savings_pct=float(cache["savings_pct"]),
        burn_per_hour_usd=float(burn["rate_per_hour_usd"]),
        projected_eom_usd=float(forecast["projected_eom_usd"]),
        month_to_date_usd=float(forecast["month_to_date_usd"]),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: assemble documentary brief from analytics"
```

---

### Task 3: Template narration engine

**Files:**
- Modify: `tokmon/documentary.py`
- Test: `tests/test_documentary.py`

**Interfaces:**
- Consumes: `DocBrief`.
- Produces: `render_template(brief: DocBrief, seed: int = 0) -> str`. Deterministic for a given seed. Never contains an em dash.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_documentary.py
def test_render_template_is_nonempty_and_has_no_em_dash(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    text = D.render_template(brief, seed=1)
    assert len(text) > 80
    assert "—" not in text
    assert f"{brief.turns}" in text


def test_render_template_is_deterministic(loaded):
    brief = D.build_brief(loaded, since="all", host=None)
    assert D.render_template(brief, seed=42) == D.render_template(brief, seed=42)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_documentary.py::test_render_template_is_nonempty_and_has_no_em_dash -v`
Expected: FAIL with `AttributeError: ... has no attribute 'render_template'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to tokmon/documentary.py
import random


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def render_template(brief: DocBrief, seed: int = 0) -> str:
    rnd = random.Random(seed)
    model = brief.dominant_model or "an unidentified model"
    project = brief.busiest_project or "an unnamed project"
    beats: list[str] = []

    beats.append(rnd.choice([
        f"Here, in the pale glow of the terminal, we find the developer in its "
        f"natural habitat. Across this period it produced {brief.turns} turns "
        f"over {brief.sessions} sessions and {brief.projects} projects.",
        f"Observe the developer at work. In this window it has generated "
        f"{brief.turns} turns, spread across {brief.sessions} sessions and "
        f"{brief.projects} distinct projects.",
    ]))

    beats.append(rnd.choice([
        f"Its companion of choice is {model}, summoned again and again. The "
        f"project {project} consumed the most of its attention, at "
        f"{_usd(brief.busiest_project_usd)}.",
        f"The developer favors {model} above all others. Most of its energy "
        f"flows into {project}, which alone accounts for "
        f"{_usd(brief.busiest_project_usd)}.",
    ]))

    if brief.biggest_turn_model:
        hour = brief.biggest_turn_hour
        when = ""
        if hour is not None:
            phase = "in the small hours" if hour < 6 else (
                "under cover of night" if hour >= 22 else f"at the {hour}:00 hour")
            when = f", {phase}"
        beats.append(
            f"Then comes the feeding frenzy. A single turn on "
            f"{brief.biggest_turn_model}, in {brief.biggest_turn_project or 'the wild'}"
            f"{when}, cost {_usd(brief.biggest_turn_usd)}. A remarkable display of appetite."
        )

    beats.append(
        f"The reckoning. Over this window the developer spent "
        f"{_usd(brief.total_usd)}. Caching spared it a further "
        f"{_usd(brief.cache_saved_usd)}, some {brief.cache_savings_pct:.0f} percent "
        f"of what it might have paid. At the current pace, the month will close "
        f"near {_usd(brief.projected_eom_usd)}."
    )

    beats.append(rnd.choice([
        "And so the cycle continues, as it does every day, in terminals the world over.",
        "The sun sets on the workspace. Tomorrow, the developer will hunt again.",
    ]))

    return "\n\n".join(beats).replace("—", ", ")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: local template narration engine"
```

---

### Task 4: Ollama narration and orchestration

**Files:**
- Modify: `tokmon/documentary.py`
- Test: `tests/test_documentary.py`

**Interfaces:**
- Consumes: `DocBrief`, `ollama_status`, `render_template`.
- Produces:
  - `render_ollama(brief, model, url=None) -> str | None` (None on any failure).
  - `narrate(conn, since="all", host=None, engine="auto", url=None, model=None) -> dict` returning `{"text": str, "engine": "ollama"|"template", "model": str|None, "empty": bool}`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_documentary.py
from unittest.mock import patch


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_documentary.py::test_narrate_falls_back_to_template_when_ollama_absent -v`
Expected: FAIL with `AttributeError: ... has no attribute 'narrate'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to tokmon/documentary.py
SYSTEM_PROMPT = (
    "You are Sir David Attenborough narrating a wildlife documentary about a "
    "software developer, observed through their AI coding tool usage. Voice: "
    "wry, affectionate, understated, present tense. Refer to the subject as "
    "'the developer' or 'our subject'. Write five to seven short paragraphs "
    "that weave the facts into narration rather than listing them. Never use "
    "em dashes."
)


def _facts_text(brief: DocBrief) -> str:
    lines = [
        f"turns: {brief.turns}",
        f"sessions: {brief.sessions}",
        f"projects: {brief.projects}",
        f"total spend: {_usd(brief.total_usd)}",
        f"favourite model: {brief.dominant_model}",
        f"busiest project: {brief.busiest_project} ({_usd(brief.busiest_project_usd)})",
        f"most expensive turn: {brief.biggest_turn_model} in "
        f"{brief.biggest_turn_project} for {_usd(brief.biggest_turn_usd)}"
        + (f" at local hour {brief.biggest_turn_hour}" if brief.biggest_turn_hour is not None else ""),
        f"cache saved: {_usd(brief.cache_saved_usd)} ({brief.cache_savings_pct:.0f}%)",
        f"projected month end: {_usd(brief.projected_eom_usd)}",
    ]
    return "Facts about the subject's session:\n" + "\n".join(lines)


def render_ollama(brief: DocBrief, model: str, url: str | None = None) -> str | None:
    base = (url or OLLAMA_URL).rstrip("/")
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _facts_text(brief)},
        ],
    }
    try:
        req = urllib.request.Request(
            base + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data.get("message", {}).get("content", "").strip()
    except Exception:
        return None
    text = text.replace("—", ", ")
    return text or None


def narrate(conn, since: str = "all", host: str | None = None,
            engine: str = "auto", url: str | None = None,
            model: str | None = None) -> dict:
    brief = build_brief(conn, since=since, host=host)
    if brief.empty:
        return {"text": "", "engine": "template", "model": None, "empty": True}
    if engine in ("auto", "ollama"):
        status = ollama_status(url)
        use_model = model or status.get("model")
        if status["available"] and use_model:
            text = render_ollama(brief, use_model, url)
            if text:
                return {"text": text, "engine": "ollama", "model": use_model, "empty": False}
    seed = abs(hash((since, host or "", brief.turns))) % (2 ** 31)
    return {"text": render_template(brief, seed), "engine": "template",
            "model": None, "empty": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: Ollama narration with template fallback"
```

---

### Task 5: Server endpoints

**Files:**
- Modify: `tokmon/server.py` (add two routes near the other `@app.get` routes, for example after `api_summary`)
- Test: `tests/test_documentary.py`

**Interfaces:**
- Consumes: `tokmon.documentary.narrate`, `tokmon.documentary.ollama_status`, existing `_conn()`.
- Produces: `GET /api/documentary?since=&host=&engine=` and `GET /api/capabilities`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_documentary.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_documentary.py::test_api_documentary_returns_template_payload -v`
Expected: FAIL with `AttributeError: module 'tokmon.server' has no attribute 'api_documentary'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to tokmon/server.py, near the other @app.get routes
@app.get("/api/documentary")
def api_documentary(since: str = Query("all"), host: str | None = Query(None),
                    engine: str = Query("auto")):
    from . import documentary as D
    conn = _conn()
    return D.narrate(conn, since=since, host=host, engine=engine)


@app.get("/api/capabilities")
def api_capabilities():
    from . import documentary as D
    return {"ollama": D.ollama_status()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_documentary.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: PASS (all prior tests plus the new ones)

- [ ] **Step 6: Commit**

```bash
git add tokmon/server.py tests/test_documentary.py
git commit -m "feat: documentary and capabilities API endpoints"
```

---

### Task 6: Documentary dashboard tab

**Files:**
- Modify: `web/index.html` (nav button near line 112, a new `<section data-pane="documentary">`, a `<style>` rule, and JS: `loadDocumentary`, `rollFilm`, `renderDocBanner`, a `load()` case, and a click listener)

**Interfaces:**
- Consumes: `GET /api/documentary`, `GET /api/capabilities`, existing `api()`, `since()`, `hostQ()`, `host()`, `$()`, `setActive()`, `load()`.
- Produces: a working Documentary tab. Verified manually (the repo has no JS test harness).

- [ ] **Step 1: Add the nav button**

In the tab nav (the run of `<button class="tab ..." data-tab="...">` around line 112), add before the `quotas` button:

```html
    <button class="tab px-3 py-2 rounded-t text-sm font-medium whitespace-nowrap flex-shrink-0" data-tab="documentary">Documentary</button>
```

- [ ] **Step 2: Add the pane and its style**

Add a new section alongside the other `<section data-pane=...>` blocks:

```html
  <section data-pane="documentary" class="hidden space-y-4">
    <div id="doc-banner"></div>
    <div class="bg-slate-900 text-slate-100 rounded shadow p-6">
      <div class="flex items-center justify-between mb-4 gap-4">
        <div>
          <h2 class="text-lg font-semibold">Attenborough Mode</h2>
          <p id="doc-range" class="text-sm text-slate-400"></p>
        </div>
        <button id="doc-roll" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded text-sm font-medium">Roll film</button>
      </div>
      <div id="doc-output" class="doc-subtitles">Press Roll film to narrate this window.</div>
      <p id="doc-credit" class="text-xs text-slate-500 mt-4"></p>
    </div>
  </section>
```

In the document `<style>` block, add:

```css
.doc-subtitles {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.05rem;
  line-height: 1.9;
  text-align: center;
  max-width: 46rem;
  margin: 0 auto;
  white-space: pre-wrap;
}
```

- [ ] **Step 3: Add the JS (near the other `load*` functions and listeners)**

```javascript
async function loadDocumentary() {
  const sinceSel = $("#since");
  const sinceText = sinceSel && sinceSel.selectedOptions.length
    ? sinceSel.selectedOptions[0].textContent : "all time";
  $("#doc-range").textContent = `narrating: ${host() || "all hosts"}, ${sinceText}`;
  let caps = { ollama: { available: false } };
  try { caps = await api("/api/capabilities"); } catch (e) { /* offline gate */ }
  renderDocBanner(caps.ollama);
}

function renderDocBanner(ollama) {
  const el = $("#doc-banner");
  if (ollama && ollama.available) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <div class="bg-amber-50 border border-amber-200 rounded p-3 text-sm flex flex-wrap items-center gap-3">
      <span>You are in template mode. Install Ollama for richer narration.</span>
      <button id="doc-copy" class="px-2 py-1 bg-white border rounded text-xs">Copy command</button>
      <button id="doc-recheck" class="px-2 py-1 bg-white border rounded text-xs">Check again</button>
    </div>`;
  $("#doc-copy").addEventListener("click",
    () => navigator.clipboard && navigator.clipboard.writeText("ollama pull llama3.2"));
  $("#doc-recheck").addEventListener("click", () => loadDocumentary());
}

async function rollFilm() {
  const out = $("#doc-output");
  out.textContent = "Observing the specimen...";
  $("#doc-credit").textContent = "";
  try {
    const d = await api(`/api/documentary?since=${since()}${hostQ()}`);
    if (d.empty) { out.textContent = "Nothing stirred in this period."; return; }
    out.textContent = d.text;
    $("#doc-credit").textContent = d.engine === "ollama"
      ? `narrated by Ollama (${d.model})`
      : "narrated by the local template engine";
  } catch (e) {
    out.textContent = "The documentary could not be filmed. " + e.message;
  }
}
```

- [ ] **Step 4: Wire the dispatch and the button**

In `load(tab)` (around line 989), add:

```javascript
  if (tab === "documentary") await loadDocumentary();
```

Near the other click listeners (around line 832), add:

```javascript
$("#doc-roll").addEventListener("click", rollFilm);
```

- [ ] **Step 5: Manual verification**

Start the dashboard against the synthetic demo data (or your real data) and check the tab.

Run (from the repo root, using the scratchpad demo generator if you still have it, or your own DB):
```bash
tokmon serve
```
Then in a browser at `http://127.0.0.1:8765/`:
- Click the **Documentary** tab. It shows the range line and the "Press Roll film" prompt.
- Click **Roll film**. Narration appears. With Ollama running, the credit reads "narrated by Ollama (<model>)"; with Ollama stopped, the amber banner appears and the credit reads "narrated by the local template engine".
- Confirm the narration contains no em dashes.

- [ ] **Step 6: Commit**

```bash
git add web/index.html
git commit -m "feat: Documentary tab with Attenborough narration"
```

---

## Self-Review

**Spec coverage:**
- Documentary tab, six beats: Tasks 3 (template beats), 4 (Ollama), 6 (tab). Covered.
- Tiered local engine (Ollama then template): Task 4 `narrate`. Covered.
- Text only, subtitle styling: Task 6 `.doc-subtitles`. Covered.
- Graceful capability handling + CTA + Check again: Task 6 `renderDocBanner` plus `/api/capabilities` in Task 5. Covered. Because Attenborough has a fallback, the banner variant is used; the no-fallback empty-state card is a documented future extension in the spec and is intentionally not built now (YAGNI).
- No em dashes: enforced in Task 3 banks, Task 4 system prompt and post-process replace, tested in Task 3.
- Empty window handling: `narrate` returns `empty: True`, surfaced in `rollFilm`. Covered.
- No new Python dependencies: only `urllib`/`json`. Covered.
- Env overrides: Task 1 constants. Covered.

**Placeholder scan:** No TBDs. Every code step shows complete code. No "handle errors" hand-waving; `render_ollama` and `ollama_status` catch and return sentinels explicitly.

**Type consistency:** `narrate` returns the same dict shape consumed by `rollFilm` (`text`, `engine`, `model`, `empty`). `ollama_status` returns the same dict shape consumed by `narrate`, `api_capabilities`, and `renderDocBanner` (`available`, `model`). `build_brief` field names used in `render_template` and `_facts_text` match the `DocBrief` dataclass. Consistent.

## Notes for the implementer

- Ollama is installed on the development machine, so the `auto` path will use it there. CI has no Ollama, so tests either mock it or force `engine="template"`.
- Do not hold an open write connection while calling the endpoints in tests. The provided endpoint tests call `loaded.close()` first.
- Keep everything on the `feat/attenborough-mode` branch. Merge to main after review.
