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
def api_summary(since: str = Query("all")):
    conn = A.connect_with_views()
    return A.summary(conn, since=since)


@app.get("/api/spend")
def api_spend(
    by: str = Query("project"),
    since: str = Query("all"),
    limit: int = Query(50),
):
    conn = A.connect_with_views()
    rows = A.spend_by(conn, by, since=since, limit=limit)
    keymap = {
        "project": ["project_label", "project_path", "turns", "tokens", "usd"],
        "model":   ["model", "turns", "tokens", "usd"],
        "day":     ["day", "turns", "tokens", "usd"],
        "session": ["session_id", "project", "turns", "tokens", "usd"],
        "tool":    ["tool_name", "calls", "turns_using", "input_chars"],
        "host":    ["host", "sessions", "turns", "tokens", "usd"],
    }
    if by not in keymap:
        raise HTTPException(400, f"unknown by={by}")
    out = _rows_to_dicts(rows, keymap[by])
    for d in out:
        if "day" in d and d["day"] is not None:
            d["day"] = str(d["day"])
    return out


@app.get("/api/top")
def api_top(metric: str = "cost", n: int = 20, since: str = "all"):
    conn = A.connect_with_views()
    rows = A.top_turns(conn, metric=metric, n=n, since=since)
    keys = ["uuid", "ts", "project", "session_id", "model",
            "input_tokens", "output_tokens", "cache_write", "cache_read", "total_usd"]
    out = _rows_to_dicts(rows, keys)
    for d in out:
        d["ts"] = d["ts"].isoformat() if d["ts"] else None
    return out


@app.get("/api/projects/{name_or_path}")
def api_project(name_or_path: str):
    conn = A.connect_with_views()
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
    conn = A.connect_with_views()
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
def api_cache():
    conn = A.connect_with_views()
    rows = A.cache_efficiency(conn)
    return _rows_to_dicts(rows,
                          ["model", "cache_read", "cache_write",
                           "uncached_input", "cache_hit_pct"])


@app.get("/api/timeseries")
def api_timeseries(bucket: str = "day", since: str = "all"):
    conn = A.connect_with_views()
    rows = A.timeseries(conn, bucket=bucket, since=since)
    out = []
    for b, m, t, u in rows:
        out.append({"bucket": str(b), "model": m, "turns": t, "usd": u})
    return out


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
