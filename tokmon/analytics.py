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
CREATE OR REPLACE TEMP VIEW v_turn_cost AS
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

CREATE OR REPLACE TEMP VIEW v_session_summary AS
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

CREATE OR REPLACE TEMP VIEW v_project_summary AS
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

CREATE OR REPLACE TEMP VIEW v_model_summary AS
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

CREATE OR REPLACE TEMP VIEW v_daily_spend AS
SELECT
    date_trunc('day', ts) AS day,
    model,
    COUNT(*) AS turns,
    SUM(input_tokens + output_tokens + cache_write_5m + cache_write_1h + cache_read) AS total_tokens,
    SUM(total_usd) AS total_usd
FROM v_turn_cost
GROUP BY day, model
ORDER BY day, model;

CREATE OR REPLACE TEMP VIEW v_tool_rollup AS
SELECT
    tc.tool_name,
    COUNT(*)                AS calls,
    COUNT(DISTINCT tc.turn_uuid) AS turns_using,
    SUM(tc.input_chars)     AS total_input_chars,
    AVG(tc.input_chars)     AS avg_input_chars
FROM tool_calls tc
GROUP BY tc.tool_name
ORDER BY calls DESC;

CREATE OR REPLACE TEMP VIEW v_cache_efficiency AS
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


def connect_with_views(
    db_path: Path | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    conn = connect(db_path, read_only=read_only)
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


def _build_filter(
    since: str | None = None,
    host: str | None = None,
) -> tuple[str, list]:
    """Return (where_clause, params) for the standard since+host filter."""
    clauses = []
    params: list = []
    cutoff = parse_since(since)
    if cutoff:
        clauses.append("ts >= ?")
        params.append(cutoff)
    if host:
        clauses.append("host = ?")
        params.append(host)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def summary(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
) -> dict:
    where, params = _build_filter(since, host)
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
    host: str | None = None,
) -> list[tuple]:
    # When the dimension *is* host, ignore the host filter (the breakdown
    # already shows per-host; filtering would collapse it to one row).
    effective_host = None if dimension == "host" else host
    where, params = _build_filter(since, effective_host)

    if dimension == "project":
        q = f"""
            SELECT project_label, project_path, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY project_label, project_path
            ORDER BY usd DESC LIMIT {limit}
        """
    elif dimension == "project_label":
        # Same logical project across machines collapses to one row.
        # Returns extra `paths` column so the UI can show "3 paths" when
        # the same label spans multiple cwds.
        q = f"""
            SELECT project_label,
                   COUNT(DISTINCT project_path) AS paths,
                   COUNT(*)                     AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY project_label
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
    elif dimension == "hour":
        q = f"""
            SELECT date_trunc('hour', ts) AS hour, COUNT(*) AS turns,
                   SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
                   SUM(total_usd) AS usd
            FROM v_turn_cost {where}
            GROUP BY hour ORDER BY hour DESC LIMIT {limit}
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
        # tool_calls table joins via turn_uuid → can filter on host through turns
        if host:
            q = """
                SELECT tc.tool_name, COUNT(*) AS calls,
                       COUNT(DISTINCT tc.turn_uuid) AS turns_using,
                       SUM(tc.input_chars) AS input_chars
                FROM tool_calls tc
                JOIN turns t ON t.uuid = tc.turn_uuid
                WHERE t.host = ?
                GROUP BY tc.tool_name ORDER BY calls DESC LIMIT 50
            """
            params = [host]
        else:
            q = """
                SELECT tool_name, COUNT(*) AS calls,
                       COUNT(DISTINCT turn_uuid) AS turns_using,
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
    host: str | None = None,
) -> list[tuple]:
    where, params = _build_filter(since, host)
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


def cache_efficiency(
    conn: duckdb.DuckDBPyConnection,
    host: str | None = None,
) -> list[tuple]:
    if host:
        # Recompute the rollup with a host filter rather than using the view,
        # which doesn't pre-group by host.
        return conn.execute(
            """
            SELECT model,
                   SUM(cache_read) AS cache_read,
                   SUM(cache_write_5m + cache_write_1h) AS cache_write,
                   SUM(input_tokens) AS uncached_input,
                   CASE WHEN SUM(cache_read + cache_write_5m + cache_write_1h) > 0
                        THEN 100.0 * SUM(cache_read)
                             / SUM(cache_read + cache_write_5m + cache_write_1h)
                        ELSE 0 END AS cache_hit_pct
            FROM v_turn_cost
            WHERE host = ?
            GROUP BY model
            ORDER BY cache_read DESC
            """,
            [host],
        ).fetchall()
    return conn.execute(
        """
        SELECT model, cache_read, cache_write, uncached_input, cache_hit_pct
        FROM v_cache_efficiency
        ORDER BY cache_read DESC
        """
    ).fetchall()


def calendar_heatmap(
    conn: duckdb.DuckDBPyConnection,
    days_back: int = 365,
    host: str | None = None,
) -> list[tuple]:
    """One row per day for the last N days. Empty days are not returned —
    caller fills gaps with 0 to render the grid."""
    where_clauses = ["ts >= CURRENT_TIMESTAMP - (INTERVAL 1 DAY * ?)"]
    params: list = [days_back]
    if host:
        where_clauses.append("host = ?")
        params.append(host)
    where = "WHERE " + " AND ".join(where_clauses)
    return conn.execute(
        f"""
        SELECT CAST(date_trunc('day', ts) AS DATE) AS day,
               SUM(total_usd) AS usd,
               COUNT(*) AS turns
        FROM v_turn_cost
        {where}
        GROUP BY day
        ORDER BY day
        """,
        params,
    ).fetchall()


def hour_dow_heatmap(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
) -> list[tuple]:
    """7×24 grid: (dow 0=Sunday..6=Saturday, hour 0-23, turns, usd)."""
    where, params = _build_filter(since, host)
    return conn.execute(
        f"""
        SELECT CAST(EXTRACT(dow FROM ts) AS INT) AS dow,
               CAST(EXTRACT(hour FROM ts) AS INT) AS hour,
               COUNT(*) AS turns,
               SUM(total_usd) AS usd
        FROM v_turn_cost
        {where}
        GROUP BY dow, hour
        ORDER BY dow, hour
        """,
        params,
    ).fetchall()


def cache_savings(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
) -> dict:
    """Counterfactual cost if every cache_read token had been full-price input.

    Anthropic's cache_read pricing is 0.1× the input rate by convention, so the
    extra you would have paid is exactly 9× what cache_read actually cost.
    """
    where, params = _build_filter(since, host)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(cache_read_usd), 0) AS cache_read_usd,
               COALESCE(SUM(total_usd), 0) AS actual_total,
               COALESCE(SUM(cache_read), 0) AS cache_read_tokens
        FROM v_turn_cost
        {where}
        """,
        params,
    ).fetchone()
    cache_read_usd, actual_total, cache_read_tokens = row
    counterfactual_extra = float(cache_read_usd) * 9.0
    counterfactual_total = float(actual_total) + counterfactual_extra
    savings_pct = (
        100.0 * counterfactual_extra / counterfactual_total
        if counterfactual_total > 0 else 0.0
    )
    return {
        "cache_read_tokens": int(cache_read_tokens),
        "actual_cache_read_usd": float(cache_read_usd),
        "actual_total_usd": float(actual_total),
        "counterfactual_extra_usd": counterfactual_extra,
        "counterfactual_total_usd": counterfactual_total,
        "savings_pct": savings_pct,
    }


