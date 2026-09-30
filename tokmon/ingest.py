"""JSONL → DuckDB ingest. Incremental per-file via offset journal."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from . import config as cfg_mod
from .db import connect
from .schema import ToolCallRecord, TurnRecord, UserTurnRecord

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass
class IngestStats:
    files_scanned: int = 0
    files_with_new_data: int = 0
    new_turns: int = 0
    new_user_turns: int = 0
    new_tool_calls: int = 0
    malformed_lines: int = 0
    bytes_read: int = 0


def _project_label_from_path(path: str) -> str:
    """Last path component, splitting on both / and \\ so Windows paths
    (`C:\\Users\\you\\...\\memsync`) yield 'memsync', not the whole string.
    `os.path.basename` only understands the host OS's separator."""
    if not path:
        return "<unknown>"
    s = path.rstrip("/\\")
    last_sep = max(s.rfind("/"), s.rfind("\\"))
    return (s[last_sep + 1:] if last_sep >= 0 else s) or "<root>"


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0)
    s = value.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromtimestamp(0)


def _parse_assistant(
    obj: dict, source_file: str, source_offset: int, host: str
) -> TurnRecord | None:
    msg = obj.get("message") or {}
    uuid = obj.get("uuid")
    if not uuid:
        return None
    usage = msg.get("usage") or {}
    cache_creation = usage.get("cache_creation") or {}
    server_tool = usage.get("server_tool_use") or {}

    content = msg.get("content") or []
    tool_calls: list[ToolCallRecord] = []
    has_thinking = False
    thinking_chars = 0
    text_chars = 0
    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "thinking":
            has_thinking = True
            thinking_chars += len(block.get("thinking") or "")
        elif btype == "text":
            text_chars += len(block.get("text") or "")
        elif btype == "tool_use":
            raw_input = block.get("input")
            input_str = json.dumps(raw_input, sort_keys=True) if raw_input is not None else ""
            tool_calls.append(
                ToolCallRecord(
                    idx=idx,
                    name=block.get("name") or "<unknown>",
                    input_chars=len(input_str),
                    input_preview=input_str[:500],
                )
            )

    project_path = obj.get("cwd") or ""
    return TurnRecord(
        uuid=uuid,
        parent_uuid=obj.get("parentUuid"),
        request_id=obj.get("requestId"),
        session_id=obj.get("sessionId") or "<unknown>",
        project_path=project_path,
        project_label=_project_label_from_path(project_path),
        git_branch=obj.get("gitBranch"),
        model=msg.get("model") or "<unknown>",
        ts=_parse_ts(obj.get("timestamp")),
        is_sidechain=bool(obj.get("isSidechain")),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_write_5m=int(cache_creation.get("ephemeral_5m_input_tokens") or 0),
        cache_write_1h=int(cache_creation.get("ephemeral_1h_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        service_tier=usage.get("service_tier"),
        stop_reason=msg.get("stop_reason"),
        has_thinking=has_thinking,
        thinking_chars=thinking_chars,
        text_chars=text_chars,
        web_search_requests=int(server_tool.get("web_search_requests") or 0),
        web_fetch_requests=int(server_tool.get("web_fetch_requests") or 0),
        raw_usage=json.dumps(usage),
        source_file=source_file,
        source_offset=source_offset,
        host=host,
        tool_calls=tool_calls,
    )


def _is_human_prompt(obj: dict) -> bool:
    """Claude Code logs tool results, slash-command echoes, skill bodies and
    compaction summaries as type=user too. Only typed messages count."""
    if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isSidechain"):
        return False
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        head = content.lstrip()[:32]
        return not head.startswith(("<command-", "<local-command", "[Request interrupted"))
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_result" in types or not types:
            return False
        texts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if texts and all(t.lstrip().startswith("[Request interrupted") for t in texts):
            return False
        return True
    return False


def _parse_user(obj: dict, source_file: str) -> UserTurnRecord | None:
    uuid = obj.get("uuid")
    if not uuid:
        return None
    project_path = obj.get("cwd") or ""
    return UserTurnRecord(
        uuid=uuid,
        session_id=obj.get("sessionId") or "<unknown>",
        project_path=project_path,
        ts=_parse_ts(obj.get("timestamp")),
        source_file=source_file,
        is_prompt=_is_human_prompt(obj),
    )


_TURN_COLUMNS = (
    "uuid", "parent_uuid", "request_id", "session_id", "project_path",
    "project_label", "git_branch", "model", "ts", "is_sidechain",
    "input_tokens", "output_tokens", "cache_write_5m", "cache_write_1h",
    "cache_read", "service_tier", "stop_reason", "has_thinking",
    "thinking_chars", "text_chars", "web_search_requests",
    "web_fetch_requests", "raw_usage", "source_file", "source_offset", "host",
    "provider", "reasoning_tokens", "reasoning_effort",
)
_RATE_COLUMNS = (
    "provider", "host", "ts", "session_id", "limit_id", "plan_type",
    "window_name", "window_minutes", "used_percent", "resets_at",
    "reached_type", "source_file",
)


def _multi_insert(conn, table: str, columns: tuple[str, ...], rows: list[list],
                  chunk: int = 200) -> None:
    """Multi-row INSERT ... VALUES. No ON CONFLICT clause: duckdb 1.5's
    conflict-checking insert path costs ~30 ms/row on the turns table versus
    ~1 ms for a plain insert, so _BatchWriter filters out existing keys first."""
    cols = ", ".join(columns)
    one = "(" + ", ".join("?" for _ in columns) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES " + ", ".join(one for _ in part),
            [v for row in part for v in row],
        )


class _BatchWriter:
    """Buffers parsed records and writes them in multi-row statements.

    Row-at-a-time inserts dominated ingest time (a 3.8 GB Codex history is
    ~87k turns and ~86k tool calls). Each flush does one key lookup per table
    and a handful of multi-row inserts. Keys already in the DB, or repeated
    within the batch, are dropped: first occurrence wins, matching the old
    ON CONFLICT DO NOTHING semantics.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, flush_every: int = 1000):
        self.conn = conn
        self.flush_every = flush_every
        self.turns: dict[str, TurnRecord] = {}
        self.users: dict[str, UserTurnRecord] = {}
        self.rates: dict[tuple, dict] = {}
        self.new_turns = 0
        self.new_user_turns = 0
        self.new_tool_calls = 0

    def add(self, rec) -> None:
        if isinstance(rec, TurnRecord):
            self.turns.setdefault(rec.uuid, rec)
        elif isinstance(rec, UserTurnRecord):
            self.users.setdefault(rec.uuid, rec)
        elif isinstance(rec, dict):
            key = (rec["session_id"], rec["limit_id"], rec["window_name"], rec["ts"])
            self.rates.setdefault(key, rec)
        if len(self.turns) + len(self.users) + len(self.rates) >= self.flush_every:
            self.flush()

    def _existing(self, table: str, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        rows = self.conn.execute(
            f"SELECT uuid FROM {table} WHERE uuid IN (SELECT unnest(?::VARCHAR[]))",
            [keys],
        ).fetchall()
        return {r[0] for r in rows}

    def flush(self) -> None:
        conn = self.conn
        if self.turns:
            have = self._existing("turns", list(self.turns))
            fresh = [r for k, r in self.turns.items() if k not in have]
            _multi_insert(conn, "turns", _TURN_COLUMNS,
                          [[getattr(r, c) for c in _TURN_COLUMNS] for r in fresh])
            tool_rows = [
                [r.uuid, tc.idx, tc.name, tc.input_chars, tc.input_preview]
                for r in fresh for tc in r.tool_calls
            ]
            _multi_insert(conn, "tool_calls",
                          ("turn_uuid", "idx", "tool_name", "input_chars", "input_preview"),
                          tool_rows)
            self.new_turns += len(fresh)
            self.new_tool_calls += len(tool_rows)
            self.turns.clear()
        if self.users:
            have = self._existing("user_turns", list(self.users))
            fresh_u = [r for k, r in self.users.items() if k not in have]
            _multi_insert(conn, "user_turns",
                          ("uuid", "session_id", "project_path", "ts", "source_file",
                           "is_prompt"),
                          [[r.uuid, r.session_id, r.project_path, r.ts, r.source_file,
                            r.is_prompt]
                           for r in fresh_u])
            self.new_user_turns += len(fresh_u)
            self.users.clear()
        if self.rates:
            sessions = sorted({k[0] for k in self.rates})
            have_r = {
                tuple(r) for r in conn.execute(
                    """
                    SELECT session_id, limit_id, window_name, ts
                    FROM rate_limit_samples
                    WHERE session_id IN (SELECT unnest(?::VARCHAR[]))
                    """,
                    [sessions],
                ).fetchall()
            }
            _multi_insert(conn, "rate_limit_samples", _RATE_COLUMNS,
                          [[r[c] for c in _RATE_COLUMNS]
                           for k, r in self.rates.items() if k not in have_r])
            self.rates.clear()


def _iter_jsonl_lines(
    fh, start_offset: int
) -> Iterable[tuple[bytes, int, int]]:
    """Yield (line_bytes, offset_at_start_of_line, new_offset).

    A final line with no trailing newline is only yielded if it already parses
    as JSON. Otherwise it is probably still being written, and consuming it now
    would record the half-line as malformed and move the offset past it, losing
    the record for good once the writer finishes it.
    """
    fh.seek(start_offset)
    offset = start_offset
    for line in fh:
        if not line.endswith(b"\n"):
            try:
                json.loads(line)
            except ValueError:
                return
        line_start = offset
        offset += len(line)
        yield line, line_start, offset


class _ClaudeReader:
    """Claude Code transcripts are self-describing: every record carries its
    own session, cwd and model, so there is no state to carry between runs."""

    provider = "claude"
    state = None

    def __init__(self, source_file: str, host: str, state: dict | None = None):
        self.source_file = source_file
        self.host = host

    @staticmethod
    def wants(raw_line: bytes) -> bool:
        return True

    def feed(self, obj: dict, line_start: int) -> list:
        otype = obj.get("type")
        if otype == "assistant":
            rec = _parse_assistant(obj, self.source_file, line_start, self.host)
            return [rec] if rec is not None else []
        if otype == "user":
            rec_u = _parse_user(obj, self.source_file)
            return [rec_u] if rec_u is not None else []
        return []


def _reader_class(provider: str):
    if provider == "codex":
        from .ingest_codex import CodexReader
        return CodexReader
    return _ClaudeReader


def _ingest_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: Path,
    stats: IngestStats,
    host: str = "local",
    provider: str = "claude",
) -> None:
    source_file = str(file_path)
    try:
        st = file_path.stat()
        mtime, size = st.st_mtime, st.st_size
    except FileNotFoundError:
        return

    row = conn.execute(
        "SELECT mtime, last_offset, malformed_lines FROM ingest_log WHERE source_file = ?",
        [source_file],
    ).fetchone()
    last_offset = 0
    malformed_total = 0
    if row is not None:
        prev_mtime, last_offset, malformed_total = row
        if last_offset > size:
            # Truncated/rewritten: start over, parser state included.
            last_offset = 0
            malformed_total = 0

    if last_offset >= size:
        return

    reader_cls = _reader_class(provider)
    state = None
    if last_offset > 0 and reader_cls is not _ClaudeReader:
        srow = conn.execute(
            "SELECT state FROM ingest_state WHERE source_file = ?", [source_file]
        ).fetchone()
        if srow is not None:
            state = json.loads(srow[0])
        else:
            # Offset without state: re-read from the top. Inserts are
            # idempotent, so this only costs time.
            last_offset = 0
    reader = reader_cls(source_file, host, state)

    stats.files_scanned += 1
    writer = _BatchWriter(conn)
    malformed_local = 0
    bytes_consumed = 0

    with open(file_path, "rb") as fh:
        for raw_line, line_start, new_offset in _iter_jsonl_lines(fh, last_offset):
            bytes_consumed = new_offset - last_offset
            if not reader.wants(raw_line):
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed_local += 1
                continue
            if not isinstance(obj, dict):
                malformed_local += 1
                continue
            for rec in reader.feed(obj, line_start):
                writer.add(rec)
    writer.flush()
    new_turns_local = writer.new_turns
    new_user_turns_local = writer.new_user_turns
    new_tool_calls_local = writer.new_tool_calls

    new_offset = last_offset + bytes_consumed
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    conn.execute(
        """
        INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (source_file) DO UPDATE
        SET mtime = excluded.mtime,
            last_offset = excluded.last_offset,
            last_ingested_at = excluded.last_ingested_at,
            malformed_lines = excluded.malformed_lines
        """,
        [source_file, mtime, new_offset, now_utc, malformed_total + malformed_local],
    )
    if reader.state is not None:
        conn.execute(
            """
            INSERT INTO ingest_state VALUES (?, ?)
            ON CONFLICT (source_file) DO UPDATE SET state = excluded.state
            """,
            [source_file, json.dumps(reader.state)],
        )

    if new_turns_local or new_user_turns_local or malformed_local:
        stats.files_with_new_data += 1
    stats.new_turns += new_turns_local
    stats.new_user_turns += new_user_turns_local
    stats.new_tool_calls += new_tool_calls_local
    stats.malformed_lines += malformed_local
    stats.bytes_read += bytes_consumed


def _iter_root_files(root_path: Path, provider: str) -> Iterable[Path]:
    if provider == "codex":
        from .ingest_codex import iter_rollout_files
        return iter_rollout_files(root_path)
    return sorted(root_path.rglob("*.jsonl"))


Root = tuple  # (path, host) or (path, host, provider)


def _normalize_root(root: Root) -> tuple[Path, str, str]:
    if len(root) == 3:
        return Path(root[0]), root[1], root[2]
    return Path(root[0]), root[1], "claude"


def _resolve_roots(
    roots: list[Root] | None,
    projects_dir: Path | None,
) -> list[tuple[Path, str, str]]:
    """Caller-provided roots win; else legacy single-dir path; else config."""
    if roots is not None:
        return [_normalize_root(r) for r in roots]
    if projects_dir is not None:
        return [(projects_dir, "local", "claude")]
    cfg = cfg_mod.load()
    return list(cfg_mod.iter_roots(cfg.all_roots()))


def incremental(
    conn: duckdb.DuckDBPyConnection | None = None,
    projects_dir: Path | None = None,
    roots: list[Root] | None = None,
) -> IngestStats:
    """Scan one or more project roots and ingest anything new.

    Resolution order: explicit `roots` > legacy single `projects_dir` > config.
    """
    own_conn = False
    if conn is None:
        conn = connect()
        own_conn = True
    resolved_roots = _resolve_roots(roots, projects_dir)
    stats = IngestStats()
    if not resolved_roots:
        if own_conn:
            conn.close()
        return stats
    before_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    before_user = conn.execute("SELECT COUNT(*) FROM user_turns").fetchone()[0]
    before_tools = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    # Per-file transactions so DuckDB doesn't hold a single huge WAL in memory
    # (the Pi has only ~6 GiB and a 10k-turn full ingest OOM'd as one big txn).
    # A file failing rolls back only that file's inserts; ingest_log isn't
    # updated for it, so the next run retries from the same offset.
    file_count = 0
    for root_path, host_label, provider in resolved_roots:
        if not root_path.exists():
            continue
        for f in _iter_root_files(root_path, provider):
            conn.execute("BEGIN")
            try:
                _ingest_file(conn, f, stats, host=host_label, provider=provider)
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                print(f"[tokmon] error on {f}: {e}", file=__import__('sys').stderr)
                # continue with other files rather than abort the whole run
            file_count += 1
            # Force flush every 10 files so DuckDB drops its in-memory page
            # cache. Without this the WAL grows unbounded on low-RAM hosts.
            if file_count % 10 == 0:
                conn.execute("CHECKPOINT")
    conn.execute("CHECKPOINT")
    try:
        from .analytics import refresh_cache
        if refresh_cache(conn):
            conn.execute("CHECKPOINT")
    except Exception as e:  # the dashboard falls back to live views
        print(f"[tokmon] cache refresh failed: {e}", file=__import__('sys').stderr)
    after_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    after_user = conn.execute("SELECT COUNT(*) FROM user_turns").fetchone()[0]
    after_tools = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    stats.new_turns = after_turns - before_turns
    stats.new_user_turns = after_user - before_user
    stats.new_tool_calls = after_tools - before_tools
    if own_conn:
        conn.close()
    return stats


def full(
    projects_dir: Path | None = None,
    roots: list[Root] | None = None,
) -> IngestStats:
    """Wipe DB and re-ingest everything."""
    from .db import reset
    reset()
    return incremental(projects_dir=projects_dir, roots=roots)
