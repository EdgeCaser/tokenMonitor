"""Claude Code vs Codex: head-to-head analytics, plus Codex's measured quota.

Everything here reads the v_*_all views, so it sees both providers no matter
which provider lens the connection has. `since` / `host` filters still apply.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import duckdb

from .analytics import (
    WINDOW_SECONDS,
    _build_filter,
    _local_ts_expr,
    _window_report,
    parse_window,
)

PROVIDERS = ("claude", "codex")


# ---------------------------------------------------------------------------
# tool taxonomy
# ---------------------------------------------------------------------------
# Claude and Codex name their tools differently and Codex does most of its
# reading and searching through the shell. To compare what the two agents
# actually spend their calls on, both are mapped onto one set of verbs, and
# shell calls are classified by the command they run.

_TOOL_CATEGORY = {
    # Claude Code
    "Read": "read", "NotebookRead": "read",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Grep": "search", "Glob": "search", "LS": "search", "ToolSearch": "search",
    "WebSearch": "web", "WebFetch": "web",
    "Task": "delegate", "Agent": "delegate", "SendMessage": "delegate",
    "TodoWrite": "plan", "TaskCreate": "plan", "TaskUpdate": "plan",
    "EnterPlanMode": "plan", "ExitPlanMode": "plan",
    "Bash": "shell", "BashOutput": "shell", "KillShell": "shell", "PowerShell": "shell",
    # Codex
    "apply_patch": "edit",
    "view_image": "read",
    "web_search": "web", "web__run": "web",
    "spawn_agent": "delegate", "send_message": "delegate", "wait_agent": "delegate",
    "list_agents": "delegate", "followup_task": "delegate", "close_agent": "delegate",
    "update_plan": "plan",
    "exec_command": "shell", "shell_command": "shell", "shell": "shell",
    "local_shell": "shell", "exec": "shell", "write_stdin": "shell",
    "js": "code", "mcp__node_repl__js": "code",
    "wait": "wait", "sleep": "wait",
    "tool_search": "search",
    "image_generation": "create",
}

_SHELL_VERBS = [
    ("test", re.compile(r"^(pytest|python -m pytest|npm (run )?test|pnpm test|yarn test|cargo test|go test|jest|vitest)\b")),
    ("git", re.compile(r"^(git|gh)\b")),
    ("search", re.compile(r"^(rg|grep|find|fd|ls|dir|tree|Get-ChildItem|Select-String|gci)\b", re.I)),
    ("read", re.compile(r"^(cat|head|tail|sed -n|less|more|type|Get-Content|nl|wc|bat)\b", re.I)),
    ("build", re.compile(r"^(npm|pnpm|yarn|pip|uv|cargo|go|make|dotnet|python|node|npx|tsc)\b")),
]
_CMD_RE = re.compile(r'''(?:"cmd"|"command"|\bcmd|\bcommand)\s*:\s*\\?"(?:\[\s*\\?")?((?:[^"\\]|\\.)*)''')


def _shell_verb(preview: str) -> str:
    m = _CMD_RE.search(preview or "")
    if not m:
        return "shell"
    cmd = m.group(1).replace('\\"', '"').strip()
    # peel wrappers: `bash -lc '...'`, `powershell -Command ...`, `cd x &&`
    cmd = re.sub(r"^(bash|sh|zsh|pwsh|powershell)(\.exe)?\s+(-\w+\s+)*['\"]?", "", cmd, flags=re.I)
    cmd = re.sub(r"^cd\s+\S+\s*(&&|;)\s*", "", cmd)
    for verb, rx in _SHELL_VERBS:
        if rx.search(cmd):
            return verb
    return "shell"


def tool_category(name: str, preview: str = "") -> str:
    if name.startswith("mcp__") and name not in _TOOL_CATEGORY:
        return "mcp"
    cat = _TOOL_CATEGORY.get(name, "other")
    if cat == "shell":
        return _shell_verb(preview)
    return cat


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _f(x) -> float:
    return float(x or 0)


def _ratio(a, b) -> float | None:
    return (float(a) / float(b)) if b else None


# (key, label, higher_is_better) — None means "no winner, just a difference".
SCORECARD = [
    ("usd", "API-equivalent spend", None),
    ("turns", "model responses", None),
    ("prompts", "prompts", None),
    ("sessions", "sessions", None),
    ("projects", "projects touched", None),
    ("active_days", "active days", None),
    ("usd_per_prompt", "$ per prompt", False),
    # Responses and sessions differ in kind between the tools (a Codex
    # response is usually one tool step; a Claude session runs for hours), so
    # these show the difference without declaring a winner.
    ("usd_per_turn", "$ per response", None),
    ("usd_per_session", "$ per session", None),
    ("turns_per_prompt", "responses per prompt (autonomy)", None),
    ("output_per_turn", "output tokens per response", None),
    ("cache_hit_pct", "cache hit rate %", True),
    ("usd_per_mtok_out", "$ per 1M output tokens (all-in)", False),
    ("reasoning_share_pct", "reasoning share of output %", None),
    ("tools_per_turn", "tool calls per response", None),
    ("subagent_share_pct", "subagent share of spend %", None),
    ("median_session_min", "median session length (min)", None),
]


# ---------------------------------------------------------------------------
# versus
# ---------------------------------------------------------------------------

def versus(
    conn: duckdb.DuckDBPyConnection,
    since: str | None = None,
    host: str | None = None,
    timezone_name: str | None = "America/Los_Angeles",
    handoff_gap_minutes: int = 120,
) -> dict:
    where, params = _build_filter(since, host)
    local_ts = _local_ts_expr(timezone_name)

    base = {
        r[0]: r for r in conn.execute(
            f"""
            SELECT provider,
                   COUNT(*) AS turns,
                   COUNT(DISTINCT session_id) AS sessions,
                   COUNT(DISTINCT project_label) AS projects,
                   COUNT(DISTINCT CAST({local_ts} AS DATE)) AS active_days,
                   COUNT(DISTINCT model) AS models,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_write_5m + cache_write_1h) AS cache_write,
                   SUM(cache_read) AS cache_read,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   SUM(total_usd) AS usd,
                   SUM(CASE WHEN is_sidechain THEN total_usd ELSE 0 END) AS sidechain_usd,
                   MIN(ts) AS first_ts,
                   MAX(ts) AS last_ts
            FROM v_turn_cost_all {where}
            GROUP BY provider
            """,
            params,
        ).fetchall()
    }

    tool_where, tool_params = _build_filter(since, host, ts_col="t.ts")
    tool_rows = conn.execute(
        f"""
        SELECT t.provider, tc.tool_name, tc.input_preview
        FROM v_billable_tool_calls_all tc
        JOIN v_turn_cost_all t ON t.uuid = tc.turn_uuid
        {tool_where}
        """,
        tool_params,
    ).fetchall()

    # is_prompt arrives with the first ingest after upgrading; a read-only
    # server can see the DB before that, so fall back to counting every row.
    ucols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_turns'"
    ).fetchall()}
    prompt_filter = "AND is_prompt IS NOT FALSE" if "is_prompt" in ucols else ""
    prompt_rows = conn.execute(
        f"""
        SELECT CASE WHEN uuid LIKE 'codex-user:%' THEN 'codex' ELSE 'claude' END AS provider,
               COUNT(*)
        FROM user_turns
        WHERE session_id IN (SELECT DISTINCT session_id FROM v_turn_cost_all {where})
          {prompt_filter}
        GROUP BY 1
        """,
        params,
    ).fetchall()
    prompts = {p: n for p, n in prompt_rows}

    sessions = conn.execute(
        f"""
        SELECT provider, session_id, ANY_VALUE(project_label) AS project,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts, SUM(total_usd) AS usd
        FROM v_turn_cost_all {where}
        GROUP BY provider, session_id
        ORDER BY first_ts
        """,
        params,
    ).fetchall()

    # -- per-provider scorecard -------------------------------------------
    tools_by_p: dict[str, int] = defaultdict(int)
    cats: dict[str, dict[str, int]] = {p: defaultdict(int) for p in PROVIDERS}
    raw_tools: dict[str, dict[str, int]] = {p: defaultdict(int) for p in PROVIDERS}
    for prov, name, preview in tool_rows:
        tools_by_p[prov] += 1
        cats.setdefault(prov, defaultdict(int))[tool_category(name, preview or "")] += 1
        raw_tools.setdefault(prov, defaultdict(int))[name] += 1

    durations: dict[str, list[float]] = defaultdict(list)
    for prov, _sid, _proj, first, last, _usd in sessions:
        durations[prov].append((last - first).total_seconds() / 60.0)

    stats: dict[str, dict] = {}
    for prov in PROVIDERS:
        r = base.get(prov)
        if r is None:
            stats[prov] = None
            continue
        (_p, turns, n_sessions, projects, active_days, models, inp, out, cw, cr,
         reasoning, usd, side_usd, first_ts, last_ts) = r
        n_prompts = prompts.get(prov, 0)
        denom_in = _f(inp) + _f(cw) + _f(cr)
        d = sorted(durations[prov])
        stats[prov] = {
            "turns": int(turns), "sessions": int(n_sessions), "projects": int(projects),
            "active_days": int(active_days), "models": int(models), "prompts": int(n_prompts),
            "input_tokens": int(inp or 0), "output_tokens": int(out or 0),
            "cache_write": int(cw or 0), "cache_read": int(cr or 0),
            "reasoning_tokens": int(reasoning or 0),
            "usd": _f(usd),
            "usd_per_prompt": _ratio(usd, n_prompts),
            "usd_per_turn": _ratio(usd, turns),
            "usd_per_session": _ratio(usd, n_sessions),
            "turns_per_prompt": _ratio(turns, n_prompts),
            "output_per_turn": _ratio(out, turns),
            "cache_hit_pct": 100.0 * _f(cr) / denom_in if denom_in else None,
            "usd_per_mtok_out": _ratio(_f(usd) * 1e6, out),
            "reasoning_share_pct": (100.0 * _f(reasoning) / _f(out)) if out and prov == "codex" else None,
            "tools_per_turn": _ratio(tools_by_p.get(prov, 0), turns),
            "subagent_share_pct": 100.0 * _f(side_usd) / _f(usd) if usd else None,
            "median_session_min": d[len(d) // 2] if d else None,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
        }

    scorecard = []
    for key, label, higher_better in SCORECARD:
        a = (stats.get("claude") or {}).get(key)
        b = (stats.get("codex") or {}).get(key)
        winner = None
        if higher_better is not None and a is not None and b is not None and a != b:
            winner = ("claude" if a > b else "codex") if higher_better else ("claude" if a < b else "codex")
        scorecard.append({"key": key, "label": label, "claude": a, "codex": b,
                          "winner": winner,
                          "ratio": (b / a) if (a and b is not None) else None})

    # -- tool mix -----------------------------------------------------------
    all_cats = sorted({c for p in cats.values() for c in p})
    tool_mix = []
    for c in all_cats:
        row = {"category": c}
        for prov in PROVIDERS:
            tot = sum(cats[prov].values())
            row[prov] = cats[prov].get(c, 0)
            row[f"{prov}_pct"] = 100.0 * cats[prov].get(c, 0) / tot if tot else 0.0
        tool_mix.append(row)
    tool_mix.sort(key=lambda r: -(r["claude_pct"] + r["codex_pct"]))
    top_tools = {
        prov: sorted(({"tool": k, "calls": v} for k, v in raw_tools[prov].items()),
                     key=lambda r: -r["calls"])[:12]
        for prov in PROVIDERS
    }

    # -- weekly share: the migration curve ----------------------------------
    share = conn.execute(
        f"""
        SELECT date_trunc('week', {local_ts}) AS wk,
               SUM(CASE WHEN provider = 'claude' THEN total_usd ELSE 0 END) AS claude,
               SUM(CASE WHEN provider = 'codex'  THEN total_usd ELSE 0 END) AS codex,
               SUM(CASE WHEN provider = 'claude' THEN 1 ELSE 0 END) AS claude_turns,
               SUM(CASE WHEN provider = 'codex'  THEN 1 ELSE 0 END) AS codex_turns
        FROM v_turn_cost_all {where}
        GROUP BY wk ORDER BY wk
        """,
        params,
    ).fetchall()
    weekly = [
        {"week": wk.date().isoformat(), "claude_usd": _f(c), "codex_usd": _f(x),
         "claude_turns": int(ct), "codex_turns": int(xt),
         "codex_share_pct": 100.0 * _f(x) / (_f(c) + _f(x)) if (_f(c) + _f(x)) else None}
        for wk, c, x, ct, xt in share
    ]

    # -- circadian: who gets the late shift ---------------------------------
    hours = conn.execute(
        f"""
        SELECT EXTRACT(hour FROM {local_ts}) AS h, provider, SUM(total_usd), COUNT(*)
        FROM v_turn_cost_all {where}
        GROUP BY 1, 2 ORDER BY 1
        """,
        params,
    ).fetchall()
    by_hour = [{"hour": h, "claude_usd": 0.0, "codex_usd": 0.0,
                "claude_turns": 0, "codex_turns": 0} for h in range(24)]
    for h, prov, usd, n in hours:
        by_hour[int(h)][f"{prov}_usd"] = _f(usd)
        by_hour[int(h)][f"{prov}_turns"] = int(n)

    # -- projects: shared ground and loyalty ---------------------------------
    proj = conn.execute(
        f"""
        SELECT project_label,
               SUM(CASE WHEN provider = 'claude' THEN total_usd ELSE 0 END) AS claude,
               SUM(CASE WHEN provider = 'codex'  THEN total_usd ELSE 0 END) AS codex,
               COUNT(DISTINCT CASE WHEN provider = 'claude' THEN session_id END) AS cs,
               COUNT(DISTINCT CASE WHEN provider = 'codex'  THEN session_id END) AS xs
        FROM v_turn_cost_all {where}
        GROUP BY project_label
        ORDER BY claude + codex DESC
        """,
        params,
    ).fetchall()
    projects = []
    for label, c, x, cs, xs in proj:
        c, x = _f(c), _f(x)
        tot = c + x
        projects.append({
            "project": label, "claude_usd": c, "codex_usd": x,
            "claude_sessions": int(cs), "codex_sessions": int(xs),
            # -1 = all Claude, +1 = all Codex
            "lean": ((x - c) / tot) if tot else 0.0,
        })
    shared = [p for p in projects if p["claude_usd"] > 0 and p["codex_usd"] > 0]

    # -- handoffs: switching tools mid-task ----------------------------------
    handoffs = _handoffs(sessions, handoff_gap_minutes)

    # -- daily duels ----------------------------------------------------------
    duels = conn.execute(
        f"""
        WITH d AS (
            SELECT CAST({local_ts} AS DATE) AS day,
                   SUM(CASE WHEN provider = 'claude' THEN total_usd ELSE 0 END) AS c,
                   SUM(CASE WHEN provider = 'codex'  THEN total_usd ELSE 0 END) AS x
            FROM v_turn_cost_all {where}
            GROUP BY day
        )
        SELECT
            COUNT(*) FILTER (WHERE c > 0 AND x > 0) AS both_days,
            COUNT(*) FILTER (WHERE c > 0 AND x > 0 AND c > x) AS claude_heavier,
            COUNT(*) FILTER (WHERE c > 0 AND x > 0 AND x > c) AS codex_heavier,
            COUNT(*) FILTER (WHERE c > 0 AND x = 0) AS claude_only,
            COUNT(*) FILTER (WHERE x > 0 AND c = 0) AS codex_only
        FROM d
        """,
        params,
    ).fetchone()

    # -- codex reasoning effort ladder ----------------------------------------
    effort = conn.execute(
        f"""
        SELECT COALESCE(reasoning_effort, 'default') AS effort, COUNT(*) AS turns,
               SUM(total_usd) AS usd, AVG(reasoning_tokens) AS avg_reasoning,
               AVG(output_tokens) AS avg_output
        FROM v_turn_cost_all {where + (' AND ' if where else 'WHERE ')} provider = 'codex'
        GROUP BY 1
        """,
        params,
    ).fetchall()
    order = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "default": 5}
    effort_ladder = sorted(
        ({"effort": e, "turns": int(n), "usd": _f(u), "usd_per_turn": _ratio(u, n),
          "avg_reasoning_tokens": _f(ar), "avg_output_tokens": _f(ao)}
         for e, n, u, ar, ao in effort),
        key=lambda r: order.get(r["effort"], 9),
    )

    models = conn.execute(
        f"""
        SELECT provider, model, COUNT(*) AS turns, SUM(total_usd) AS usd,
               SUM(output_tokens) AS out
        FROM v_turn_cost_all {where}
        GROUP BY provider, model ORDER BY usd DESC
        """,
        params,
    ).fetchall()

    return {
        "filters": {"since": since, "host": host},
        "stats": stats,
        "scorecard": scorecard,
        "tool_mix": tool_mix,
        "top_tools": top_tools,
        "weekly": weekly,
        "by_hour": by_hour,
        "projects": projects[:40],
        "shared_projects": len(shared),
        "exclusive_projects": {
            "claude": sum(1 for p in projects if p["codex_usd"] == 0 and p["claude_usd"] > 0),
            "codex": sum(1 for p in projects if p["claude_usd"] == 0 and p["codex_usd"] > 0),
        },
        "handoffs": handoffs,
        "duels": dict(zip(["both_days", "claude_heavier", "codex_heavier",
                           "claude_only", "codex_only"], [int(x or 0) for x in duels])),
        "effort_ladder": effort_ladder,
        "models": [{"provider": p, "model": m, "turns": int(n), "usd": _f(u),
                    "output_tokens": int(o or 0)} for p, m, n, u, o in models],
    }


def _handoffs(sessions: list[tuple], gap_minutes: int) -> dict:
    """Count tool switches on the same project: a session of one provider that
    starts within `gap_minutes` of the other provider's last activity there.

    A switch that follows an expensive session is flagged as a possible
    "rescue": you gave up on one agent and handed the problem to the other.
    """
    gap = timedelta(minutes=gap_minutes)
    by_project: dict[str, list[tuple]] = defaultdict(list)
    for prov, sid, proj, first, last, usd in sessions:
        by_project[proj].append((first, last, prov, _f(usd)))
    counts = {"claude_to_codex": 0, "codex_to_claude": 0}
    per_project: dict[str, dict[str, int]] = defaultdict(lambda: {"claude_to_codex": 0, "codex_to_claude": 0})
    recent: list[dict] = []
    for proj, rows in by_project.items():
        rows.sort()
        last_seen: dict[str, tuple] = {}
        for first, last, prov, usd in rows:
            other = "codex" if prov == "claude" else "claude"
            prev = last_seen.get(other)
            mine = last_seen.get(prov)
            if prev is not None and timedelta(0) <= first - prev[0] <= gap \
                    and (mine is None or mine[0] < prev[0]):
                key = f"{other}_to_{prov}"
                counts[key] += 1
                per_project[proj][key] += 1
                recent.append({"project": proj, "from": other, "to": prov,
                               "at": first.isoformat(),
                               "gap_min": (first - prev[0]).total_seconds() / 60.0,
                               "from_session_usd": prev[1]})
            prev_last = last_seen.get(prov)
            last_seen[prov] = (max(last, prev_last[0]) if prev_last else last, usd)
    recent.sort(key=lambda r: r["at"], reverse=True)
    top = sorted(({"project": p, **v, "total": sum(v.values())} for p, v in per_project.items()),
                 key=lambda r: -r["total"])[:10]
    return {"gap_minutes": gap_minutes, **counts,
            "total": sum(counts.values()), "top_projects": top, "recent": recent[:15]}


# ---------------------------------------------------------------------------
# Codex rate limits: measured, not inferred
# ---------------------------------------------------------------------------

def _epoch(dt: datetime) -> float:
    """Epoch seconds for a naive-UTC datetime (how tokmon stores ts).
    Plain .timestamp() would read a naive value as local time."""
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _from_epoch(x: float) -> datetime:
    return datetime.fromtimestamp(x, timezone.utc).replace(tzinfo=None)


def _window_label(minutes: int | None) -> str:
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "weekly"
    if minutes is None:
        return "?"
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def codex_limits(
    conn: duckdb.DuckDBPyConnection,
    now: datetime | None = None,
) -> dict:
    """What Codex's own rate-limit meter says, joined with API-equivalent cost.

    Codex logs `used_percent` of each limit window with every response. Paired
    with tokmon's API-$ pricing of the same responses, each window yields the
    implied size of a full (100%) window in API dollars, measured rather than
    inferred. The same peak-clustering inference tokmon runs for Claude is then
    run on Codex usage, so its estimate can be graded against the real value.

    Account-wide: all hosts are combined, since the meter is per account.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        samples = conn.execute(
            """
            SELECT ts, limit_id, plan_type, window_minutes, used_percent, resets_at,
                   reached_type, host
            FROM rate_limit_samples
            WHERE provider = 'codex' AND resets_at IS NOT NULL
            ORDER BY ts
            """
        ).fetchall()
    except duckdb.CatalogException:
        samples = []
    notes = [
        "Measured: Codex writes its account rate-limit meter into every session log.",
        "Implied capacity = API-equivalent $ of Codex responses in a window divided by the "
        "window's used %, fit across every meter reading in the window.",
        "Usage outside these logs (Codex cloud tasks, other unsynced machines, the ChatGPT "
        "app) moves the meter without adding $, which makes the implied capacity read low.",
    ]
    if not samples:
        return {"windows": [], "current": [], "calibration": {}, "limit_hits": [],
                "by_plan": [], "notes": notes}

    turns = conn.execute(
        "SELECT ts, total_usd FROM v_turn_cost_all WHERE provider = 'codex' ORDER BY ts"
    ).fetchall()
    t_times = [_epoch(t) for t, _ in turns]
    t_usd = [_f(u) for _, u in turns]
    cum = [0.0]
    for u in t_usd:
        cum.append(cum[-1] + u)

    import bisect

    def usd_between(a: datetime, b: datetime) -> float:
        if b <= a:
            return 0.0
        i = bisect.bisect_left(t_times, _epoch(a))
        j = bisect.bisect_right(t_times, _epoch(b))
        return cum[j] - cum[i]

    # Group readings into concrete windows: same length + same reset instant.
    # resets_at can jitter by a second or two between readings, so snap it.
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for s in samples:
        ts, limit_id, plan, wmin, used, resets_at, reached, host = s
        snap = _from_epoch(round(_epoch(resets_at) / 600) * 600)
        groups[(limit_id, wmin, snap)].append(s)

    windows = []
    for (limit_id, wmin, reset), rows in sorted(groups.items(), key=lambda kv: kv[0][2]):
        if not wmin:
            continue
        start = reset - timedelta(minutes=wmin)
        # Each thread reports the meter as of its own last response, so
        # parallel threads interleave stale readings (3% right after another
        # thread saw 10%), and a few arrive after the reset. The meter can't
        # go down inside a window, so fit its running maximum.
        live = sorted((r for r in rows if start <= r[0] <= reset), key=lambda r: r[0])
        if not live:
            continue
        pts = []
        envelope = 0.0
        for r in live:
            envelope = max(envelope, float(r[4]))
            pts.append((usd_between(start, r[0]), envelope))
        max_used = envelope
        usd_so_far = usd_between(start, live[-1][0])
        # least squares through the origin: used% = k * usd
        sxx = sum(x * x for x, _ in pts)
        sxy = sum(x * y for x, y in pts)
        k = sxy / sxx if sxx else None
        implied = (100.0 / k) if k else None
        # residual spread tells how well $ tracks the meter in this window
        resid = None
        if k and len(pts) > 2:
            resid = (sum((y - k * x) ** 2 for x, y in pts) / (len(pts) - 1)) ** 0.5
        plans = {r[2] for r in live if r[2]}
        windows.append({
            "limit_id": limit_id,
            "window": _window_label(wmin),
            "window_minutes": wmin,
            "start": start.isoformat(),
            "resets_at": reset.isoformat(),
            "plan": sorted(plans)[-1] if plans else None,
            "readings": len(live),
            "max_used_pct": max_used,
            "usd": usd_so_far,
            "implied_full_usd": implied,
            "fit_resid_pct": resid,
            "reliable": bool(implied and max_used >= 10 and len(pts) >= 3),
            "hit_limit": any(r[6] for r in live) or max_used >= 100,
            "hosts": sorted({r[7] for r in live}),
        })

    # -- current windows -------------------------------------------------------
    current = []
    latest: dict[tuple, dict] = {}
    for w in windows:
        latest[(w["limit_id"], w["window_minutes"])] = w
    for (limit_id, wmin), w in latest.items():
        reset = datetime.fromisoformat(w["resets_at"])
        if reset <= now:
            continue
        start = reset - timedelta(minutes=wmin)
        elapsed_h = max((now - start).total_seconds() / 3600.0, 1e-9)
        # burn over the last 24h (or window-to-date if shorter)
        look = min(timedelta(hours=24), now - start)
        recent_usd = usd_between(now - look, now)
        rate_usd_h = recent_usd / max(look.total_seconds() / 3600.0, 1e-9)
        capacity = _typical_capacity(windows, wmin, w["plan"])
        used_now = w["max_used_pct"]
        proj_used = None
        exhausts_at = None
        if capacity:
            hours_left = (reset - now).total_seconds() / 3600.0
            proj_used = used_now + 100.0 * rate_usd_h * hours_left / capacity
            if rate_usd_h > 0 and used_now < 100:
                h_to_full = (100.0 - used_now) / 100.0 * capacity / rate_usd_h
                if h_to_full < hours_left:
                    exhausts_at = (now + timedelta(hours=h_to_full)).isoformat()
        current.append({
            "limit_id": limit_id, "window": _window_label(wmin), "plan": w["plan"],
            "used_pct": used_now, "resets_at": w["resets_at"],
            "hours_to_reset": (reset - now).total_seconds() / 3600.0,
            "elapsed_hours": elapsed_h,
            "burn_usd_per_hour_24h": rate_usd_h,
            "typical_full_usd": capacity,
            "projected_used_pct_at_reset": proj_used,
            "projected_exhaustion": exhausts_at,
        })

    # -- per plan: what a full window is worth ---------------------------------
    by_plan: dict[tuple, list[float]] = defaultdict(list)
    for w in windows:
        if w["reliable"]:
            by_plan[(w["plan"], w["window"])].append(w["implied_full_usd"])
    plan_rows = []
    for (plan, win), vals in sorted(by_plan.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        vals.sort()
        plan_rows.append({"plan": plan, "window": win, "n_windows": len(vals),
                          "median_full_usd": vals[len(vals) // 2],
                          "min_full_usd": vals[0], "max_full_usd": vals[-1]})

    # -- calibration: grade the Claude-style inference on Codex -----------------
    calibration = {}
    for label in ("5h", "weekly"):
        wmin = WINDOW_SECONDS[label] // 60
        truth = _typical_capacity(windows, wmin, None)
        if not truth or not t_times:
            continue
        rep = _window_report(t_times, t_usd, WINDOW_SECONDS[label])
        est = rep.get("ceiling_estimate")
        calibration[label] = {
            "measured_full_usd": truth,
            "inferred_ceiling_usd": est,
            "inferred_lower_bound_usd": rep.get("lower_bound"),
            "confidence": rep.get("confidence"),
            "error_pct": (100.0 * (est - truth) / truth) if est else None,
            "lower_bound_pct_of_truth": (100.0 * rep["lower_bound"] / truth) if rep.get("lower_bound") else None,
            "verdict": _verdict(est, rep.get("lower_bound"), truth),
        }

    hits = [
        {"ts": s[0].isoformat(), "window": _window_label(s[3]), "used_pct": s[4],
         "reached_type": s[6], "plan": s[2], "host": s[7]}
        for s in samples if s[6]
    ]

    return {
        "windows": windows[-60:],
        "current": sorted(current, key=lambda c: c["hours_to_reset"]),
        "by_plan": plan_rows,
        "calibration": calibration,
        "limit_hits": hits[-30:],
        "n_samples": len(samples),
        "notes": notes,
    }


def _typical_capacity(windows: list[dict], wmin: int, plan: str | None) -> float | None:
    vals = sorted(
        w["implied_full_usd"] for w in windows
        if w["reliable"] and w["window_minutes"] == wmin and (plan is None or w["plan"] == plan)
    )
    if not vals and plan is not None:
        return _typical_capacity(windows, wmin, None)
    return vals[len(vals) // 2] if vals else None


def _verdict(est: float | None, lower: float | None, truth: float) -> str:
    if est is None:
        if lower and lower <= truth * 1.05:
            return "no ceiling claimed; lower bound held"
        if lower:
            return "no ceiling claimed; lower bound overshot"
        return "no data"
    err = abs(est - truth) / truth
    if err <= 0.10:
        return "nailed it"
    if err <= 0.25:
        return "close"
    return "overshot" if est > truth else "undershot"