def burn_rate(
    conn: duckdb.DuckDBPyConnection,
    window_minutes: int = 60,
    host: str | None = None,
) -> dict:
    """Recent spend rate. Returns $ in the window AND extrapolated $/hour."""
    where_clauses = ["ts >= CURRENT_TIMESTAMP - (INTERVAL 1 MINUTE * ?)"]
    params: list = [window_minutes]
    if host:
        where_clauses.append("host = ?")
        params.append(host)
    where = "WHERE " + " AND ".join(where_clauses)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(total_usd), 0) AS usd,
               COUNT(*) AS turns
        FROM v_turn_cost
        {where}
        """,
        params,
    ).fetchone()
    spend = float(row[0])
    return {
        "window_minutes": window_minutes,
        "spend_in_window_usd": spend,
        "turns_in_window": int(row[1]),
        "rate_per_hour_usd": spend * (60.0 / window_minutes),
    }


def tool_cost_attribution(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
) -> list[tuple]:
    """For each tool: how many turns called it, the avg cost of those turns,
    and the total cost contributed."""
    filter_clauses = []
    params: list = []
    cutoff = parse_since(since)
    if cutoff:
        filter_clauses.append("t.ts >= ?")
        params.append(cutoff)
    if host:
        filter_clauses.append("t.host = ?")
        params.append(host)
    where = ("WHERE " + " AND ".join(filter_clauses)) if filter_clauses else ""
    return conn.execute(
        f"""
        SELECT tc.tool_name,
               COUNT(DISTINCT tc.turn_uuid) AS turns_using,
               COUNT(*)                    AS total_calls,
               AVG(t.total_usd)            AS avg_turn_usd,
               SUM(t.total_usd)            AS total_turn_usd
        FROM tool_calls tc
        JOIN v_turn_cost t ON t.uuid = tc.turn_uuid
        {where}
        GROUP BY tc.tool_name
        ORDER BY total_turn_usd DESC
        """,
        params,
    ).fetchall()


def outlier_sessions(
    conn: duckdb.DuckDBPyConnection,
    z_threshold: float = 2.0,
    min_sessions_per_project: int = 3,
    host: str | None = None,
) -> list[dict]:
    """Find sessions whose cost is z_threshold std-devs above their project mean.

    Skips projects with too few sessions to have a meaningful baseline.
    """
    extra_filter = "AND host = ?" if host else ""
    params = [host] if host else []
    rows = conn.execute(
        f"""
        WITH session_costs AS (
            SELECT session_id,
                   ANY_VALUE(project_path)  AS project_path,
                   ANY_VALUE(project_label) AS project_label,
                   ANY_VALUE(host)          AS host,
                   MIN(ts)                  AS started_at,
                   COUNT(*)                 AS turns,
                   SUM(total_usd)           AS usd
            FROM v_turn_cost
            WHERE 1=1 {extra_filter}
            GROUP BY session_id
        ),
        project_stats AS (
            SELECT project_path,
                   COUNT(*)            AS n_sessions,
                   AVG(usd)            AS mean_usd,
                   stddev_samp(usd)    AS std_usd
            FROM session_costs
            GROUP BY project_path
        )
        SELECT s.session_id, s.project_label, s.project_path, s.host,
               s.started_at, s.turns, s.usd,
               p.mean_usd, p.std_usd,
               (s.usd - p.mean_usd) / NULLIF(p.std_usd, 0) AS z_score,
               p.n_sessions
        FROM session_costs s
        JOIN project_stats p USING (project_path)
        WHERE p.n_sessions >= ?
          AND p.std_usd > 0
          AND (s.usd - p.mean_usd) / p.std_usd >= ?
        ORDER BY z_score DESC
        LIMIT 50
        """,
        [*params, min_sessions_per_project, z_threshold],
    ).fetchall()
    keys = ["session_id", "project_label", "project_path", "host", "started_at",
            "turns", "usd", "mean_usd", "std_usd", "z_score", "n_sessions"]
    out = []
    for row in rows:
        d = dict(zip(keys, row))
        if d["started_at"]:
            d["started_at"] = d["started_at"].isoformat()
        d["usd"] = float(d["usd"])
        d["mean_usd"] = float(d["mean_usd"])
        d["std_usd"] = float(d["std_usd"])
        d["z_score"] = float(d["z_score"])
        d["multiplier"] = d["usd"] / d["mean_usd"] if d["mean_usd"] > 0 else 0
        out.append(d)
    return out


def monthly_forecast(
    conn: duckdb.DuckDBPyConnection,
    host: str | None = None,
) -> dict:
    """Project this month's spend from month-to-date using mean daily burn rate.

    Returns: month_to_date_usd, days_elapsed, days_in_month, projected_eom_usd,
    plus a comparison to last month's same-day-cumulative.
    """
    extra = "AND host = ?" if host else ""
    params: list = [host] if host else []
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN date_trunc('month', ts) = date_trunc('month', CURRENT_TIMESTAMP)
                              THEN total_usd ELSE 0 END), 0) AS month_to_date,
            COALESCE(SUM(CASE WHEN date_trunc('month', ts) = date_trunc('month', CURRENT_TIMESTAMP - INTERVAL 1 MONTH)
                              AND ts < date_trunc('month', CURRENT_TIMESTAMP - INTERVAL 1 MONTH) + (CURRENT_TIMESTAMP - date_trunc('month', CURRENT_TIMESTAMP))
                              THEN total_usd ELSE 0 END), 0) AS last_month_same_window,
            COALESCE(SUM(CASE WHEN date_trunc('month', ts) = date_trunc('month', CURRENT_TIMESTAMP - INTERVAL 1 MONTH)
                              THEN total_usd ELSE 0 END), 0) AS last_month_total
        FROM v_turn_cost
        WHERE 1=1 {extra}
        """,
        params,
    ).fetchone()
    from datetime import datetime, date
    today = datetime.now()
    # Days elapsed in the current month (including today, fractional)
    days_elapsed = today.day + (today.hour / 24.0)
    # Days in the current month
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    last_day_of_month = (next_month - __import__("datetime").timedelta(days=1)).day
    mtd, lm_same, lm_total = float(row[0]), float(row[1]), float(row[2])
    daily_rate = mtd / days_elapsed if days_elapsed > 0 else 0
    projected = daily_rate * last_day_of_month
    return {
        "month_to_date_usd": mtd,
        "days_elapsed": days_elapsed,
        "days_in_month": last_day_of_month,
        "daily_rate_usd": daily_rate,
        "projected_eom_usd": projected,
        "last_month_total_usd": lm_total,
        "last_month_same_window_usd": lm_same,
        "vs_last_month_pct": (
            100.0 * (mtd - lm_same) / lm_same if lm_same > 0 else 0
        ),
    }


