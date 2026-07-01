# Live Narration Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in on/off switch for live (Ollama) narration to the Documentary tab so the model only loads when explicitly requested, and bound Ollama's resource footprint.

**Architecture:** A client-side switch (default off, persisted in `localStorage`) drives an explicit `engine` param on the existing `GET /api/documentary` call: `template` (zero Ollama contact, nothing loads) or `ollama`. Backend changes bound Ollama's footprint (`num_ctx`, short self-evicting `keep_alive`) and add a tested-but-unused `unload_model` seam for a possible future immediate-unload.

**Tech Stack:** Python 3.11/3.12 (stdlib `urllib` only), FastAPI, DuckDB, a single static `web/index.html` (vanilla JS + Tailwind CDN classes), pytest.

## Global Constraints

- No new Python dependencies. Ollama is reached with the standard library (`urllib`) only.
- Generated prose must never contain em dashes. `render_ollama` already strips them; do not remove that.
- Ollama footprint values are exact: `options.num_ctx = 4096`, top-level `keep_alive = "5m"`.
- Off means the template engine and zero Ollama contact. Never load a model just because Ollama is reachable.
- No new server endpoint in this change. `unload_model` ships as a function only (the seam), not wired to any route or the UI.
- Model selection stays deploy-time config (`TOKMON_OLLAMA_MODEL`, default `llama3.2:3b`); do not add a model picker.
- Work lands as commits on `feat/attenborough-mode`.
- End each commit message with the environment's `Co-Authored-By:` and `Claude-Session:` trailers.

---

### Task 1: Bound Ollama footprint (num_ctx + keep_alive)

