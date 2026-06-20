"""SQL views and query helpers over the ingested data."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from .db import connect
from .pricing import load_rates


def _register_pricing(conn: duckdb.DuckDBPyConnection) -> None:
    """Load pricing into a temporary table joined by view DDL."""
    rates = load_rates()
    conn.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pricing (
            model VARCHAR PRIMARY KEY,
            input_per_mtok DOUBLE,
            output_per_mtok DOUBLE,
            cache_write_5m_per_mtok DOUBLE,
            cache_write_1h_per_mtok DOUBLE,
            cache_read_per_mtok DOUBLE
        );
        """
    )
    for model, rate in rates.items():
        conn.execute(
            "INSERT INTO _pricing VALUES (?, ?, ?, ?, ?, ?)",
            [
                model, rate.input, rate.output,
                rate.cache_write_5m, rate.cache_write_1h, rate.cache_read,
            ],
        )
    sonnet = rates["claude-sonnet-4-6"]
    conn.execute(
        "INSERT INTO _pricing VALUES ('<fallback>', ?, ?, ?, ?, ?)",
        [
            sonnet.input, sonnet.output,
            sonnet.cache_write_5m, sonnet.cache_write_1h, sonnet.cache_read,
        ],
    )
    conn.execute(
        "INSERT INTO _pricing VALUES ('<synthetic>', 0, 0, 0, 0, 0)"
    )


VIEW_SQL = """
CREATE OR REPLACE VIEW v_turn_cost AS
SELECT
    t.uuid,
    t.session_id,
    t.project_path,
    t.project_label,
    t.git_branch,
    t.model,
    t.ts,
    t.is_sidechain,
    t.host,
    t.input_tokens,
    t.output_tokens,
    t.cache_write_5m,
    t.cache_write_1h,
    t.cache_read,
    t.input_tokens   * COALESCE(p.input_per_mtok,   f.input_per_mtok)   / 1e6 AS input_usd,
    t.output_tokens  * COALESCE(p.output_per_mtok,  f.output_per_mtok)  / 1e6 AS output_usd,
    t.cache_write_5m * COALESCE(p.cache_write_5m_per_mtok, f.cache_write_5m_per_mtok) / 1e6 AS cache_write_5m_usd,
    t.cache_write_1h * COALESCE(p.cache_write_1h_per_mtok, f.cache_write_1h_per_mtok) / 1e6 AS cache_write_1h_usd,
    t.cache_read     * COALESCE(p.cache_read_per_mtok,     f.cache_read_per_mtok)     / 1e6 AS cache_read_usd,
    (
        t.input_tokens   * COALESCE(p.input_per_mtok,   f.input_per_mtok)   +
        t.output_tokens  * COALESCE(p.output_per_mtok,  f.output_per_mtok)  +
        t.cache_write_5m * COALESCE(p.cache_write_5m_per_mtok, f.cache_write_5m_per_mtok) +
        t.cache_write_1h * COALESCE(p.cache_write_1h_per_mtok, f.cache_write_1h_per_mtok) +
        t.cache_read     * COALESCE(p.cache_read_per_mtok,     f.cache_read_per_mtok)
    ) / 1e6 AS total_usd
FROM turns t
LEFT JOIN _pricing p ON p.model = t.model
CROSS JOIN (SELECT * FROM _pricing WHERE model = '<fallback>') f;

CREATE OR REPLACE VIEW v_session_summary AS
SELECT
    session_id,
    ANY_VALUE(project_label) AS project_label,
    ANY_VALUE(project_path)  AS project_path,
    COUNT(*) AS turns,
    MIN(ts)  AS first_ts,
    MAX(ts)  AS last_ts,
    SUM(input_tokens)   AS input_tokens,
    SUM(output_tokens)  AS output_tokens,
    SUM(cache_write_5m) AS cache_write_5m,
    SUM(cache_write_1h) AS cache_write_1h,
    SUM(cache_read)     AS cache_read,
    SUM(total_usd)      AS total_usd
FROM v_turn_cost
GROUP BY session_id;

CREATE OR REPLACE VIEW v_project_summary AS
SELECT
    project_path,
    ANY_VALUE(project_label) AS project_label,
    COUNT(DISTINCT session_id) AS sessions,
    COUNT(*)                   AS turns,
    SUM(input_tokens)   AS input_tokens,
    SUM(output_tokens)  AS output_tokens,
    SUM(cache_write_5m) AS cache_write_5m,
    SUM(cache_write_1h) AS cache_write_1h,
    SUM(cache_read)     AS cache_read,
    SUM(total_usd)      AS total_usd
FROM v_turn_cost
GROUP BY project_path;

CREATE OR REPLACE VIEW v_model_summary AS
SELECT
    model,
    COUNT(*) AS turns,
    SUM(input_tokens)   AS input_tokens,
    SUM(output_tokens)  AS output_tokens,
    SUM(cache_write_5m) AS cache_write_5m,
    SUM(cache_write_1h) AS cache_write_1h,
    SUM(cache_read)     AS cache_read,
    SUM(total_usd)      AS total_usd
FROM v_turn_cost
GROUP BY model;

CREATE OR REPLACE VIEW v_daily_spend AS
SELECT
    date_trunc('day', ts) AS day,
    model,
    COUNT(*) AS turns,
    SUM(input_tokens + output_tokens + cache_write_5m + cache_write_1h + cache_read) AS total_tokens,
    SUM(total_usd) AS total_usd
FROM v_turn_cost
GROUP BY day, model
ORDER BY day, model;

CREATE OR REPLACE VIEW v_tool_rollup AS
SELECT
    tc.tool_name,
    COUNT(*)                AS calls,
    COUNT(DISTINCT tc.turn_uuid) AS turns_using,
    SUM(tc.input_chars)     AS total_input_chars,
    AVG(tc.input_chars)     AS avg_input_chars
FROM tool_calls tc
GROUP BY tc.tool_name
ORDER BY calls DESC;

CREATE OR REPLACE VIEW v_cache_efficiency AS
SELECT
    model,
    SUM(cache_read)     AS cache_read,
    SUM(cache_write_5m + cache_write_1h) AS cache_write,
    SUM(input_tokens)   AS uncached_input,
    CASE WHEN SUM(cache_read + cache_write_5m + cache_write_1h) > 0
         THEN 100.0 * SUM(cache_read)
              / SUM(cache_read + cache_write_5m + cache_write_1h)
         ELSE 0 END AS cache_hit_pct
FROM v_turn_cost
GROUP BY model;
"""