def branch_spend(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
    limit: int = 50,
) -> list[tuple]:
    """Spend grouped by (project, git_branch). Skips rows where branch is NULL."""
    where, params = _build_filter(since, host)
    extra = " AND " if where else " WHERE "
    return conn.execute(
        f"""
        SELECT project_label, git_branch,
               COUNT(*) AS turns,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(total_usd) AS usd
        FROM v_turn_cost
        {where}{extra}git_branch IS NOT NULL AND git_branch != ''
        GROUP BY project_label, git_branch
        ORDER BY usd DESC
        LIMIT {limit}
        """,
        params,
    ).fetchall()


def turn_explorer(
    conn: duckdb.DuckDBPyConnection,
    model: str | None = None,
    project: str | None = None,
    host: str | None = None,
    tool: str | None = None,
    min_usd: float | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[tuple]:
    """Filterable turn-level listing for the explorer tab."""
    cutoff = parse_since(since)
    where = []
    params: list = []
    if cutoff:
        where.append("t.ts >= ?")
        params.append(cutoff)
    if model:
        where.append("t.model = ?")
        params.append(model)
    if project:
        where.append("(t.project_label = ? OR t.project_path = ?)")
        params.extend([project, project])
    if host:
        where.append("t.host = ?")
        params.append(host)
    if min_usd is not None:
        where.append("t.total_usd >= ?")
        params.append(min_usd)
    if tool:
        where.append("EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.turn_uuid = t.uuid AND tc.tool_name = ?)")
        params.append(tool)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"""
        SELECT t.uuid, t.ts, t.host, t.project_label, t.session_id,
               t.model, t.input_tokens, t.output_tokens,
               t.cache_write_5m + t.cache_write_1h AS cache_write,
               t.cache_read, t.total_usd,
               (SELECT COUNT(*) FROM tool_calls tc WHERE tc.turn_uuid = t.uuid) AS n_tools,
               (SELECT string_agg(tc.tool_name, ',') FROM tool_calls tc WHERE tc.turn_uuid = t.uuid) AS tools
        FROM v_turn_cost t
        {where_clause}
        ORDER BY t.total_usd DESC
        LIMIT {limit}
        """,
        params,
    ).fetchall()


