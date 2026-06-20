"""JSONL → DuckDB ingest. Incremental per-file via offset journal."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
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
    if not path:
        return "<unknown>"
    return os.path.basename(path.rstrip("/")) or "<root>"


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
    )


def _iter_jsonl_lines(
    fh, start_offset: int
) -> Iterable[tuple[bytes, int, int]]:
    """Yield (line_bytes, offset_at_start_of_line, new_offset)."""
    fh.seek(start_offset)
    offset = start_offset
    for line in fh:
        line_start = offset
        offset += len(line)
        yield line, line_start, offset


def _ingest_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: Path,
    stats: IngestStats,
    host: str = "local",
) -> None:
    source_file = str(file_path)
    try:
        mtime = file_path.stat().st_mtime
        size = file_path.stat().st_size
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
            last_offset = 0
            malformed_total = 0

    if last_offset >= size:
        return

    stats.files_scanned += 1
    new_turns_local = 0
    new_user_turns_local = 0
    new_tool_calls_local = 0
    malformed_local = 0
    bytes_consumed = 0

    with open(file_path, "rb") as fh:
        for raw_line, line_start, new_offset in _iter_jsonl_lines(fh, last_offset):
            bytes_consumed = new_offset - last_offset
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed_local += 1
                continue
            otype = obj.get("type")
            if otype == "assistant":
                rec = _parse_assistant(obj, source_file, line_start, host)
                if rec is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO turns VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT (uuid) DO NOTHING
                    """,
                    [
                        rec.uuid, rec.parent_uuid, rec.request_id, rec.session_id,
                        rec.project_path, rec.project_label, rec.git_branch, rec.model,
                        rec.ts, rec.is_sidechain, rec.input_tokens, rec.output_tokens,
                        rec.cache_write_5m, rec.cache_write_1h, rec.cache_read,
                        rec.service_tier, rec.stop_reason, rec.has_thinking,
                        rec.thinking_chars, rec.text_chars,
                        rec.web_search_requests, rec.web_fetch_requests,
                        rec.raw_usage, rec.source_file, rec.source_offset,
                        rec.host,
                    ],
                )
                new_turns_local += 1
                for tc in rec.tool_calls:
                    conn.execute(
                        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)",
                        [rec.uuid, tc.idx, tc.name, tc.input_chars, tc.input_preview],
                    )
                    new_tool_calls_local += 1
            elif otype == "user":
                rec_u = _parse_user(obj, source_file)
                if rec_u is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO user_turns VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (uuid) DO NOTHING
                    """,
                    [rec_u.uuid, rec_u.session_id, rec_u.project_path, rec_u.ts, rec_u.source_file],
                )
                new_user_turns_local += 1

    new_offset = last_offset + bytes_consumed if bytes_consumed else size
    conn.execute(
        """
        INSERT INTO ingest_log VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT (source_file) DO UPDATE
        SET mtime = excluded.mtime,
            last_offset = excluded.last_offset,
            last_ingested_at = excluded.last_ingested_at,
            malformed_lines = excluded.malformed_lines
        """,
        [source_file, mtime, new_offset, malformed_total + malformed_local],
    )

    if new_turns_local or new_user_turns_local or malformed_local:
        stats.files_with_new_data += 1
    stats.new_turns += new_turns_local
    stats.new_user_turns += new_user_turns_local
    stats.new_tool_calls += new_tool_calls_local
    stats.malformed_lines += malformed_local
    stats.bytes_read += bytes_consumed


def _resolve_roots(
    roots: list[tuple[Path, str]] | None,
    projects_dir: Path | None,
) -> list[tuple[Path, str]]:
    """Caller-provided roots win; else legacy single-dir path; else config."""
    if roots is not None:
        return roots
    if projects_dir is not None:
        return [(projects_dir, "local")]
    cfg = cfg_mod.load()
    return list(cfg_mod.iter_roots(cfg.all_roots()))


def incremental(
    conn: duckdb.DuckDBPyConnection | None = None,
    projects_dir: Path | None = None,
    roots: list[tuple[Path, str]] | None = None,
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
    for root_path, host_label in resolved_roots:
        if not root_path.exists():
            continue
        for f in sorted(root_path.rglob("*.jsonl")):
            conn.execute("BEGIN")
            try:
                _ingest_file(conn, f, stats, host=host_label)
                conn.execute("COMMIT")
            except Exception as e:
                conn.execute("ROLLBACK")
                print(f"[tokmon] error on {f}: {e}", file=__import__('sys').stderr)
                # continue with other files rather than abort the whole run
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
    roots: list[tuple[Path, str]] | None = None,
) -> IngestStats:
    """Wipe DB and re-ingest everything."""
    from .db import reset
    reset()
    return incremental(projects_dir=projects_dir, roots=roots)