def connect_with_views(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    conn = connect(db_path)
    _register_pricing(conn)
    conn.execute(VIEW_SQL)
    return conn


def parse_since(since: str | None) -> datetime | None:
    """Accept '7d', '30d', '24h', '60m', 'all', or None."""
    if not since or since == "all":
        return None
    s = since.strip().lower()
    if s.endswith("d"):
        return datetime.now() - timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return datetime.now() - timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return datetime.now() - timedelta(minutes=int(s[:-1]))
    raise ValueError(f"unrecognized --since: {since!r}")


def summary(conn: duckdb.DuckDBPyConnection, since: str | None = None) -> dict:
    cutoff = parse_since(since)
    where = "WHERE ts >= ?" if cutoff else ""
    params = [cutoff] if cutoff else []
    totals = conn.execute(
        f"""
        SELECT
            COUNT(*)                              AS turns,
            COUNT(DISTINCT session_id)            AS sessions,
            COUNT(DISTINCT project_path)          AS projects,
            COUNT(DISTINCT model)                 AS models,
            COALESCE(SUM(input_tokens),0)         AS input_tokens,
            COALESCE(SUM(output_tokens),0)        AS output_tokens,
            COALESCE(SUM(cache_write_5m),0)       AS cache_write_5m,
            COALESCE(SUM(cache_write_1h),0)       AS cache_write_1h,
            COALESCE(SUM(cache_read),0)           AS cache_read,
            COALESCE(SUM(total_usd),0)            AS total_usd
        FROM v_turn_cost
        {where}
        """,
        params,
    ).fetchone()
    keys = [
        "turns", "sessions", "projects", "models",
        "input_tokens", "output_tokens", "cache_write_5m", "cache_write_1h", "cache_read",
        "total_usd",
    ]
    return dict(zip(keys, totals))


def spend_by(
    conn: duckdb.DuckDBPyConnection,
    dimension: str,
    since: str | None = None,
    limit: int = 50,
) -> list[tuple]:
    cutoff = parse_since(since)
    where = "WHERE ts >= ?" if cutoff else ""
    params = [cutoff] if cutoff else []

    if dimension == "project":
        q = f"""
            SELECT project_label, project_path, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY project_label, project_path
            ORDER BY usd DESC LIMIT {limit}
        """
    elif dimension == "model":
        q = f"""
            SELECT model, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY model ORDER BY usd DESC LIMIT {limit}
        """
    elif dimension == "day":
        q = f"""
            SELECT CAST(date_trunc('day', ts) AS DATE) AS day, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY day ORDER BY day DESC LIMIT {limit}
        """
    elif dimension == "session":
        q = f"""
            SELECT session_id, ANY_VALUE(project_label) AS project, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY session_id ORDER BY usd DESC LIMIT {limit}
        """
    elif dimension == "tool":
        q = """
            SELECT tool_name, COUNT(*) AS calls, COUNT(DISTINCT turn_uuid) AS turns_using,
                   SUM(input_chars) AS input_chars
            FROM tool_calls
            GROUP BY tool_name ORDER BY calls DESC LIMIT 50
        """
        params = []
    elif dimension == "host":
        q = f"""
            SELECT host, COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY host ORDER BY usd DESC LIMIT {limit}
        """
    else:
        raise ValueError(f"unknown dimension: {dimension}")

    return conn.execute(q, params).fetchall()


def top_turns(
    conn: duckdb.DuckDBPyConnection,
    metric: str = "cost",
    n: int = 20,
    since: str | None = None,
) -> list[tuple]:
    cutoff = parse_since(since)
    where = "WHERE ts >= ?" if cutoff else ""
    params = [cutoff] if cutoff else []
    order_by = {
        "cost": "total_usd",
        "tokens": "(input_tokens + output_tokens + cache_write_5m + cache_write_1h + cache_read)",
        "cache_write": "(cache_write_5m + cache_write_1h)",
        "cache_read": "cache_read",
        "output": "output_tokens",
    }[metric]
    return conn.execute(
        f"""
        SELECT uuid, ts, project_label, session_id, model,
               input_tokens, output_tokens, cache_write_5m+cache_write_1h AS cache_write,
               cache_read, total_usd
        FROM v_turn_cost {where}
        ORDER BY {order_by} DESC
        LIMIT ?
        """,
        [*params, n],
    ).fetchall()


def project_drilldown(conn: duckdb.DuckDBPyConnection, name_or_path: str) -> dict:
    row = conn.execute(
        """
        SELECT project_path, project_label, sessions, turns, input_tokens, output_tokens,
               cache_write_5m, cache_write_1h, cache_read, total_usd
        FROM v_project_summary
        WHERE project_label = ? OR project_path = ?
        LIMIT 1
        """,
        [name_or_path, name_or_path],
    ).fetchone()
    if row is None:
        return {}
    keys = [
        "project_path", "project_label", "sessions", "turns",
        "input_tokens", "output_tokens", "cache_write_5m", "cache_write_1h", "cache_read",
        "total_usd",
    ]
    out = dict(zip(keys, row))
    out["models"] = conn.execute(
        """
        SELECT model, COUNT(*) AS turns, SUM(total_usd) AS usd
        FROM v_turn_cost
        WHERE project_path = ?
        GROUP BY model ORDER BY usd DESC
        """,
        [out["project_path"]],
    ).fetchall()
    out["sessions_detail"] = conn.execute(
        """
        SELECT session_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
               COUNT(*) AS turns, SUM(total_usd) AS usd
        FROM v_turn_cost
        WHERE project_path = ?
        GROUP BY session_id ORDER BY usd DESC LIMIT 50
        """,
        [out["project_path"]],
    ).fetchall()
    return out


def session_trace(conn: duckdb.DuckDBPyConnection, session_id: str) -> list[tuple]:
    return conn.execute(
        """
        SELECT ts, model, input_tokens, output_tokens,
               cache_write_5m+cache_write_1h AS cache_write, cache_read,
               total_usd, (SELECT COUNT(*) FROM tool_calls tc WHERE tc.turn_uuid = t.uuid) AS tools
        FROM v_turn_cost t
        WHERE session_id = ?
        ORDER BY ts
        """,
        [session_id],
    ).fetchall()


def cache_efficiency(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute(
        """
        SELECT model, cache_read, cache_write, uncached_input, cache_hit_pct
        FROM v_cache_efficiency
        ORDER BY cache_read DESC
        """
    ).fetchall()


def timeseries(
    conn: duckdb.DuckDBPyConnection,
    bucket: str = "day",
    since: str | None = None,
) -> list[tuple]:
    cutoff = parse_since(since)
    where = "WHERE ts >= ?" if cutoff else ""
    params = [cutoff] if cutoff else []
    return conn.execute(
        f"""
        SELECT CAST(date_trunc(?, ts) AS DATE) AS bucket, model,
               COUNT(*) AS turns,
               SUM(total_usd) AS usd
        FROM v_turn_cost {where}
        GROUP BY bucket, model
        ORDER BY bucket
        """,
        [bucket, *params],
    ).fetchall()