**Files:**
- Modify: `tokmon/documentary.py:195-203` (the `render_ollama` payload)
- Test: `tests/test_documentary.py:130-146` (extend `test_render_ollama_sends_bounded_options`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `render_ollama(brief, model, url)` unchanged signature; its POST body now additionally carries `options.num_ctx == 4096` and top-level `keep_alive == "5m"`.

- [ ] **Step 1: Extend the existing bounded-options test to assert the new keys**

In `tests/test_documentary.py`, add two assertions at the end of `test_render_ollama_sends_bounded_options` (currently ends at line 146):

```python
    assert captured["body"]["options"]["num_predict"] == 500
    assert captured["body"]["options"]["num_ctx"] == 4096
    assert captured["body"]["keep_alive"] == "5m"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_documentary.py::test_render_ollama_sends_bounded_options -v`
Expected: FAIL with `KeyError: 'num_ctx'` (the payload has no `num_ctx` key yet).

- [ ] **Step 3: Add the bounded options to the payload**

In `tokmon/documentary.py`, change the `render_ollama` payload (lines 195-203) from:

```python
        payload = {
            "model": model,
            "stream": False,
            "options": {"num_predict": 500},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _facts_text(brief)},
            ],
        }
```

to:

```python
        payload = {
            "model": model,
            "stream": False,
            "keep_alive": "5m",
            "options": {"num_predict": 500, "num_ctx": 4096},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _facts_text(brief)},
            ],
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_documentary.py::test_render_ollama_sends_bounded_options -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: bound Ollama context and shorten keep_alive"
```

---

### Task 2: `unload_model` seam

**Files:**
- Modify: `tokmon/documentary.py` (add `unload_model` immediately after `render_ollama`, before `narrate`)
- Test: `tests/test_documentary.py` (append two tests)

**Interfaces:**
- Consumes: module globals `OLLAMA_URL`, `urllib.request`, `json` (already imported at top of `documentary.py`).
- Produces: `unload_model(model: str, url: str | None = None) -> bool` — POSTs `{"model": model, "keep_alive": 0}` to `<base>/api/generate`; returns `True` on a clean call, `False` on any failure; never raises. Not called by any route or the UI (future seam).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_documentary.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_documentary.py -k unload_model -v`
Expected: FAIL with `AttributeError: module 'tokmon.documentary' has no attribute 'unload_model'`.

- [ ] **Step 3: Implement `unload_model`**

In `tokmon/documentary.py`, insert this function directly after `render_ollama` (after its `return text or None` line) and before `def narrate(`:

```python
def unload_model(model: str, url: str | None = None) -> bool:
    """Ask Ollama to evict a model from memory. Never raises.

    Not wired to any route or the UI yet; this is the seam for a future
    immediate-unload-on-off behavior. Ollama unloads a model when it receives
    a request carrying keep_alive: 0.
    """
    base = (url or OLLAMA_URL).rstrip("/")
    payload = {"model": model, "keep_alive": 0}
    try:
        req = urllib.request.Request(
            base + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception:
        return False
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_documentary.py -k unload_model -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add tokmon/documentary.py tests/test_documentary.py
git commit -m "feat: add unload_model seam for future immediate unload"
```

---

### Task 3: Documentary tab live-narration switch (frontend)

**Files:**
- Modify: `web/index.html:729` (header markup — replace the lone Roll film button with a toggle + button group)
- Modify: `web/index.html:2147-2155` (`loadDocumentary` — reflect Ollama availability + stored state on the switch)
- Modify: `web/index.html:2168-2182` (`rollFilm` — send the `engine` param)
- Modify: `web/index.html:859` (bind the switch's change handler for persistence)

**Interfaces:**
- Consumes: existing JS helpers `$`, `api`, `since`, `hostQ`, `host`; the backend `GET /api/documentary?...&engine=ollama|template` (already accepts `engine`); `GET /api/capabilities` returning `{ollama: {available: bool, ...}}`.
- Produces: DOM element `#doc-live` (checkbox) and `#doc-live-wrap` (label); `localStorage` key `tokmon.docLive` = `"on"` | `"off"`.

Note: this task is browser-verified. There is no JS unit-test harness in this repo; frontend states are validated manually, consistent with the rest of the dashboard. The backend guarantee this relies on (that `engine="template"` makes zero Ollama contact) is already covered by `test_narrate_template_engine_skips_ollama`.

- [ ] **Step 1: Add the switch markup**

In `web/index.html`, replace line 729:

```html
        <button id="doc-roll" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded text-sm font-medium">Roll film</button>
```

with:

```html
        <div class="flex items-center gap-4">
          <label id="doc-live-wrap" class="flex items-center gap-2 text-sm text-slate-300 cursor-pointer select-none" title="requires a reachable Ollama host">
            <input type="checkbox" id="doc-live" class="accent-emerald-500" disabled>
            <span>Live narration (Ollama)</span>
          </label>
          <button id="doc-roll" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded text-sm font-medium">Roll film</button>
        </div>
```

- [ ] **Step 2: Reflect availability and stored state in `loadDocumentary`**

In `web/index.html`, replace the body of `loadDocumentary` (lines 2147-2155) so it ends by configuring the switch. The final function reads:

```javascript
async function loadDocumentary() {
  const sinceSel = $("#since");
  const sinceText = sinceSel && sinceSel.selectedOptions.length
    ? sinceSel.selectedOptions[0].textContent : "all time";
  $("#doc-range").textContent = `narrating: ${host() || "all hosts"}, ${sinceText}`;
  let caps = { ollama: { available: false } };
  try { caps = await api("/api/capabilities"); } catch (e) { /* offline gate */ }
  renderDocBanner(caps.ollama);
  const live = $("#doc-live");
  const wrap = $("#doc-live-wrap");
  const available = !!(caps.ollama && caps.ollama.available);
  live.disabled = !available;
  live.checked = available && localStorage.getItem("tokmon.docLive") === "on";
  wrap.title = available ? "" : "requires a reachable Ollama host";
  wrap.classList.toggle("opacity-50", !available);
  wrap.classList.toggle("cursor-not-allowed", !available);
}
```

- [ ] **Step 3: Send the engine param from `rollFilm`**

In `web/index.html`, change the fetch line inside `rollFilm` (line 2173) from:

```javascript
    const d = await api(`/api/documentary?since=${since()}${hostQ()}`);
```

to:

```javascript
    const engine = ($("#doc-live") && $("#doc-live").checked) ? "ollama" : "template";
    const d = await api(`/api/documentary?since=${since()}${hostQ()}&engine=${engine}`);
```

- [ ] **Step 4: Persist the switch state on change**

In `web/index.html`, directly after line 859 (`$("#doc-roll").addEventListener("click", rollFilm);`), add:

```javascript
$("#doc-live").addEventListener("change", (e) => {
  localStorage.setItem("tokmon.docLive", e.target.checked ? "on" : "off");
});
```

- [ ] **Step 5: Verify in the browser (Ollama up)**

Preconditions: Ollama running on the box with `llama3.2:3b` pulled; start the dashboard against real data:

```bash
TOKMON_OLLAMA_MODEL=llama3.2:3b python -m tokmon.server
```

Open the dashboard, go to the Documentary tab, and confirm:
- The "Live narration (Ollama)" switch is enabled (not greyed) and unchecked by default.
- With the switch OFF, "Roll film" returns instantly and the credit reads "narrated by the local template engine".
- Turn the switch ON, reload the page: the switch is still ON (localStorage persisted).
- With the switch ON, "Roll film" credit reads "narrated by Ollama (llama3.2:3b)"; output has no em dashes.

- [ ] **Step 6: Verify in the browser (Ollama down)**

Stop Ollama (or point `TOKMON_OLLAMA_URL` at a dead port), reload the Documentary tab, and confirm:
- The switch is greyed/disabled with the "requires a reachable Ollama host" tooltip.
- The existing template-mode banner is shown.
- "Roll film" still works and credits the template engine.

- [ ] **Step 7: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests pass (the 57 existing plus the 2 new `unload_model` tests = 59).

- [ ] **Step 8: Commit**

```bash
git add web/index.html
git commit -m "feat: add live-narration on/off switch to Documentary tab"
```

---

## Self-Review

**Spec coverage:**
- Toggle on Documentary tab, default off, `localStorage` persistence, disabled-when-unavailable → Task 3 (Steps 1, 2, 4).
- Off = template only, zero Ollama contact → Task 3 Step 3 (sends `engine=template`); backend guarantee already tested by `test_narrate_template_engine_skips_ollama`.
- `render_ollama` `num_ctx=4096` + `keep_alive="5m"` → Task 1.
- `unload_model` tested seam, function only, no endpoint → Task 2.
- Model default `llama3.2:3b` via `TOKMON_OLLAMA_MODEL`, no UI picker → deploy config; used in Task 3 Step 5 verification, no code task (correct — it is env config).
- Graceful fallback (Ollama down while on) → existing `narrate` behavior; verified in Task 3 Step 6.
- Tests: bounded-options assertions, unload_model success/failure, template contact-free → Tasks 1, 2, and existing coverage.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step shows complete code. Clear.

**Type consistency:** `unload_model(model, url=None) -> bool` defined in Task 2 and referenced consistently; DOM ids `#doc-live` / `#doc-live-wrap` and `localStorage` key `tokmon.docLive` used identically across Task 3 steps; `engine` values `"ollama"`/`"template"` match the backend's accepted values. Consistent.
