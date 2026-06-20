"""DuckDB connection, schema, and views."""

from __future__ import annotations

import os
import socket

import duckdb
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".tokmon" / "tokmon.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turns (
    uuid              VARCHAR PRIMARY KEY,
    parent_uuid       VARCHAR,
    request_id        VARCHAR,
    session_id        VARCHAR NOT NULL,
    project_path      VARCHAR NOT NULL,
    project_label     VARCHAR NOT NULL,
    git_branch        VARCHAR,
    model             VARCHAR NOT NULL,
    ts                TIMESTAMP NOT NULL,
    is_sidechain      BOOLEAN NOT NULL DEFAULT FALSE,
    input_tokens      BIGINT NOT NULL DEFAULT 0,
    output_tokens     BIGINT NOT NULL DEFAULT 0,
    cache_write_5m    BIGINT NOT NULL DEFAULT 0,
    cache_write_1h    BIGINT NOT NULL DEFAULT 0,
    cache_read        BIGINT NOT NULL DEFAULT 0,
    service_tier      VARCHAR,
    stop_reason       VARCHAR,
    has_thinking      BOOLEAN NOT NULL DEFAULT FALSE,
    thinking_chars    BIGINT NOT NULL DEFAULT 0,
    text_chars        BIGINT NOT NULL DEFAULT 0,
    web_search_requests INT NOT NULL DEFAULT 0,
    web_fetch_requests  INT NOT NULL DEFAULT 0,
    raw_usage         VARCHAR,
    source_file       VARCHAR NOT NULL,
    source_offset     BIGINT NOT NULL,
    host              VARCHAR NOT NULL DEFAULT 'local'
);

CREATE TABLE IF NOT EXISTS tool_calls (
    turn_uuid     VARCHAR NOT NULL,
    idx           INT NOT NULL,
    tool_name     VARCHAR NOT NULL,
    input_chars   BIGINT NOT NULL,
    input_preview VARCHAR
);

CREATE TABLE IF NOT EXISTS user_turns (
    uuid         VARCHAR PRIMARY KEY,
    session_id   VARCHAR NOT NULL,
    project_path VARCHAR NOT NULL,
    ts           TIMESTAMP NOT NULL,
    source_file  VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    source_file        VARCHAR PRIMARY KEY,
    mtime              DOUBLE  NOT NULL,
    last_offset        BIGINT  NOT NULL,
    last_ingested_at   TIMESTAMP NOT NULL,
    malformed_lines    BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_turns_session   ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_project   ON turns(project_path);
CREATE INDEX IF NOT EXISTS idx_turns_model     ON turns(model);
CREATE INDEX IF NOT EXISTS idx_turns_ts        ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_toolcalls_turn  ON tool_calls(turn_uuid);
CREATE INDEX IF NOT EXISTS idx_toolcalls_name  ON tool_calls(tool_name);
"""


def _migrate_add_host_column(conn) -> None:
    """Backfill host column for DBs created before multi-root support.

    Must run AFTER SCHEMA_SQL so the table exists; runs BEFORE the host index
    so the column is present when we try to index it.
    """
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'turns'"
    ).fetchall()}
    if "host" not in cols:
        local_label = socket.gethostname()
        conn.execute("ALTER TABLE turns ADD COLUMN host VARCHAR;")
        conn.execute("UPDATE turns SET host = ? WHERE host IS NULL;", [local_label])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_host ON turns(host);")


def connect(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    # Constrain on low-memory hosts (the Pi has ~6 GiB; DuckDB will grab it all
    # by default and OOM mid-ingest). Set DUCKDB_MEMORY_LIMIT=2GB / DUCKDB_THREADS=2
    # in the systemd unit on the Pi.
    mem = os.environ.get("DUCKDB_MEMORY_LIMIT")
    if mem:
        conn.execute(f"SET memory_limit = '{mem}'")
    threads = os.environ.get("DUCKDB_THREADS")
    if threads:
        conn.execute(f"SET threads = {int(threads)}")
    if os.environ.get("DUCKDB_PRESERVE_INSERTION_ORDER", "1") == "0":
        conn.execute("SET preserve_insertion_order = false")
    conn.execute(SCHEMA_SQL)
    _migrate_add_host_column(conn)
    return conn


def reset(db_path: Path | None = None) -> None:
    """Drop all data and re-create schema. Used by `ingest --full`."""
    conn = connect(db_path)
    conn.execute("DROP TABLE IF EXISTS tool_calls;")
    conn.execute("DROP TABLE IF EXISTS turns;")
    conn.execute("DROP TABLE IF EXISTS user_turns;")
    conn.execute("DROP TABLE IF EXISTS ingest_log;")
    conn.execute(SCHEMA_SQL)
    conn.close()
