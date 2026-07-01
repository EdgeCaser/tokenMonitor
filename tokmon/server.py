"""FastAPI server + static dashboard."""

from __future__ import annotations

import contextvars
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import analytics as A

app = FastAPI(title="tokmon", version="0.1.0")

# Per-request DuckDB connections, closed by the middleware below. DuckDB only
# allows a writer when no other process holds the file open, so the server must
# release every read-only connection promptly — otherwise the 10-minute ingest
# can never acquire the write lock and new data stops appearing.
_request_conns: contextvars.ContextVar[list] = contextvars.ContextVar("_request_conns")


def _conn():
    """Open a read-only analytics connection, tracked for close after the request."""
    conn = A.connect_with_views(read_only=True)
    try:
        _request_conns.get().append(conn)
    except LookupError:
        pass  # opened outside a request (tests/CLI) — caller owns close
    return conn


@app.middleware("http")
async def _close_request_conns(request, call_next):
    token = _request_conns.set([])
    try:
        return await call_next(request)
    finally:
        for conn in _request_conns.get():
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        _request_conns.reset(token)


def _rows_to_dicts(rows, keys):
    return [dict(zip(keys, r)) for r in rows]


@app.get("/api/summary")
def api_summary(since: str = Query("all"), host: str | None = Query(None)):
    conn = _conn()
    return A.summary(conn, since=since, host=host)


@app.get("/api/documentary")
def api_documentary(since: str = Query("all"), host: str | None = Query(None),
                    engine: str | None = Query(None)):
    from . import documentary as D
    conn = _conn()
    return D.narrate(conn, since=since, host=host, engine=engine or D.DOC_ENGINE)


@app.get("/api/capabilities")
def api_capabilities():
    from . import documentary as D
    return {"ollama": D.ollama_status()}


@app.get("/api/spend")
def api_spend(
    by: str = Query("project"),
    since: str = Query("all"),
    limit: int = Query(50),
    host: str | None = Query(None),
):
    conn = _conn()
    rows = A.spend_by(conn, by, since=since, limit=limit, host=host)
    keymap = {
        "project":       ["project_label", "project_path", "turns", "tokens", "usd"],
        "project_label": ["project_label", "paths", "turns", "tokens", "usd"],
        "model":         ["model", "turns", "tokens", "usd"],
        "day":           ["day", "turns", "tokens", "usd"],
        "hour":          ["hour", "turns", "tokens", "usd"],
        "session":       ["session_id", "project", "turns", "tokens", "usd"],
        "tool":          ["tool_name", "calls", "turns_using", "input_chars"],
        "host":          ["host", "sessions", "turns", "tokens", "usd"],
    }
    if by not in keymap:
        raise HTTPException(400, f"unknown by={by}")
    out = _rows_to_dicts(rows, keymap[by])
    for d in out:
        if "day" in d and d["day"] is not None:
            d["day"] = str(d["day"])
        if "hour" in d and d["hour"] is not None:
            d["hour"] = d["hour"].isoformat() if hasattr(d["hour"], "isoformat") else str(d["hour"])
    return out


@app.get("/api/top")
def api_top(metric: str = "cost", n: int = 20, since: str = "all",
            host: str | None = Query(None)):
    conn = _conn()
    rows = A.top_turns(conn, metric=metric, n=n, since=since, host=host)
    keys = ["uuid", "ts", "project", "session_id", "model",
            "input_tokens", "output_tokens", "cache_write", "cache_read", "total_usd"]
    out = _rows_to_dicts(rows, keys)
    for d in out:
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
    return out


@app.get("/api/projects/{name_or_path}")
def api_project(name_or_path: str):
    conn = _conn()
    data = A.project_drilldown(conn, name_or_path)
    if not data:
        raise HTTPException(404, "no such project")
    data["models"] = [
        {"model": m, "turns": t, "usd": u} for m, t, u in data["models"]
    ]
    data["sessions_detail"] = [
        {
            "session_id": sid,
            "first_ts": ft.isoformat() if ft else None,
            "last_ts": lt.isoformat() if lt else None,
            "turns": tr,
            "usd": u,
        }
        for sid, ft, lt, tr, u in data["sessions_detail"]
    ]
    return data


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str):
    conn = _conn()
    rows = A.session_trace(conn, session_id)
    if not rows:
        raise HTTPException(404, "no such session")
    keys = ["ts", "model", "input_tokens", "output_tokens",
            "cache_write", "cache_read", "total_usd", "tools"]
    out = _rows_to_dicts(rows, keys)
    for d in out:
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
    return out


@app.get("/api/cache")
def api_cache(host: str | None = Query(None)):
    conn = _conn()
    rows = A.cache_efficiency(conn, host=host)
    return _rows_to_dicts(rows,
                          ["model", "cache_read", "cache_write",
                           "uncached_input", "cache_hit_pct"])


@app.get("/api/calendar")
def api_calendar(days_back: int = 365, host: str | None = Query(None)):
    conn = _conn()
    rows = A.calendar_heatmap(conn, days_back=days_back, host=host)
    return [
        {"day": str(d), "usd": float(u), "turns": int(t), "tokens": int(tok or 0)}
        for d, u, t, tok in rows
    ]


@app.get("/api/heatmap")
def api_heatmap(since: str = "all", host: str | None = Query(None)):
    conn = _conn()
    rows = A.hour_dow_heatmap(conn, since=since, host=host)
    return [{"dow": int(d), "hour": int(h), "turns": int(t), "usd": float(u or 0)}
            for d, h, t, u in rows]


