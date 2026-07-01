# Live narration toggle design

Date: 2026-07-01
Status: Approved, ready for implementation planning
Lands on: `feat/attenborough-mode` (folded into the same feature, one final review before merge)

## Summary

Add an on/off switch for live (Ollama) narration to the Documentary tab, so the
model only loads when the user explicitly wants rich narration. When the switch
is off, tokmon uses the local template engine and never contacts Ollama, so no
model is loaded and no RAM or VRAM is held. This makes the RAM cost of the
feature opt-in rather than a side effect of Ollama merely being reachable.

Alongside the toggle, this change bounds and shortens Ollama's resource
footprint (`num_ctx`, self-evicting `keep_alive`) and adds a tested, unused
`unload_model` primitive as a seam for a future "unload immediately on off"
behavior if perf pressure ever calls for it.

## Motivation

Today "Roll film" calls `GET /api/documentary` with no `engine` param, so the
server default `auto` runs: if Ollama is reachable, every roll loads the model
and Ollama keeps it resident. On a 10 GB RTX 3080 the recommended `llama3.2:3b`
model holds ~2.6 GB of VRAM, and the previously configured `qwen2.5:14b` spilled
onto system RAM. Either way, the user pays that cost whenever Ollama happens to
be up, even between rolls and even if they only wanted the instant template
narration. The user asked for an explicit on/off so live narration does not hog
RAM when it is not being used.

## Goals

- A visible on/off switch for live narration on the Documentary tab.
- Off means zero Ollama contact: no probe-driven load, no model resident.
- Default off, so nothing loads unless the user opts in.
- Bound and shorten Ollama's footprint while on (context cap, short keep_alive).
- Leave a clean, tested seam to later switch to immediate model unload without a
  redesign.
- No new Python dependencies. No regressions to the existing template path.

## Non-goals (out of scope for this change)

- Immediate model unload on toggle-off. The seam is built and tested, but the UI
  does not call it yet. Wiring it up is a deliberate future decision, gated on
  observing real perf pressure.
- A model picker in the UI. Model selection stays deploy-time config
  (`TOKMON_OLLAMA_MODEL`), defaulting to `llama3.2:3b`.
- Server-side or cross-client persistence of the toggle. State is per-browser.
- Any change to voice/TTS, additional tabs, or other engines.

## Decisions (locked in brainstorming)

- **Off behavior:** off switches to the template engine only. The model unloads
  on Ollama's own `keep_alive` timeout. We do *not* force an immediate unload,
  but we architect for it (see the `unload_model` seam).
- **keep_alive:** short and self-evicting (`"5m"`), overriding the earlier
  handoff plan of `30m`. This matches "not hogging RAM when not in use." A roll
  after a long idle pays a reload, which for the 3b model is a few seconds.
- **Unload seam:** ship the `unload_model` *function* only (tested, reusable).
  No `/api/documentary/unload` endpoint yet, to avoid dead API surface on a now
  public repo. Adding the endpoint plus one line of JS later is trivial.
- **Branch:** fold into `feat/attenborough-mode`. The toggle governs this
  feature's Ollama behavior and the `num_ctx`/`keep_alive` tweaks were already on
  the finalize list, so one comprehensive whole-branch review and one merge.

## User experience

The Documentary tab gains a small labeled switch in its header, near "Roll film":

> Live narration (Ollama)  [ off / on ]

- **Ollama reachable:** the switch is enabled. Its state is read from and written
  to `localStorage` (`tokmon.docLive`), defaulting to **off**. When on, rolls use
  Ollama; when off, rolls use the template engine.
- **Ollama unreachable:** the switch is shown disabled with a short hint
  ("requires a reachable Ollama host"), and the existing template-mode banner
  still appears. The switch cannot turn on what is not there.