def turn_detail(
    conn: duckdb.DuckDBPyConnection,
    turn_uuid: str,
) -> dict | None:
    """Full detail for one turn, including raw_usage JSON and tool calls."""
    row = conn.execute(
        """
        SELECT t.uuid, t.ts, t.host, t.project_label, t.project_path, t.session_id,
               t.git_branch, t.model, t.input_tokens, t.output_tokens,
               t.cache_write_5m, t.cache_write_1h, t.cache_read,
               t.total_usd, t.stop_reason, t.has_thinking, t.thinking_chars,
               t.text_chars, turns.raw_usage
        FROM v_turn_cost t
        JOIN turns USING (uuid)
        WHERE t.uuid = ?
        """,
        [turn_uuid],
    ).fetchone()
    if not row:
        return None
    keys = ["uuid", "ts", "host", "project_label", "project_path", "session_id",
            "git_branch", "model", "input_tokens", "output_tokens",
            "cache_write_5m", "cache_write_1h", "cache_read",
            "total_usd", "stop_reason", "has_thinking", "thinking_chars",
            "text_chars", "raw_usage"]
    out = dict(zip(keys, row))
    if out["ts"]:
        out["ts"] = out["ts"].isoformat()
    out["total_usd"] = float(out["total_usd"])
    tools = conn.execute(
        "SELECT idx, tool_name, input_chars, input_preview FROM tool_calls WHERE turn_uuid = ? ORDER BY idx",
        [turn_uuid],
    ).fetchall()
    out["tools"] = [
        {"idx": i, "name": n, "input_chars": int(c), "input_preview": p}
        for i, n, c, p in tools
    ]
    return out


