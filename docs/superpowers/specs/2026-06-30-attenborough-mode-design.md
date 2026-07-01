# Attenborough Mode design

Date: 2026-06-30
Status: Approved, ready for implementation planning

## Summary

A new **Documentary** tab in the tokmon dashboard. On demand, tokmon turns the
currently selected time window into a short David-Attenborough-style nature
documentary about the developer's own coding day, observed through their token
usage. Output is text only, styled like letterboxed subtitles.

Narration is generated locally. If Ollama is running on the machine, tokmon uses
it for rich prose. If not, tokmon falls back to a built-in template engine so the
feature still works for everyone who clones the repo. Nothing leaves the machine
in either case, which preserves the project's core promise.

## Motivation

tokmon already leans into playful derived metrics (Toy Energy in kWh, Carbon
Confetti in grams, Reading Time in airport novels, Achievements). Attenborough
Mode extends that personality with a narrative surface that is cool, shareable,
and gloriously unnecessary. It also forces a reusable pattern for degrading
gracefully when an optional dependency is missing, which future features will
reuse.

## Goals

- A Documentary tab that narrates the selected host and date window.
- Tiered, fully local narration: Ollama when present, template fallback when not.
- Never break, error out, or show a blank tab when a dependency is missing.
- A reusable capability-gate pattern for this and future optional features.
- Generated prose never contains em dashes.

## Non-goals (explicitly out of scope for v1)

- Spoken audio or TTS. Voice is a v2 add-on and the architecture leaves room for
  it, but v1 is text only.
- The Pure Spectacle features (The Furnace, Token Weather, Sankey of Regret) and
  the Receipt Printer. Each gets its own spec.
- Scheduled or emailed delivery. Narration is on demand.
- Multi-language narration.

## User experience

The Documentary tab has four states.

1. **Ready.** A cinematic dark panel, a "Roll film" button, and a line stating
   the window it will narrate (for example, "narrating: all hosts, last 30 days").
