"""Codex rollout JSONL -> turn records.

Codex (CLI, Desktop, IDE extension) writes one rollout file per thread under
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, moved to ~/.codex/archived_sessions
when a thread is archived. Unlike Claude Code transcripts, a usage record there
doesn't carry its own context: model, cwd and session identity arrive in
earlier header records (session_meta, turn_context). The reader therefore keeps
a small state dict that is persisted between incremental runs.

Usage comes from two record kinds, depending on CLI version:

- ``token_usage_record`` (newer): one per model response, keyed by response_id.
- ``event_msg/token_count`` (all versions): a running thread total plus the
  last response's usage. It is emitted more than once per response and, in
  files that also carry token_usage_records, occasionally drops a response.

Once a file has shown a token_usage_record, token_count usage is ignored for
the rest of that file. Before that, token_count is deduped on the thread's
cumulative total. token_count still feeds the rate-limit meter either way.

Token accounting follows the OpenAI API: ``input_tokens`` includes cached
input and ``output_tokens`` includes reasoning. tokmon stores uncached input,
so cached (and cache-write) tokens are subtracted out, and reasoning is kept
as a separate subset column.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .schema import ToolCallRecord, TurnRecord, UserTurnRecord

PROVIDER = "codex"

# Record kinds worth parsing. Anything else (encrypted compaction blobs,
# world_state snapshots, tool outputs) is skipped on a cheap prefix check,
# which matters with multi-GB session directories.
_WANTED_TYPES = frozenset({
    b"session_meta", b"turn_context", b"token_usage_record", b"token_count",
    b"task_started", b"thread_settings_applied", b"function_call",
    b"custom_tool_call", b"local_shell_call", b"web_search_call",
    b"image_generation_call", b"tool_search_call", b"reasoning", b"message",
})
_TYPE_RE = re.compile(rb'"type"\s*:\s*"([a-z_]+)"')
_ASSISTANT_RE = re.compile(rb'"role"\s*:\s*"assistant"')
# A larger head than the ~120 bytes the markers need covers a long timestamp
# or reordered keys without scanning megabyte lines.
_HEAD_BYTES = 400

# The Codex "exec" custom tool takes a JS snippet that calls the real tools,
# e.g. `await tools.exec_command({...})`. Credit the inner tools.
_INNER_TOOL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")

_BUILTIN_CALLS = {
    "local_shell_call": "local_shell",
    "web_search_call": "web_search",
    "image_generation_call": "image_generation",
    "tool_search_call": "tool_search",
}


def iter_rollout_files(root: Path) -> Iterator[Path]:
    """Rollout files under a Codex home (~/.codex) or a sessions dir itself.

    Only sessions/ and archived_sessions/ are scanned when present, so plugin
    fixtures and other stray .jsonl under ~/.codex/.tmp are never mistaken
    for usage.
    """
    subdirs = [root / "sessions", root / "archived_sessions"]
    present = [d for d in subdirs if d.is_dir()]
    if present:
        for d in present:
            yield from sorted(d.rglob("rollout-*.jsonl"))
    else:
        yield from sorted(root.rglob("rollout-*.jsonl"))


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0)
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return datetime.fromtimestamp(0)


def _utc_from_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _project_label(path: str) -> str:
    from .ingest import _project_label_from_path
    return _project_label_from_path(path)


def _fresh_state(source_file: str) -> dict:
    stem = Path(source_file).stem
    return {
        "thread_id": stem,
        "session_id": stem,
        "is_sidechain": False,
        "cwd": "",
        "git_branch": None,
        "model": "<unknown>",
        "effort": None,
        "service_tier": None,
        "turn_id": None,
        "has_records": False,
        "last_total": None,
        "pending": _empty_pending(),
        "rl_last": {},
    }


def _empty_pending() -> dict:
    return {
        "tools": [],          # [name, input_chars, preview]
        "reasoning": False,
        "thinking_chars": 0,
        "text_chars": 0,
        "web_search": 0,
    }


class CodexReader:
    """Stateful line consumer for one rollout file."""

    provider = PROVIDER

    def __init__(self, source_file: str, host: str, state: dict | None = None):
        self.source_file = source_file
        self.host = host
        self.state = state or _fresh_state(source_file)

    @staticmethod
    def wants(raw_line: bytes) -> bool:
        # Both the record type and the payload type sit in the first ~120
        # bytes; whitespace-tolerant so re-serialized logs still match.
        head = raw_line[:_HEAD_BYTES]
        types = _TYPE_RE.findall(head)
        if b"message" in types:
            # Developer/user messages can be huge (injected instructions) and
            # carry nothing tokmon counts; only assistant text is measured.
            return _ASSISTANT_RE.search(head) is not None
        return any(t in _WANTED_TYPES for t in types)

    # -- dispatch ---------------------------------------------------------

    def feed(self, obj: dict, line_start: int) -> list:
        """Consume one parsed record; return any TurnRecord /
        UserTurnRecord / rate-limit sample dicts it produced."""
        rtype = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return []
        ptype = payload.get("type")
        ts = _parse_ts(obj.get("timestamp"))

        if rtype == "session_meta":
            self._on_session_meta(payload)
        elif rtype == "turn_context":
            self._on_turn_context(payload)
        elif rtype == "token_usage_record":
            self.state["has_records"] = True
            return self._on_usage_record(payload, ts, line_start)
        elif rtype == "event_msg":
            if ptype == "token_count":
                return self._on_token_count(payload, ts, line_start)
            if ptype == "task_started":
                # One task per submitted prompt (or automation wake-up). Newer
                # Codex versions stopped logging user_message events, so this
                # is the only reliable per-prompt marker across versions.
                self.state["turn_id"] = payload.get("turn_id") or self.state["turn_id"]
                return [self._user_turn(ts)]
            if ptype == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                if settings.get("model"):
                    self.state["model"] = settings["model"]
                if settings.get("service_tier"):
                    self.state["service_tier"] = settings["service_tier"]
        elif rtype == "response_item":
            self._on_response_item(payload)
        return []

    # -- context records --------------------------------------------------

    def _on_session_meta(self, p: dict) -> None:
        st = self.state
        thread_id = p.get("id") or st["thread_id"]
        st["thread_id"] = thread_id
        # session_id is the root of a spawn tree; subagent threads share it,
        # the same way Claude Code sidechains share their parent's sessionId.
        st["session_id"] = p.get("session_id") or p.get("parent_thread_id") or thread_id
        source = p.get("source")
        st["is_sidechain"] = isinstance(source, dict) and "subagent" in source
        if p.get("cwd"):
            st["cwd"] = p["cwd"]
        if p.get("model"):
            st["model"] = p["model"]
        elif p.get("thread_source") == "guardian_review" and st["model"] == "<unknown>":
            # Auto-review threads can bill a compaction before their first
            # turn_context names the model.
            st["model"] = "codex-auto-review"
        git = p.get("git") or {}
        if isinstance(git, dict) and git.get("branch"):
            st["git_branch"] = git["branch"]

    def _on_turn_context(self, p: dict) -> None:
        st = self.state
        if p.get("model"):
            st["model"] = p["model"]
        if p.get("cwd"):
            st["cwd"] = p["cwd"]
        st["effort"] = p.get("effort") or p.get("reasoning_effort") or st["effort"]
        if p.get("turn_id"):
            st["turn_id"] = p["turn_id"]

    def _on_response_item(self, p: dict) -> None:
        pend = self.state["pending"]
        ptype = p.get("type")
        if ptype == "reasoning":
            pend["reasoning"] = True
            for part in (p.get("summary") or []):
                if isinstance(part, dict):
                    pend["thinking_chars"] += len(part.get("text") or "")
            for part in (p.get("content") or []):
                if isinstance(part, dict):
                    pend["thinking_chars"] += len(part.get("text") or "")
        elif ptype == "message":
            if p.get("role") != "assistant":
                return
            for part in (p.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    pend["text_chars"] += len(part.get("text") or "")
        elif ptype == "function_call":
            args = p.get("arguments") or ""
            self._add_tool(p.get("name") or "<unknown>", args)
        elif ptype == "custom_tool_call":
            name = p.get("name") or "<unknown>"
            body = p.get("input") or ""
            inner = _INNER_TOOL_RE.findall(body) if name == "exec" else []
            if inner:
                share = len(body) // len(inner)
                for i, inner_name in enumerate(inner):
                    self._add_tool(inner_name, body if i == 0 else "", chars=share)
            else:
                self._add_tool(name, body)
        elif ptype in _BUILTIN_CALLS:
            if ptype == "web_search_call":
                pend["web_search"] += 1
            action = p.get("action")
            self._add_tool(_BUILTIN_CALLS[ptype],
                           json.dumps(action, sort_keys=True) if action else "")

    def _add_tool(self, name: str, body, chars: int | None = None) -> None:
        text = body if isinstance(body, str) else json.dumps(body, sort_keys=True)
        self.state["pending"]["tools"].append(
            [name, len(text) if chars is None else chars, text[:500]]
        )

    # -- usage ------------------------------------------------------------

    def _on_usage_record(self, p: dict, ts: datetime, line_start: int) -> list:
        usage = p.get("usage") or {}
        total = (p.get("thread_token_usage") or {}).get("total_tokens")
        response_id = p.get("response_id")
        key = response_id or f"{self.state['thread_id']}:{total}"
        if p.get("turn_id"):
            self.state["turn_id"] = p["turn_id"]
        self.state["last_total"] = total
        return [self._turn(key, response_id, usage, ts, line_start)]

    def _on_token_count(self, p: dict, ts: datetime, line_start: int) -> list:
        out: list = list(self._rate_limit_samples(p.get("rate_limits"), ts))
        info = p.get("info")
        if self.state["has_records"] or not isinstance(info, dict):
            return out
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        last = info.get("last_token_usage") or {}
        # token_count fires more than once per response with the same running
        # total; only a moved total is a new response.
        if total is None or total == self.state["last_total"] or not last:
            return out
        self.state["last_total"] = total
        out.append(self._turn(f"{self.state['thread_id']}:{total}", None, last, ts, line_start))
        return out

    def _turn(self, key: str, response_id: str | None, usage: dict,
              ts: datetime, line_start: int) -> TurnRecord:
        st = self.state
        pend = st["pending"]
        cached = int(usage.get("cached_input_tokens") or 0)
        cache_write = int(usage.get("cache_write_input_tokens") or 0)
        raw_input = int(usage.get("input_tokens") or 0)
        tools = [
            ToolCallRecord(idx=i, name=name, input_chars=chars, input_preview=preview)
            for i, (name, chars, preview) in enumerate(pend["tools"])
        ]
        rec = TurnRecord(
            uuid=f"codex:{key}",
            parent_uuid=st["turn_id"],
            request_id=f"codex:{response_id or key}",
            session_id=st["session_id"],
            project_path=st["cwd"],
            project_label=_project_label(st["cwd"]),
            git_branch=st["git_branch"],
            model=st["model"] or "<unknown>",
            ts=ts,
            is_sidechain=st["is_sidechain"],
            input_tokens=max(0, raw_input - cached - cache_write),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_5m=cache_write,
            cache_write_1h=0,
            cache_read=cached,
            service_tier=st["service_tier"],
            stop_reason=None,
            has_thinking=pend["reasoning"],
            thinking_chars=pend["thinking_chars"],
            text_chars=pend["text_chars"],
            web_search_requests=pend["web_search"],
            web_fetch_requests=0,
            raw_usage=json.dumps(usage),
            source_file=self.source_file,
            source_offset=line_start,
            host=self.host,
            provider=PROVIDER,
            reasoning_tokens=int(usage.get("reasoning_output_tokens") or 0),
            reasoning_effort=st["effort"],
            tool_calls=tools,
        )
        st["pending"] = _empty_pending()
        return rec

    def _user_turn(self, ts: datetime) -> UserTurnRecord:
        st = self.state
        return UserTurnRecord(
            # Content-keyed so the same file read from sessions/ and later from
            # archived_sessions/ collapses to one row.
            uuid=f"codex-user:{st['thread_id']}:{st['turn_id'] or ts.isoformat()}",
            session_id=st["session_id"],
            project_path=st["cwd"],
            ts=ts,
            source_file=self.source_file,
            # Subagent and auto-review threads start tasks too; only the root
            # thread's tasks come from a person (or their own automation).
            is_prompt=not st["is_sidechain"],
        )

    # -- rate limits ------------------------------------------------------

    def _rate_limit_samples(self, rl, ts: datetime) -> Iterator[dict]:
        if not isinstance(rl, dict):
            return
        limit_id = rl.get("limit_id") or "codex"
        for window_name in ("primary", "secondary"):
            w = rl.get(window_name)
            if not isinstance(w, dict) or w.get("used_percent") is None:
                continue
            resets_at = _utc_from_epoch(w.get("resets_at"))
            if resets_at is None and w.get("resets_in_seconds") is not None:
                resets_at = ts + timedelta(seconds=float(w["resets_in_seconds"]))
            used = float(w["used_percent"])
            reached = rl.get("rate_limit_reached_type")
            # Keep change points only: token_count repeats the same meter
            # reading dozens of times per turn.
            reset_key = int(resets_at.timestamp() // 60) if resets_at else None
            sig = [used, reset_key, reached, rl.get("plan_type")]
            k = f"{limit_id}/{window_name}"
            if self.state["rl_last"].get(k) == sig:
                continue
            self.state["rl_last"][k] = sig
            yield {
                "provider": PROVIDER,
                "host": self.host,
                "ts": ts,
                "session_id": self.state["thread_id"],
                "limit_id": limit_id,
                "plan_type": rl.get("plan_type"),
                "window_name": window_name,
                "window_minutes": w.get("window_minutes"),
                "used_percent": used,
                "resets_at": resets_at,
                "reached_type": reached,
                "source_file": self.source_file,
            }