def achievements(
    conn: duckdb.DuckDBPyConnection,
    host: str | None = None,
) -> dict:
    """Fun stats: milestones reached, longest streak, hall-of-fame turn."""
    extra = "AND host = ?" if host else ""
    params: list = [host] if host else []
    # 1. Big-picture totals + most expensive single turn
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS turns,
            COUNT(DISTINCT session_id) AS sessions,
            COUNT(DISTINCT project_path) AS projects,
            SUM(total_usd) AS total_usd,
            MAX(total_usd) AS max_single_usd
        FROM v_turn_cost
        WHERE 1=1 {extra}
        """,
        params,
    ).fetchone()
    turns, sessions, projects, total_usd, max_single_usd = row
    total_usd = float(total_usd or 0)
    max_single_usd = float(max_single_usd or 0)

    # 2. The hall-of-fame turn details
    hof = conn.execute(
        f"""
        SELECT uuid, ts, project_label, model, total_usd, host
        FROM v_turn_cost
        WHERE total_usd = ? {extra}
        LIMIT 1
        """,
        [max_single_usd, *params] if host else [max_single_usd],
    ).fetchone()

    # 3. Activity streaks: count consecutive active days ending at most-recent active day
    streak_rows = conn.execute(
        f"""
        WITH active_days AS (
            SELECT DISTINCT CAST(date_trunc('day', ts) AS DATE) AS d
            FROM v_turn_cost
            WHERE 1=1 {extra}
        ),
        ordered AS (
            SELECT d,
                   d - INTERVAL (ROW_NUMBER() OVER (ORDER BY d)) DAY AS grp
            FROM active_days
        )
        SELECT MIN(d) AS start_day, MAX(d) AS end_day, COUNT(*) AS length
        FROM ordered
        GROUP BY grp
        ORDER BY length DESC
        LIMIT 5
        """,
        params,
    ).fetchall()
    longest_streak = streak_rows[0] if streak_rows else None
    # current streak: starting from today (or most recent active day if today's idle)
    current_streak = conn.execute(
        f"""
        WITH active_days AS (
            SELECT DISTINCT CAST(date_trunc('day', ts) AS DATE) AS d
            FROM v_turn_cost
            WHERE 1=1 {extra}
        ),
        ordered AS (
            SELECT d,
                   d - INTERVAL (ROW_NUMBER() OVER (ORDER BY d)) DAY AS grp
            FROM active_days
        ),
        streaks AS (
            SELECT MIN(d) AS start_day, MAX(d) AS end_day, COUNT(*) AS length
            FROM ordered GROUP BY grp
        )
        SELECT length, start_day, end_day
        FROM streaks
        ORDER BY end_day DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    # 4. Milestone badges
    milestones = []
    for tier, label, icon in [(10, "$10", "💸"), (100, "$100", "💵"),
                              (500, "$500", "💰"), (1000, "$1k", "🏦"),
                              (5000, "$5k club", "🏆"),
                              (10000, "$10k club", "👑")]:
        if total_usd >= tier:
            milestones.append({"label": label, "icon": icon, "threshold": tier})
    for tier, label, icon in [(100, "100 turns", "🪙"),
                              (1000, "1k turns", "🎯"),
                              (10000, "10k turns", "🚀"),
                              (50000, "50k turns", "🌌")]:
        if (turns or 0) >= tier:
            milestones.append({"label": label, "icon": icon, "threshold": tier})

    return {
        "turns": int(turns or 0),
        "sessions": int(sessions or 0),
        "projects": int(projects or 0),
        "total_usd": total_usd,
        "max_single_usd": max_single_usd,
        "hall_of_fame_turn": (
            {
                "uuid": hof[0],
                "ts": hof[1].isoformat() if hof[1] else None,
                "project": hof[2],
                "model": hof[3],
                "usd": float(hof[4]),
                "host": hof[5],
            }
            if hof else None
        ),
        "longest_streak": (
            {"length": int(longest_streak[2]),
             "start": str(longest_streak[0]),
             "end": str(longest_streak[1])} if longest_streak else None
        ),
        "current_streak": (
            {"length": int(current_streak[0]),
             "start": str(current_streak[1]),
             "end": str(current_streak[2])} if current_streak else None
        ),
        "milestones": milestones,
    }