2. **Filming.** A loading state while narration generates ("Observing the
   specimen...").
3. **Narrated.** The script rendered as centered serif subtitles in a letterboxed
   panel, with a quiet footer crediting the engine: "narrated by Ollama
   (<model>)" or "narrated by the local template engine."
4. **Empty window.** If the selected range has no turns, a gentle "Nothing stirred
   in this period" message rather than an error.

### Graceful degradation (the capability gate)

Because Attenborough Mode has a working template fallback, a user without Ollama
still gets a full documentary. In that case the tab also shows a small,
dismissible upgrade banner above the script:

> You are in template mode. Install Ollama for richer narration.
> [ copy command ]  [ Check again ]

- "copy command" copies the one-line install or run hint to the clipboard.
- "Check again" re-probes Ollama over the API and re-renders without a page
  reload, so the user can start Ollama and immediately upgrade.

This banner is one variant of a **reusable capability gate** used across the
dashboard:

- **Feature has a fallback** (Attenborough): run the fallback, show the
  dismissible upgrade banner.
- **Feature has no fallback** (future features): render a friendly empty-state
  card inside the tab container instead of the feature. The card states what the
  feature does, what it needs, a CTA button (copy the exact command or open the
  docs), and a "Check again" button. The tab is never blank, never an error dump.

## Architecture

### New module: `tokmon/documentary.py`

Self-contained narration core, independent of the web layer.

- `ollama_status(url) -> dict`: probe the Ollama API (`GET /api/tags`) with a short
  timeout. Returns `{available: bool, url: str, models: list[str], model: str|None}`
  where `model` is the configured model if present, else the first available.
- `build_brief(conn, since, host) -> DocBrief`: assemble structured facts from the
  existing analytics helpers. Fields:
  - `window`: since label, host filter, resolved start and end timestamps.
  - `totals`: turns, sessions, projects, total_usd, total_tokens.
  - `dominant_model`: model, usd, share of spend.
  - `busiest_project`: label, usd, turns.
  - `biggest_turn`: model, project, usd, timestamp, tokens (the single most
    expensive turn in the window).
  - `nocturnal`: count of off-hours sessions, count of night turns, latest active
    hour.
  - `economics`: cache dollars saved, cache share percent, burn rate per hour,
    projected month-end spend.
  - `top_tool`: name and call count.
  - `streak`: current daily streak length.
  These come from the existing analytics functions (summary, spend_by, top_turns,
  cache savings, monthly forecast, burn rate, achievements, and the off-hours and
  hour-of-day rollups). The implementation plan pins the exact function names.
- `render_template(brief, seed) -> str`: fill Attenborough phrase-banks, one
  selection per beat, seeded by the window so output is stable within a day and
  varies across days. Always available. The phrase-banks contain no em dashes.
- `render_ollama(brief, model, url) -> str | None`: post the brief to Ollama with
  a narrator system prompt. Returns text, or None on any failure (unreachable,
  timeout, bad response).
- `narrate(conn, since, host, engine="auto") -> dict`: orchestrate. For
  `engine="auto"`, try Ollama if reachable, else template. Returns
  `{text: str, engine: "ollama"|"template", model: str|None}`.

### Server endpoints (`tokmon/server.py`)

- `GET /api/documentary?since=&host=&engine=`: returns the narration payload from
  `narrate(...)`. Uses a read-only analytics connection.
- `GET /api/capabilities`: returns `{ollama: {available, url, model}}` for the
  frontend capability gate. Extensible: future features add keys here.

Ollama is called with the Python standard library (`urllib`), so there are **no
new Python dependencies**.

### Frontend (`web/index.html`)

- A "Documentary" nav button beside the existing tabs.
- A pane that reuses the existing tab, fetch, and host/date filter machinery.
- The four states above, plus the capability banner helper.
- A small, reusable `renderCapabilityCard` / `renderUpgradeBanner` helper and CSS
  so future features share the empty-state and "Check again" behavior.

## The narration

Six beats, consumed identically by both engines:

1. **Establishing shot.** The habitat: the window, total turns and sessions,
   number of projects.
2. **The subject.** The dominant model and the busiest project.
3. **The hunt.** The single most expensive turn, framed as a feeding frenzy.
4. **Nocturnal behavior.** Off-hours or late-night sessions, if any. Skipped
   gracefully when there are none.
5. **The reckoning.** Total cost, cache savings, and the month-end forecast.
6. **Sign-off.** A closing line.

### Ollama prompt shape

- System prompt: establish the narrator voice (wry, affectionate, understated,
  present tense), refer to the developer as "the developer" or "our subject",
  produce five to seven short paragraphs, weave the facts in rather than listing
  them, and **never use em dashes**.
- User message: the brief as compact facts.

### Template engine

Per-beat phrase-banks with slots filled from the brief. A seeded RNG (seed derived
from the window) selects one line per beat for stable-but-varying output. The
banks are authored without em dashes.

## Error handling

- Ollama down, slow, or missing the model: catch, fall back to template, mark
  `engine: "template"`. The tab never errors.
- Empty window: return an empty-window marker; the frontend shows the gentle
  "Nothing stirred" state.
- Probe timeouts are short so the tab stays responsive.

## Configuration and dependencies

- No new Python dependencies (standard library for the Ollama HTTP call).
- Environment overrides:
  - `TOKMON_DOC_ENGINE` = `auto` (default), `ollama`, or `template`.
  - `TOKMON_OLLAMA_URL` (default `http://127.0.0.1:11434`).
  - `TOKMON_OLLAMA_MODEL` (default: the configured model if available, else the
    first model Ollama reports).

## Testing

- `build_brief` against the synthetic fixture returns the expected facts.
- `render_template` produces non-empty text that hits every applicable beat and
  contains no em dashes.
- `render_ollama` is tested with a mocked HTTP layer for both success and failure,
  and `narrate` falls back to template on failure.
- `GET /api/documentary` returns 200 with a narration payload and an `engine`
  field.
- `GET /api/capabilities` reports Ollama status.
- Frontend states are verified manually against the demo data.

## Future work

- **Voice (v2).** Same architecture: narration text feeds a pluggable voice
  output. Start with the browser Web Speech API, then optionally local Piper TTS.
- **Reuse the capability gate** for the Pure Spectacle features and the Receipt
  Printer, each of which declares its own required capability.
- **Share card.** A generated image of a documentary still with a subtitle line,
  for posting.

## Addendum (2026-06-30): remote Ollama, never local on the Pi

Deployment note: the dashboard host (a Raspberry Pi) must never run Ollama
locally. Instead it polls a remote Ollama on a capable machine via
`TOKMON_OLLAMA_URL`, with the template engine as the fallback when that machine
is unreachable. Generation is bounded (`num_predict`) so it completes within the
request timeout. UI copy must never instruct a user to install Ollama on the
dashboard host.