- The footer credit line continues to report the engine actually used ("narrated
  by Ollama (llama3.2:3b)" or "narrated by the local template engine"), so if
  Ollama drops while the switch is on, the honest fallback is visible.

## Architecture

### `tokmon/documentary.py`

- **`render_ollama(brief, model, url)`** gains two payload changes:
  - `options.num_ctx = 4096` (bounds the KV cache; measured to keep the model
    GPU-resident rather than spilling).
  - top-level `keep_alive = "5m"` (self-evicting; overrides Ollama's default and
    the handoff's proposed 30m).
  Behavior is otherwise unchanged: returns text, or `None` on any failure, with
  em dashes stripped.
- **`unload_model(model, url=None) -> bool`** (new): POST to Ollama
  (`/api/generate`) with an empty prompt and `keep_alive: 0`, which asks Ollama to
  evict the model. Never raises (mirrors `ollama_status`); returns `True` on a
  clean call, `False` on any failure. Not called by the server or UI yet; it is
  the seam for future immediate-unload.
- **`narrate(...)`** is unchanged in logic. It already skips Ollama entirely when
  `engine="template"` and falls back to template when Ollama is unreachable for
  `engine` in `auto`/`ollama`. The toggle drives which `engine` value arrives.

### `tokmon/server.py`

- `GET /api/documentary?since=&host=&engine=` is unchanged; it already accepts and
  forwards `engine`. No new endpoint in this change.

### `web/index.html`

- Add the labeled switch to the Documentary pane header.
- On tab load (`loadDocumentary`), after fetching `/api/capabilities`:
  - If Ollama is available, enable the switch and reflect the stored state
    (default off).
  - If not, disable the switch, show the hint, and keep the existing banner.
- `rollFilm()` reads the switch and appends `&engine=ollama` when on or
  `&engine=template` when off to the `/api/documentary` request (today it sends
  no `engine`).
- Persist the switch state to `localStorage` on change.

## Data flow

1. Open Documentary tab -> `loadDocumentary()` -> `GET /api/capabilities`.
2. Render the switch: enabled + stored state if Ollama available, else disabled +
   hint + banner.
3. Click "Roll film" -> `rollFilm()` reads switch -> `GET /api/documentary?since
   =&host=&engine=ollama|template`.
4. Server `narrate()`:
   - `engine="template"`: template narration, zero Ollama contact, nothing loads.
   - `engine="ollama"`: use Ollama if reachable (loads model, num_ctx 4096,
     keep_alive 5m), else silent template fallback.
5. UI shows the narration text and the credit line for the engine actually used.
6. Turn the switch off: the next roll uses the template engine; the model
   self-evicts after ~5 minutes idle. (Future: an off handler could call
   `unload_model` to evict at once.)

## Error handling

- Ollama down while the switch is on: `render_ollama` returns `None`, `narrate`
  falls back to template, credit line reflects it. No error surfaced.
- `/api/capabilities` fails: treat Ollama as unavailable, disable the switch,
  show the banner.
- `unload_model` never raises; failures return `False` and are ignored.

## Configuration and dependencies

- No new Python dependencies (standard-library `urllib` for the unload call).
- Model default `llama3.2:3b` set at deploy time via `TOKMON_OLLAMA_MODEL`. For a
  remote (for example Pi) deployment, also set `TOKMON_OLLAMA_URL` to the capable
  host. Existing env overrides (`TOKMON_DOC_ENGINE`, `TOKMON_OLLAMA_URL`,
  `TOKMON_OLLAMA_MODEL`) are unchanged.

## Testing

- `render_ollama`: the posted payload includes `options.num_ctx == 4096` and
  top-level `keep_alive == "5m"` (assert against a mocked HTTP layer that captures
  the request body).
- `unload_model`: posts `keep_alive: 0`, returns `True` on success and `False`
  on a raised HTTP error, and never propagates an exception.
- `narrate(engine="template")`: makes zero Ollama calls (a mock wired to raise if
  `ollama_status`/`render_ollama`'s HTTP is touched confirms the template path is
  contact-free).
- Existing 57 tests continue to pass.
- Frontend verified live in the browser against demo data: switch persists across
  reloads; off yields the template credit; on yields "Ollama (llama3.2:3b)";
  disabled state shown when Ollama is stopped; no em dashes in output.

## Future work

- **Immediate unload on off.** Add `POST /api/documentary/unload` calling
  `unload_model`, and have the off handler (or a "stop narrating" affordance) hit
  it. The primitive already exists and is tested; this is a small, deliberate
  follow-up if perf pressure warrants it.