def timeseries(
    conn: duckdb.DuckDBPyConnection,
    bucket: str = "day",
    since: str | None = None,
    host: str | None = None,
) -> list[tuple]:
    where, params = _build_filter(since, host)
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


# ---------------------------------------------------------------------------
# Quota inference
# ---------------------------------------------------------------------------
#
# Anthropic enforces subscription usage in rolling windows (a ~5-hour window
# that resets 5h after your first message in it, and a 7-day weekly window with
# a separate smaller cap for Opus). The *size* of those quotas is never sent to
# the client — there are no rate-limit headers in the Claude Code transcripts.
# So we cannot read a quota off the data; we can only INFER it from the shape of
# usage. The logic below rests on one idea:
#
#   The most usage that ever fit inside a window is a LOWER BOUND on that
#   window's quota. If you repeatedly push usage up to roughly the same level
#   and then stop — especially if you resume right when the window resets — that
#   recurring level is the ceiling itself.
#
# We therefore (a) measure the peak usage per window (always a valid lower
# bound) and (b) look for a *cluster* of near-peak windows plus "wall events"
# (a near-peak window immediately followed by a pause that ends at the reset).
# Clustering + walls promote a lower bound to an actual ceiling estimate with a
# confidence label; without them we only report the lower bound.
#
# Unit: there is no published mapping from tokens to quota units. The best
# single proxy is API-equivalent dollar cost, because the plans are loosely
# benchmarked to "≈ $X of API value" and cached tokens are discounted in both
# pricing and (per Anthropic's docs) quota accounting. `metric="tokens"` is
# offered too, but it over-weights cache-heavy usage relative to the real quota.
#
# Quotas are per ACCOUNT, so this analysis combines every host by default — the
# 5h/weekly windows are consumed by all your machines together.

WINDOW_SECONDS = {
    "5h": 5 * 3600,
    "weekly": 7 * 24 * 3600,
}


def _sliding_window_peak(times: list[float], values: list[float], window_s: float) -> float:
    """Max sum of values over any trailing window of width `window_s`.

    Both lists are aligned and `times` is sorted ascending (epoch seconds).
    The maximum sliding-window total is always achieved by a window whose right
    edge sits on a data point, so scanning each point as the right edge is exact.
    """
    best = 0.0
    cur = 0.0
    left = 0
    for right in range(len(times)):
        cur += values[right]
        while times[right] - times[left] > window_s:
            cur -= values[left]
            left += 1
        if cur > best:
            best = cur
    return best


