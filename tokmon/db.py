"""DuckDB connection, schema, and views."""

from __future__ import annotations

import os
import socket
import time

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


def _migrate_normalize_project_labels(conn) -> None:
    """Re-derive project_label from project_path using the cross-platform
    basename logic (split on / and \\). Idempotent — only updates rows whose
    stored label differs from the recomputed one. Cheap to run on every
    connect; usually a no-op.

    A previous version of `_project_label_from_path` used os.path.basename,
    which on POSIX hosts didn't split Windows-style paths, leaving labels
    like `C:\\Users\\you\\...\\memsync` instead of `memsync`.
    """
    # DuckDB regex needs the backslashes escaped twice (once for SQL, once for
    # the regex engine).
    sql = r"""
        UPDATE turns
        SET project_label = COALESCE(
            NULLIF(
                regexp_extract(rtrim(project_path, '/\'), '[^/\\]+$', 0),
                ''
            ),
            '<root>'
        )
        WHERE project_path IS NOT NULL
          AND project_path <> ''
          AND project_label <> COALESCE(
              NULLIF(
                  regexp_extract(rtrim(project_path, '/\'), '[^/\\]+$', 0),
                  ''
              ),
              '<root>'
          )
    """
    try:
        conn.execute(sql)
    except Exception:
        # Read-only connection (server) — skip silently. Ingest will fix it
        # on the next write-mode connect.
        pass


def connect(
    db_path: Path | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection.

    DuckDB allows one writer + many readers across processes, but only if the
    readers are explicitly opened read-only. The FastAPI server should always
    pass read_only=True so the ingest service can run alongside it.
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not path.exists():
        # Bootstrap an empty schema so the read-only connection has something
        # to open. Use a brief read-write connection.
        boot = duckdb.connect(str(path))
        boot.execute(SCHEMA_SQL)
        _migrate_add_host_column(boot)
        boot.close()
    # DuckDB allows only one open connection to a file across processes: while
    # the writer (ingest) holds it, readers are refused, and vice versa. Both
    # sides retry briefly to ride over the other's short-lived connection.
    # Writers (ingest, background) wait longer; readers (dashboard requests)
    # use a short backoff so the UI rides over the ~1-2s ingest write window
    # without hanging, instead of surfacing a transient lock as a 500.
    if read_only:
        attempts, backoff = 5, 0.4
    else:
        attempts, backoff = 5, 1.5
    for attempt in range(attempts):
        try:
            conn = duckdb.connect(str(path), read_only=read_only)
            break
        except (duckdb.IOException, IOError):
            if attempt == attempts - 1:
                raise
            time.sleep(backoff)
    # Constrain on low-memory hosts (Pi has 6 GiB; DuckDB grabs it all by
    # default). Set DUCKDB_MEMORY_LIMIT / DUCKDB_THREADS in the systemd unit.
    mem = os.environ.get("DUCKDB_MEMORY_LIMIT")
    if mem:
        conn.execute(f"SET memory_limit = '{mem}'")
    threads = os.environ.get("DUCKDB_THREADS")
    if threads:
        conn.execute(f"SET threads = {int(threads)}")
    if os.environ.get("DUCKDB_PRESERVE_INSERTION_ORDER", "1") == "0":
        conn.execute("SET preserve_insertion_order = false")
    if not read_only:
        conn.execute(SCHEMA_SQL)
        _migrate_add_host_column(conn)
        _migrate_normalize_project_labels(conn)
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
