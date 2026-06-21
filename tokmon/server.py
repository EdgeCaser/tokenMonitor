"""FastAPI server + static dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import analytics as A

app = FastAPI(title="tokmon", version="0.1.0")


def _rows_to_dicts(rows, keys):
    return [dict(zip(keys, r)) for r in rows]


@app.get("/api/summary")
def api_summary(since: str = Query("all"), host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.summary(conn, since=since, host=host)


@app.get("/api/spend")
def api_spend(
    by: str = Query("project"),
    since: str = Query("all"),
    limit: int = Query(50),
    host: str | None = Query(None),
):
    conn = A.connect_with_views(read_only=True)
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
    conn = A.connect_with_views(read_only=True)
    rows = A.top_turns(conn, metric=metric, n=n, since=since, host=host)
    keys = ["uuid", "ts", "project", "session_id", "model",
            "input_tokens", "output_tokens", "cache_write", "cache_read", "total_usd"]
    out = _rows_to_dicts(rows, keys)
    for d in out:
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
    return out


@app.get("/api/projects/{name_or_path}")
def api_project(name_or_path: str):
    conn = A.connect_with_views(read_only=True)
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
    conn = A.connect_with_views(read_only=True)
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
    conn = A.connect_with_views(read_only=True)
    rows = A.cache_efficiency(conn, host=host)
    return _rows_to_dicts(rows,
                          ["model", "cache_read", "cache_write",
                           "uncached_input", "cache_hit_pct"])


@app.get("/api/calendar")
def api_calendar(days_back: int = 365, host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    rows = A.calendar_heatmap(conn, days_back=days_back, host=host)
    return [{"day": str(d), "usd": float(u), "turns": int(t)} for d, u, t in rows]


@app.get("/api/heatmap")
def api_heatmap(since: str = "all", host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    rows = A.hour_dow_heatmap(conn, since=since, host=host)
    return [{"dow": int(d), "hour": int(h), "turns": int(t), "usd": float(u or 0)}
            for d, h, t, u in rows]


@app.get("/api/cache_savings")
def api_cache_savings(since: str = "all", host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.cache_savings(conn, since=since, host=host)


@app.get("/api/burn_rate")
def api_burn_rate(window_minutes: int = 60, host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.burn_rate(conn, window_minutes=window_minutes, host=host)


@app.get("/api/tool_costs")
def api_tool_costs(since: str = "all", host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    rows = A.tool_cost_attribution(conn, since=since, host=host)
    return [{"tool_name": t, "turns_using": int(tu), "total_calls": int(c),
             "avg_turn_usd": float(a), "total_turn_usd": float(s)}
            for t, tu, c, a, s in rows]


@app.get("/api/outliers")
def api_outliers(z_threshold: float = 2.0, host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.outlier_sessions(conn, z_threshold=z_threshold, host=host)


@app.get("/api/forecast")
def api_forecast(host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.monthly_forecast(conn, host=host)


@app.get("/api/branches")
def api_branches(since: str = "all", host: str | None = Query(None), limit: int = 50):
    conn = A.connect_with_views(read_only=True)
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
    limit: int = Query(100),
):
    conn = A.connect_with_views(read_only=True)
    rows = A.turn_explorer(conn, model=model, project=project, host=host,
                           tool=tool, min_usd=min_usd, since=since, limit=limit)
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
    conn = A.connect_with_views(read_only=True)
    detail = A.turn_detail(conn, turn_uuid)
    if not detail:
        raise HTTPException(404, "turn not found")
    return detail


@app.get("/api/achievements")
def api_achievements(host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    return A.achievements(conn, host=host)


@app.get("/api/timeseries")
def api_timeseries(bucket: str = "day", since: str = "all",
                   host: str | None = Query(None)):
    conn = A.connect_with_views(read_only=True)
    rows = A.timeseries(conn, bucket=bucket, since=since, host=host)
    out = []
    for b, m, t, u in rows:
        out.append({"bucket": str(b), "model": m, "turns": t, "usd": u})
    return out


@app.get("/api/quota")
def api_quota(metric: str = Query("usd"), host: str | None = Query(None)):
    if metric not in ("usd", "tokens"):
        raise HTTPException(400, "metric must be 'usd' or 'tokens'")
    conn = A.connect_with_views(read_only=True)
    return A.quota_inference(conn, metric=metric, host=host)


_WEB_DIR = Path(__file__).parent / "_web"
if not _WEB_DIR.exists():
    # Dev install — fall back to repo-root /web sibling
    _WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
def index() -> HTMLResponse:
    html = (_WEB_DIR / "index.html").read_text()
    return HTMLResponse(html)


if _WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