def _fixed_blocks(times: list[float], values: list[float], window_s: float) -> list[dict]:
    """Partition turns into reset-anchored blocks, mirroring how the windows
    actually reset: a block opens on its first turn and closes `window_s` later;
    the next turn after it opens a fresh block.
    """
    blocks: list[dict] = []
    n = len(times)
    i = 0
    while i < n:
        start = times[i]
        reset = start + window_s
        total = 0.0
        last = start
        j = i
        while j < n and times[j] < reset:
            total += values[j]
            last = times[j]
            j += 1
        blocks.append({
            "start": start,
            "reset": reset,
            "last": last,
            "total": total,
            "n_turns": j - i,
            "next_turn": times[j] if j < n else None,
        })
        i = j
    return blocks


def _ceiling_from_blocks(blocks: list[dict], window_s: float) -> dict:
    """Turn a set of reset-anchored blocks into a quota estimate.

    Reports the peak (a hard lower bound) always, and a ceiling estimate only
    when near-peak blocks cluster tightly and/or several "wall events" appear.
    """
    totals = sorted((b["total"] for b in blocks), reverse=True)
    if not totals:
        return {
            "lower_bound": 0.0, "ceiling_estimate": None, "confidence": "none",
            "n_blocks": 0, "n_near_peak": 0, "cluster_cv": None, "n_wall_events": 0,
        }
    peak = totals[0]
    near = [t for t in totals if t >= 0.9 * peak]
    n_near = len(near)
    mean_near = sum(near) / n_near
    if n_near > 1:
        var = sum((x - mean_near) ** 2 for x in near) / (n_near - 1)
        cv = (var ** 0.5) / mean_near if mean_near else 0.0
    else:
        cv = None

    # A "wall event": a near-peak block whose work ran into the second half of
    # the window and then paused until on/after the reset boundary — the
    # signature of getting locked out and resuming when the quota refreshed.
    tolerance = 0.2 * window_s
    walls = 0
    for b in blocks:
        if b["total"] < 0.9 * peak or b["next_turn"] is None:
            continue
        ran_late = b["last"] >= b["reset"] - 0.5 * window_s
        resumed_at_reset = b["reset"] <= b["next_turn"] <= b["reset"] + tolerance
        if ran_late and resumed_at_reset:
            walls += 1

    if n_near >= 5 and cv is not None and cv < 0.08:
        confidence = "high"
    elif n_near >= 3 and (cv is None or cv < 0.15):
        confidence = "medium"
    else:
        confidence = "low"
    if walls >= 2 and confidence == "low":
        confidence = "medium"  # repeated walls are strong even without tight CV

    detected = confidence in ("high", "medium") or walls >= 2
    return {
        "lower_bound": peak,
        "ceiling_estimate": mean_near if detected else None,
        "confidence": confidence if detected else "none",
        "n_blocks": len(blocks),
        "n_near_peak": n_near,
        "cluster_cv": cv,
        "n_wall_events": walls,
    }


def _top_blocks(blocks: list[dict], n: int = 8) -> list[dict]:
    from datetime import datetime, timezone
    out = []
    for b in sorted(blocks, key=lambda x: x["total"], reverse=True)[:n]:
        out.append({
            "start": datetime.fromtimestamp(b["start"], tz=timezone.utc).isoformat(),
            "total": float(b["total"]),
            "n_turns": int(b["n_turns"]),
            "duration_hours": (b["last"] - b["start"]) / 3600.0,
        })
    return out


def _window_report(times, values, window_s) -> dict:
    blocks = _fixed_blocks(times, values, window_s)
    report = _ceiling_from_blocks(blocks, window_s)
    report["rolling_peak"] = _sliding_window_peak(times, values, window_s)
    report["top_blocks"] = _top_blocks(blocks)
    return report