@app.get("/api/cache_savings")
def api_cache_savings(since: str = "all", host: str | None = Query(None)):
    conn = _conn()
    return A.cache_savings(conn, since=since, host=host)


@app.get("/api/burn_rate")
def api_burn_rate(window_minutes: int = 60, host: str | None = Query(None)):
    conn = _conn()
    return A.burn_rate(conn, window_minutes=window_minutes, host=host)


@app.get("/api/tool_costs")
def api_tool_costs(since: str = "all", host: str | None = Query(None)):
    conn = _conn()
    rows = A.tool_cost_attribution(conn, since=since, host=host)
    return [{"tool_name": t, "turns_using": int(tu), "total_calls": int(c),
             "avg_turn_usd": float(a), "total_turn_usd": float(s)}
            for t, tu, c, a, s in rows]


@app.get("/api/outliers")
def api_outliers(z_threshold: float = 2.0, host: str | None = Query(None)):
    conn = _conn()
    return A.outlier_sessions(conn, z_threshold=z_threshold, host=host)


@app.get("/api/forecast")
def api_forecast(host: str | None = Query(None)):
    conn = _conn()
    return A.monthly_forecast(conn, host=host)


@app.get("/api/branches")
def api_branches(since: str = "all", host: str | None = Query(None), limit: int = 50):
    conn = _conn()
    rows = A.branch_spend(conn, since=since, host=host, limit=limit)
    return [{"project": p, "branch": b, "turns": int(t),
             "sessions": int(s), "usd": float(u)}
            for p, b, t, s, u in rows]


@app.get("/api/turns")
def api_turns(
    model: str | None = Query(None),
    project: str | None = Query(None),
    host: str | None = Query(None),
    tool: str | None = Query(None),
    min_usd: float | None = Query(None),
    since: str = Query("all"),
    day: str | None = Query(None),
    timezone: str = Query("America/Los_Angeles"),
    limit: int = Query(100),
):
    conn = _conn()
    rows = A.turn_explorer(conn, model=model, project=project, host=host,
                           tool=tool, min_usd=min_usd, since=since, limit=limit,
                           day=day, timezone=timezone)
    keys = ["uuid", "ts", "host", "project", "session_id", "model",
            "input_tokens", "output_tokens", "cache_write", "cache_read",
            "total_usd", "n_tools", "tools"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
        d["total_usd"] = float(d["total_usd"])
        d["n_tools"] = int(d["n_tools"])
        out.append(d)
    return out


@app.get("/api/turns/{turn_uuid}")
def api_turn_detail(turn_uuid: str):
    conn = _conn()
    detail = A.turn_detail(conn, turn_uuid)
    if not detail:
        raise HTTPException(404, "turn not found")
    return detail


@app.get("/api/achievements")
def api_achievements(host: str | None = Query(None)):
    conn = _conn()
    return A.achievements(conn, host=host)


@app.get("/api/timeseries")
def api_timeseries(bucket: str = "day", since: str = "all",
                   host: str | None = Query(None)):
    conn = _conn()
    rows = A.timeseries(conn, bucket=bucket, since=since, host=host)
    out = []
    for b, m, t, u in rows:
        out.append({"bucket": str(b), "model": m, "turns": t, "usd": u})
    return out


@app.get("/api/spend_timeseries")
def api_spend_timeseries(
    bucket: str = Query("day"),
    stack: str = Query("none"),
    since: str = Query("all"),
    limit: int = Query(365),
    host: str | None = Query(None),
    timezone: str = Query("America/Los_Angeles"),
    series_limit: int = Query(12),
):
    conn = _conn()
    try:
        rows = A.grouped_timeseries(
            conn,
            bucket=bucket,
            stack=stack,
            since=since,
            limit=limit,
            host=host,
            timezone=timezone,
            series_limit=series_limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return [
        {
            "bucket": b.isoformat() if hasattr(b, "isoformat") else str(b),
            "series": s,
            "turns": int(t),
            "tokens": int(tok),
            "usd": float(u or 0),
        }
        for b, s, t, tok, u in rows
    ]


@app.get("/api/meta")
def api_meta():
    conn = _conn()
    return A.metadata(conn)


@app.get("/api/token_timeseries")
def api_token_timeseries(
    bucket: str = Query("day"),
    since: str = Query("all"),
    limit: int = Query(365),
    host: str | None = Query(None),
    timezone: str = Query("America/Los_Angeles"),
):
    conn = _conn()
    try:
        rows = A.token_type_timeseries(
            conn,
            bucket=bucket,
            since=since,
            limit=limit,
            host=host,
            timezone=timezone,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return [
        {
            "bucket": b.isoformat() if hasattr(b, "isoformat") else str(b),
            "series": s.replace("_tokens", "").replace("_", " "),
            "tokens": int(t or 0),
        }
        for b, s, t in rows
    ]


@app.get("/api/token_stats")
def api_token_stats(since: str = Query("all"), host: str | None = Query(None)):
    conn = _conn()
    return A.token_stats(conn, since=since, host=host)


@app.get("/api/quota")
def api_quota(metric: str = Query("usd"), host: str | None = Query(None)):
    if metric not in ("usd", "tokens"):
        raise HTTPException(400, "metric must be 'usd' or 'tokens'")
    conn = _conn()
    return A.quota_inference(conn, metric=metric, host=host)


_WEB_DIR = Path(__file__).parent / "_web"
if not _WEB_DIR.exists():
    # Dev install — fall back to repo-root /web sibling
    _WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
def index() -> HTMLResponse:
    html = (_WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


if _WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
