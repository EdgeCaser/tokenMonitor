"""Rich-formatted CLI for tokmon."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import analytics as A
from . import config as cfg_mod
from . import ingest as I

# On Windows, stdout/stderr fall back to a legacy code page (cp1252) when output
# is redirected (a Scheduled Task or a captured pipe), which makes rich crash on
# non-ASCII glyphs (→ — …). Force UTF-8 so output never raises UnicodeEncodeError.
# POSIX (macOS / the Pi) is left untouched.
if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

console = Console()


def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _fmt_usd(x: float | None) -> str:
    if x is None:
        return "—"
    if x < 0.01:
        return f"${x:.4f}"
    return f"${x:,.2f}"


def _fmt_ts(ts) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M")


def _shortid(s: str, head: int = 8, tail: int = 4) -> str:
    if not s or len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


@click.group()
def cli():
    """tokmon — local analytics for Claude Code token usage."""


@cli.command()
@click.option("--full", is_flag=True, help="Wipe DB and re-ingest everything.")
@click.option("--projects-dir", "extra_dirs", multiple=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Extra project root(s). Repeatable. Adds to config-defined roots.")
@click.option("--host", "extra_host", default=None,
              help="Host label applied to all --projects-dir overrides this run.")
@click.option("--only-extras", is_flag=True,
              help="Ingest ONLY the --projects-dir paths; skip default + config roots.")
def ingest(full, extra_dirs, extra_host, only_extras):
    """Scan project roots and ingest new turns."""
    cfg = cfg_mod.load()
    if only_extras:
        host = extra_host or "extra"
        roots = [(Path(d).resolve(), host) for d in extra_dirs]
    else:
        roots = list(cfg_mod.iter_roots(cfg.all_roots()))
        if extra_dirs:
            host = extra_host or "extra"
            roots.extend((Path(d).resolve(), host) for d in extra_dirs)
    with console.status("[cyan]Ingesting…"):
        stats = I.full(roots=roots) if full else I.incremental(roots=roots)
    table = Table(title="Ingest result", show_header=False, box=None)
    table.add_row("roots",                str(len(roots)))
    table.add_row("files scanned",        _fmt_int(stats.files_scanned))
    table.add_row("files with new data",  _fmt_int(stats.files_with_new_data))
    table.add_row("new assistant turns",  _fmt_int(stats.new_turns))
    table.add_row("new user turns",       _fmt_int(stats.new_user_turns))
    table.add_row("new tool calls",       _fmt_int(stats.new_tool_calls))
    table.add_row("malformed lines",      _fmt_int(stats.malformed_lines))
    table.add_row("bytes read",           _fmt_int(stats.bytes_read))
    console.print(table)


@cli.command()
@click.option("--since", default="all", help="7d | 30d | 24h | all")
def summary(since):
    """Print top-level totals."""
    conn = A.connect_with_views()
    s = A.summary(conn, since=since)
    p = Panel.fit(
        f"[bold]turns[/bold]    {_fmt_int(s['turns']):>14}\n"
        f"[bold]sessions[/bold] {_fmt_int(s['sessions']):>14}\n"
        f"[bold]projects[/bold] {_fmt_int(s['projects']):>14}\n"
        f"[bold]models[/bold]   {_fmt_int(s['models']):>14}\n"
        f"\n"
        f"input        {_fmt_int(s['input_tokens']):>14}\n"
        f"output       {_fmt_int(s['output_tokens']):>14}\n"
        f"cache write  {_fmt_int(s['cache_write_5m'] + s['cache_write_1h']):>14}\n"
        f"cache read   {_fmt_int(s['cache_read']):>14}\n"
        f"\n"
        f"[bold green]total spend  {_fmt_usd(s['total_usd']):>14}[/bold green]",
        title=f"tokmon summary — since {since}",
        border_style="cyan",
    )
    console.print(p)

    rows = A.spend_by(conn, "model", since=since, limit=10)
    if rows:
        t = Table(title="Top models", show_header=True, header_style="bold")
        t.add_column("model"); t.add_column("turns", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("$ spend", justify="right")
        for model, turns, tokens, usd in rows:
            t.add_row(model, _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
        console.print(t)

    rows = A.spend_by(conn, "project", since=since, limit=10)
    if rows:
        t = Table(title="Top projects", show_header=True, header_style="bold")
        t.add_column("project"); t.add_column("turns", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("$ spend", justify="right")
        for label, _path, turns, tokens, usd in rows:
            t.add_row(label, _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
        console.print(t)


@cli.command()
@click.option("--by", "dimension",
              type=click.Choice(["project", "model", "day", "hour", "session", "tool", "host"]),
              default="project")
@click.option("--since", default="all")
@click.option("-n", "--limit", default=20)
def spend(dimension, since, limit):
    """Show spend grouped by project / model / day / session / tool."""
    conn = A.connect_with_views()
    rows = A.spend_by(conn, dimension, since=since, limit=limit)
    if not rows:
        console.print("[yellow]no data[/yellow]")
        return
    t = Table(title=f"spend by {dimension} — since {since}", header_style="bold")
    if dimension == "project":
        t.add_column("project"); t.add_column("path", style="dim")
        t.add_column("turns", justify="right"); t.add_column("tokens", justify="right")
        t.add_column("$ spend", justify="right")
        for label, path, turns, tokens, usd in rows:
            t.add_row(label, path, _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    elif dimension == "model":
        t.add_column("model"); t.add_column("turns", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("$ spend", justify="right")
        for model, turns, tokens, usd in rows:
            t.add_row(model, _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    elif dimension == "day":
        t.add_column("day"); t.add_column("turns", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("$ spend", justify="right")
        for day, turns, tokens, usd in rows:
            t.add_row(str(day), _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    elif dimension == "hour":
        t.add_column("hour"); t.add_column("turns", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("$ spend", justify="right")
        for hour, turns, tokens, usd in rows:
            label = hour.strftime("%Y-%m-%d %H:00") if hasattr(hour, "strftime") else str(hour)
            t.add_row(label, _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    elif dimension == "session":
        t.add_column("session"); t.add_column("project")
        t.add_column("turns", justify="right"); t.add_column("tokens", justify="right")
        t.add_column("$ spend", justify="right")
        for sid, proj, turns, tokens, usd in rows:
            t.add_row(_shortid(sid), proj or "—", _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    elif dimension == "tool":
        t.add_column("tool"); t.add_column("calls", justify="right")
        t.add_column("turns using", justify="right"); t.add_column("input chars", justify="right")
        for name, calls, turns_using, input_chars in rows:
            t.add_row(name, _fmt_int(calls), _fmt_int(turns_using), _fmt_int(input_chars))
    elif dimension == "host":
        t.add_column("host"); t.add_column("sessions", justify="right")
        t.add_column("turns", justify="right"); t.add_column("tokens", justify="right")
        t.add_column("$ spend", justify="right")
        for host, sessions, turns, tokens, usd in rows:
            t.add_row(host, _fmt_int(sessions), _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    console.print(t)


@cli.command()
@click.option("--metric",
              type=click.Choice(["cost", "tokens", "cache_write", "cache_read", "output"]),
              default="cost")
@click.option("-n", default=20)
@click.option("--since", default="all")
def top(metric, n, since):
    """Top N most expensive turns."""
    conn = A.connect_with_views()
    rows = A.top_turns(conn, metric=metric, n=n, since=since)
    t = Table(title=f"top {n} turns by {metric}", header_style="bold")
    t.add_column("uuid"); t.add_column("ts"); t.add_column("project")
    t.add_column("model")
    t.add_column("in", justify="right"); t.add_column("out", justify="right")
    t.add_column("cw", justify="right"); t.add_column("cr", justify="right")
    t.add_column("$", justify="right")
    for uuid, ts, proj, _sid, model, inp, out, cw, cr, usd in rows:
        t.add_row(
            _shortid(uuid), _fmt_ts(ts), proj, model,
            _fmt_int(inp), _fmt_int(out), _fmt_int(cw), _fmt_int(cr), _fmt_usd(usd),
        )
    console.print(t)


@cli.command()
@click.argument("name_or_path")
def project(name_or_path):
    """Drill into one project."""
    conn = A.connect_with_views()
    data = A.project_drilldown(conn, name_or_path)
    if not data:
        console.print(f"[red]no project matching[/red] {name_or_path!r}")
        return
    console.print(Panel.fit(
        f"[bold]{data['project_label']}[/bold]\n[dim]{data['project_path']}[/dim]\n\n"
        f"sessions  {_fmt_int(data['sessions'])}\n"
        f"turns     {_fmt_int(data['turns'])}\n"
        f"input     {_fmt_int(data['input_tokens'])}\n"
        f"output    {_fmt_int(data['output_tokens'])}\n"
        f"cache wr  {_fmt_int(data['cache_write_5m'] + data['cache_write_1h'])}\n"
        f"cache rd  {_fmt_int(data['cache_read'])}\n"
        f"[bold green]spend     {_fmt_usd(data['total_usd'])}[/bold green]",
        title="project", border_style="cyan",
    ))
    t = Table(title="by model", header_style="bold")
    t.add_column("model"); t.add_column("turns", justify="right"); t.add_column("$", justify="right")
    for model, turns, usd in data["models"]:
        t.add_row(model, _fmt_int(turns), _fmt_usd(usd))
    console.print(t)

    t = Table(title="sessions", header_style="bold")
    t.add_column("session"); t.add_column("first"); t.add_column("last")
    t.add_column("turns", justify="right"); t.add_column("$", justify="right")
    for sid, first_ts, last_ts, turns, usd in data["sessions_detail"]:
        t.add_row(_shortid(sid), _fmt_ts(first_ts), _fmt_ts(last_ts),
                  _fmt_int(turns), _fmt_usd(usd))
    console.print(t)


@cli.command()
@click.argument("session_id")
def session(session_id):
    """Turn-by-turn trace of one session."""
    conn = A.connect_with_views()
    rows = A.session_trace(conn, session_id)
    if not rows:
        console.print(f"[red]no session matching[/red] {session_id!r}")
        return
    t = Table(title=f"session {_shortid(session_id)}", header_style="bold")
    t.add_column("ts"); t.add_column("model")
    t.add_column("in", justify="right"); t.add_column("out", justify="right")
    t.add_column("cw", justify="right"); t.add_column("cr", justify="right")
    t.add_column("$", justify="right"); t.add_column("tools", justify="right")
    for ts, model, inp, out, cw, cr, usd, tools in rows:
        t.add_row(_fmt_ts(ts), model, _fmt_int(inp), _fmt_int(out),
                  _fmt_int(cw), _fmt_int(cr), _fmt_usd(usd), _fmt_int(tools))
    console.print(t)


@cli.command()
def tools():
    """Tool-usage rollup."""
    conn = A.connect_with_views()
    rows = A.spend_by(conn, "tool", limit=50)
    t = Table(title="tool rollup", header_style="bold")
    t.add_column("tool"); t.add_column("calls", justify="right")
    t.add_column("turns using", justify="right"); t.add_column("input chars", justify="right")
    for name, calls, turns_using, input_chars in rows:
        t.add_row(name, _fmt_int(calls), _fmt_int(turns_using), _fmt_int(input_chars))
    console.print(t)


@cli.command()
def cache():
    """Cache efficiency per model."""
    conn = A.connect_with_views()
    rows = A.cache_efficiency(conn)
    t = Table(title="cache efficiency", header_style="bold")
    t.add_column("model"); t.add_column("cache read", justify="right")
    t.add_column("cache write", justify="right"); t.add_column("uncached input", justify="right")
    t.add_column("hit %", justify="right")
    for model, cr, cw, unc, pct in rows:
        t.add_row(model, _fmt_int(cr), _fmt_int(cw), _fmt_int(unc),
                  f"{(pct or 0):.1f}%")
    console.print(t)


@cli.command()
@click.option("--port", default=8765)
@click.option("--host", default="127.0.0.1")
def serve(port, host):
    """Start FastAPI + dashboard."""
    import uvicorn
    from .server import app
    console.print(f"[green]→ http://{host}:{port}/[/green]")
    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command()
@click.option("--projects-dir", default=None, help="Override path to ~/.claude/projects")
def watch(projects_dir):
    """Watch for new JSONL writes and ingest incrementally."""
    from .watcher import watch as start_watch
    pdir = Path(projects_dir) if projects_dir else None
    start_watch(pdir)


@cli.command()
@click.argument("target")
@click.argument("output_path")
@click.option("--format", "fmt",
              type=click.Choice(["csv", "parquet", "json"]), default="csv")
def export(target, output_path, fmt):
    """Export a table or view to CSV / Parquet / JSON."""
    conn = A.connect_with_views()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in target if c.isalnum() or c in "_")
    if safe != target:
        console.print(f"[red]invalid table/view name[/red]")
        return
    if fmt == "csv":
        conn.execute(f"COPY (SELECT * FROM {target}) TO ? (FORMAT 'csv', HEADER)", [str(out)])
    elif fmt == "parquet":
        conn.execute(f"COPY (SELECT * FROM {target}) TO ? (FORMAT 'parquet')", [str(out)])
    elif fmt == "json":
        conn.execute(f"COPY (SELECT * FROM {target}) TO ? (FORMAT 'json', ARRAY true)", [str(out)])
    console.print(f"[green]wrote[/green] {out}")


@cli.group()
def roots():
    """Manage extra project roots (other machines' ~/.claude/projects)."""


@roots.command("list")
def roots_list():
    """Show the current ingest roots, marking which exist on disk."""
    cfg = cfg_mod.load()
    t = Table(title="ingest roots", header_style="bold")
    t.add_column("host"); t.add_column("path"); t.add_column("status")
    for r in cfg.all_roots():
        exp = r.expanded()
        ok = "[green]exists[/green]" if exp.exists() else "[red]missing[/red]"
        t.add_row(r.host, str(exp), ok)
    console.print(t)
    extras = len(cfg.extra_roots)
    console.print(f"[dim]{extras} extra root(s) + 1 default[/dim]")


@roots.command("add")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--host", required=True, help="Label for this machine (e.g. 'pi', 'work-mac').")
def roots_add(path, host):
    """Register another machine's ~/.claude/projects path."""
    cfg = cfg_mod.load()
    added = cfg_mod.add_root(cfg, path, host)
    cfg_mod.save(cfg)
    if added:
        console.print(f"[green]added[/green] {host} → {path}")
    else:
        console.print(f"[yellow]already present[/yellow] {host} → {path}")


@roots.command("remove")
@click.argument("path_or_host")
def roots_remove(path_or_host):
    """Drop a root by path or host label."""
    cfg = cfg_mod.load()
    n = cfg_mod.remove_root(cfg, path_or_host)
    cfg_mod.save(cfg)
    console.print(f"[green]removed[/green] {n} root(s)")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what rsync would do; don't transfer.")
@click.option("-v", "--verbose", is_flag=True)
@click.option("--from", "source_dir", default=None,
              type=click.Path(path_type=Path),
              help="Source dir to push (defaults to ~/.claude/projects).")
def push(dry_run, verbose, source_dir):
    """Rsync this machine's transcripts to the Pi."""
    from . import sync as S
    try:
        target = S.load_target()
    except SystemExit as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(
        f"[cyan]→[/cyan] {target.ssh_dest}:{target.remote_root}"
    )
    rc = S.push(target=target, source=source_dir, dry_run=dry_run, verbose=verbose)
    if rc == 0:
        console.print("[green]ok[/green]")
    else:
        console.print(f"[red]rsync exited {rc}[/red]")
        sys.exit(rc)


@cli.group()
def sync():
    """Configure rsync push to the Pi."""


@sync.command("set")
@click.option("--pi-user", required=True)
@click.option("--pi-host", required=True)
@click.option("--pi-path", required=True, help="Absolute home path on the Pi (e.g. /home/ian)")
def sync_set(pi_user, pi_host, pi_path):
    """Write ~/.tokmon/sync.toml so `tokmon push` works without env vars."""
    from . import sync as S
    target = S.SyncTarget(pi_user=pi_user, pi_host=pi_host, pi_path=pi_path)
    S.save_target(target)
    console.print(f"[green]wrote[/green] {S.DEFAULT_SYNC_CONFIG}")
    console.print(f"  target: {target.ssh_dest}:{target.remote_root}")


@sync.command("show")
def sync_show():
    """Print the current sync target."""
    from . import sync as S
    try:
        target = S.load_target()
    except SystemExit as e:
        console.print(f"[yellow]{e}[/yellow]")
        return
    console.print(f"pi_user      {target.pi_user}")
    console.print(f"pi_host      {target.pi_host}")
    console.print(f"pi_path      {target.pi_path}")
    console.print(f"sync_subpath {target.sync_subpath}")
    console.print(f"\n→ {target.ssh_dest}:{target.remote_root}")


@cli.command()
def hosts():
    """Per-host rollup of spend and turns."""
    conn = A.connect_with_views()
    rows = conn.execute(
        """
        SELECT host, COUNT(DISTINCT session_id) AS sessions,
               COUNT(*) AS turns,
               SUM(input_tokens+output_tokens+cache_write_5m+cache_write_1h+cache_read) AS tokens,
               SUM(total_usd) AS usd
        FROM v_turn_cost
        GROUP BY host ORDER BY usd DESC
        """
    ).fetchall()
    t = Table(title="spend by host", header_style="bold")
    t.add_column("host"); t.add_column("sessions", justify="right")
    t.add_column("turns", justify="right"); t.add_column("tokens", justify="right")
    t.add_column("$ spend", justify="right")
    for host, sessions, turns, tokens, usd in rows:
        t.add_row(host, _fmt_int(sessions), _fmt_int(turns), _fmt_int(tokens), _fmt_usd(usd))
    console.print(t)


if __name__ == "__main__":
    cli()