def quota_inference(
    conn: duckdb.DuckDBPyConnection,
    metric: str = "usd",
    host: str | None = None,
) -> dict:
    """Infer the size of the 5-hour, weekly, and per-session quotas — and how
    they have moved over time — from observed usage.

    metric: "usd" (API-$ equivalent, the best proxy) or "tokens" (raw count).
    host:   normally None — quotas are per-account, so all hosts are combined.
            Pass a host only to inspect one machine's contribution.
    """
    if metric not in ("usd", "tokens"):
        raise ValueError(f"metric must be 'usd' or 'tokens', got {metric!r}")
    value_expr = (
        "total_usd" if metric == "usd"
        else "(input_tokens + output_tokens + cache_write_5m + cache_write_1h + cache_read)"
    )
    where = "WHERE host = ?" if host else ""
    params = [host] if host else []
    rows = conn.execute(
        f"""
        SELECT ts, model, session_id, {value_expr} AS val
        FROM v_turn_cost
        {where}
        ORDER BY ts
        """,
        params,
    ).fetchall()

    notes = [
        "Quotas are inferred, not measured: Claude Code transcripts carry no "
        "rate-limit headers. Peak window usage is a hard lower bound; a ceiling "
        "estimate appears only when near-peak windows cluster or repeat at reset.",
        "Unit is API-$ equivalent — the closest available proxy for Anthropic's "
        "internal quota accounting, not an official quota unit."
        if metric == "usd" else
        "Unit is raw token count, which over-weights cache-heavy usage relative "
        "to the real quota; the $-equivalent metric tracks the quota more closely.",
        "Per-account: all hosts are combined unless a host filter is set, because "
        "the 5h and weekly windows are shared across every machine on the account.",
    ]

    if not rows:
        return {
            "metric": metric, "host": host, "data_range": None,
            "windows": {}, "weekly_opus": None, "evolution": [], "notes": notes,
        }

    # Row layout: r = (ts, model, session_id, val)
    times = [r[0].timestamp() for r in rows]
    models = [r[1] or "" for r in rows]
    session_ids = [r[2] for r in rows]
    values = [float(r[3] or 0.0) for r in rows]

    # --- time-window quotas (5h, weekly) over the whole account ---
    windows = {
        "5h": _window_report(times, values, WINDOW_SECONDS["5h"]),
        "weekly": _window_report(times, values, WINDOW_SECONDS["weekly"]),
    }

    # --- per Claude Code session_id (no server quota; shown for completeness) ---
    sess_totals: dict[str, float] = {}
    for sid, v in zip(session_ids, values):
        sess_totals[sid] = sess_totals.get(sid, 0.0) + v
    sorted_sessions = sorted(sess_totals.values(), reverse=True)
    windows["session"] = {
        "lower_bound": sorted_sessions[0] if sorted_sessions else 0.0,
        "ceiling_estimate": None,
        "confidence": "none",
        "n_blocks": len(sorted_sessions),
        "note": "per Claude Code session_id — not an enforced server window; "
                "Anthropic meters the 5h and weekly windows instead.",
    }

    # --- Opus-only weekly (Anthropic's separate, smaller Opus weekly cap) ---
    opus_times = [t for t, m in zip(times, models) if "opus" in m.lower()]
    opus_values = [v for v, m in zip(values, models) if "opus" in m.lower()]
    weekly_opus = (
        _window_report(opus_times, opus_values, WINDOW_SECONDS["weekly"])
        if opus_times else None
    )

    # --- evolution: per-month peak window usage, to expose ceiling shifts ---
    from collections import defaultdict
    by_month_idx: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_month_idx[r[0].strftime("%Y-%m")].append(i)
    evolution = []
    for month in sorted(by_month_idx):
        idxs = by_month_idx[month]
        mt = [times[i] for i in idxs]
        mv = [values[i] for i in idxs]
        evolution.append({
            "month": month,
            "n_turns": len(idxs),
            "total": float(sum(mv)),
            "peak_5h": _sliding_window_peak(mt, mv, WINDOW_SECONDS["5h"]),
            "peak_weekly": _sliding_window_peak(mt, mv, WINDOW_SECONDS["weekly"]),
        })

    return {
        "metric": metric,
        "host": host,
        "data_range": {
            "first_ts": rows[0][0].isoformat(),
            "last_ts": rows[-1][0].isoformat(),
            "n_turns": len(rows),
            "n_days": (times[-1] - times[0]) / 86400.0,
        },
        "windows": windows,
        "weekly_opus": weekly_opus,
        "evolution": evolution,
        "notes": notes,
    }
