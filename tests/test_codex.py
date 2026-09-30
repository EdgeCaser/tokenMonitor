"""Codex ingest, pricing, provider lens, versus analytics and the rate-limit
meter. Fixtures are synthetic rollouts built in code, shaped like real Codex
logs (CLI 0.117 legacy token_count-only files and 0.15x files that also carry
token_usage_record)."""

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmon import analytics as A
from tokmon import config as cfg_mod
from tokmon import db, ingest
from tokmon import ingest_codex as IC
from tokmon import pricing as P
from tokmon import sync as S
from tokmon import versus as V

CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.jsonl"
T0 = datetime(2026, 9, 1, 12, 0, 0)


def _ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).isoformat(timespec="milliseconds") + "Z"


def _epoch(sec: float) -> int:
    return int((T0 + timedelta(seconds=sec)).replace(tzinfo=timezone.utc).timestamp())


def _line(sec, rtype, payload):
    return json.dumps({"timestamp": _ts(sec), "type": rtype, "payload": payload})


def _usage(inp, cached, out, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": 0, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def _rate(used_week, used_5h=None, plan="prolite", reset_s=6 * 86400):
    rl = {"limit_id": "codex", "plan_type": plan, "rate_limit_reached_type": None,
          "primary": {"used_percent": used_week, "window_minutes": 10080,
                      "resets_at": _epoch(reset_s)},
          "secondary": None}
    if used_5h is not None:
        rl["primary"], rl["secondary"] = (
            {"used_percent": used_5h, "window_minutes": 300, "resets_at": _epoch(4 * 3600)},
            rl["primary"],
        )
    return rl


def _token_count(sec, total, last, rl=None):
    info = None if total is None else {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": last,
        "model_context_window": 258400,
    }
    return _line(sec, "event_msg", {"type": "token_count", "info": info, "rate_limits": rl})


def legacy_rollout(thread="thread-legacy", cwd="/work/widget-factory"):
    """CLI 0.117 style: usage only via token_count, which repeats totals."""
    u1 = _usage(1000, 600, 100, 40)
    u2 = _usage(3000, 2500, 200, 80)
    return [
        _line(0, "session_meta", {"id": thread, "session_id": thread, "cwd": cwd,
                                   "originator": "codex_cli_rs", "cli_version": "0.117.0",
                                   "source": "cli", "git": {"branch": "main"}}),
        _token_count(1, None, None, _rate(1.0, used_5h=0.0)),
        _line(2, "event_msg", {"type": "task_started", "turn_id": "turn-1"}),
        _line(3, "turn_context", {"turn_id": "turn-1", "model": "gpt-5.4", "cwd": cwd, "effort": "high"}),
        _line(4, "response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan it"}]}),
        _line(5, "response_item", {"type": "function_call", "name": "shell_command",
                                    "arguments": json.dumps({"command": "rg TODO src"})}),
        _token_count(6, 1100, u1, _rate(2.0, used_5h=5.0)),
        _token_count(6.5, 1100, u1, _rate(2.0, used_5h=5.0)),   # duplicate emit
        _line(7, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "done!"}]}),
        _token_count(8, 4300, u2, _rate(4.0, used_5h=12.0)),
    ]


def modern_rollout(thread="thread-modern", cwd="/work/widget-factory"):
    """CLI 0.15x style: token_usage_record per response plus token_count."""
    lines = [
        _line(100, "session_meta", {"id": thread, "session_id": thread, "cwd": cwd,
                                     "originator": "Codex Desktop", "cli_version": "0.155.0",
                                     "source": "vscode"}),
        _line(101, "event_msg", {"type": "task_started", "turn_id": "turn-a"}),
        _line(102, "turn_context", {"turn_id": "turn-a", "model": "gpt-5.6-sol", "cwd": cwd, "effort": "xhigh"}),
        _line(103, "response_item", {"type": "custom_tool_call", "name": "exec",
                                      "input": 'const r = await tools.exec_command({cmd:"git status"});'
                                               ' await tools.apply_patch({patch:"x"});'}),
    ]
    total = 0
    for i, (inp, cached, out) in enumerate([(5000, 4000, 300), (6000, 5500, 500), (7000, 6800, 50)]):
        total += inp + out
        sec = 110 + i * 10
        lines.append(_line(sec, "token_usage_record", {
            "thread_id": thread, "turn_id": "turn-a", "response_id": f"resp-{thread}-{i}",
            "usage": _usage(inp, cached, out, out // 3),
            "thread_token_usage": {"total_tokens": total}}))
        # token_count repeats the same usage; must not double count
        lines.append(_token_count(sec + 1, total, _usage(inp, cached, out), _rate(4.0 + 2 * (i + 1))))
    # Seen in real logs: a lagging token_count that repeats an older running
    # total with a different last-usage. Only token_usage_record is trusted
    # once a file has any, so this must not become a turn.
    lines.append(_token_count(145, 5300, _usage(900, 0, 90)))
    return lines


def guardian_rollout(parent="thread-modern"):
    """Auto-review subagent: bills a compaction before naming its model."""
    thread = "thread-guardian"
    return [
        _line(200, "session_meta", {"id": thread, "session_id": parent, "parent_thread_id": parent,
                                     "cwd": "/work/widget-factory", "source": {"subagent": {"other": "guardian"}},
                                     "thread_source": "guardian_review"}),
        _token_count(201, 900, _usage(800, 0, 100)),
        _line(202, "event_msg", {"type": "task_started", "turn_id": "turn-g"}),
        _line(203, "turn_context", {"turn_id": "turn-g", "model": "codex-auto-review", "cwd": "/work/widget-factory"}),
        _token_count(204, 1500, _usage(500, 400, 100)),
    ]


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "tokmon.duckdb")
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.delenv("TOKMON_PROVIDER", raising=False)
    home = tmp_path / "home"
    claude = home / ".claude" / "projects"
    (claude / "-work-widget-factory").mkdir(parents=True)
    shutil.copy(CLAUDE_FIXTURE, claude / "-work-widget-factory" / "s1.jsonl")
    codex = home / ".codex"
    day = codex / "sessions" / "2026" / "09" / "01"
    _write(day / "rollout-2026-09-01T12-00-00-thread-legacy.jsonl", legacy_rollout())
    _write(day / "rollout-2026-09-01T12-01-40-thread-modern.jsonl", modern_rollout())
    _write(day / "rollout-2026-09-01T12-03-20-thread-guardian.jsonl", guardian_rollout())
    # stray jsonl under ~/.codex that must never be read as usage
    _write(codex / ".tmp" / "plugins" / "fixture.jsonl", [_line(0, "token_usage_record", {})])
    monkeypatch.setattr(cfg_mod, "DEFAULT_PROJECTS_DIR", claude)
    return {"home": home, "claude": claude, "codex": codex, "day": day}


def _roots(env):
    return [(env["claude"], "box", "claude"), (env["codex"], "box", "codex")]


def test_codex_ingest_counts_and_dedupe(env):
    stats = ingest.incremental(roots=_roots(env))
    conn = db.connect()
    rows = conn.execute(
        "SELECT uuid, model, input_tokens, cache_read, output_tokens, reasoning_tokens, "
        "is_sidechain, session_id, reasoning_effort FROM turns WHERE provider = 'codex' ORDER BY ts"
    ).fetchall()
    # legacy: 2 (duplicate token_count dropped); modern: 3 records (token_count
    # ignored); guardian: 2
    assert len(rows) == 7
    by_uuid = {r[0]: r for r in rows}
    legacy1 = by_uuid["codex:thread-legacy:1100"]
    assert legacy1[1] == "gpt-5.4"
    assert legacy1[2] == 400 and legacy1[3] == 600      # input excludes cached
    assert legacy1[4] == 100 and legacy1[5] == 40
    assert legacy1[8] == "high"
    assert by_uuid["codex:resp-thread-modern-0"][1] == "gpt-5.6-sol"
    # guardian's pre-context compaction still gets its model, and it rolls up
    # into the parent session as a sidechain
    g = by_uuid["codex:thread-guardian:900"]
    assert g[1] == "codex-auto-review" and g[6] is True and g[7] == "thread-modern"
    assert stats.new_turns == 7 + 3   # plus the Claude fixture's 3 turns


def test_codex_tool_calls_and_prompts(env):
    ingest.incremental(roots=_roots(env))
    conn = db.connect()
    tools = sorted(r[0] for r in conn.execute(
        "SELECT tool_name FROM tool_calls WHERE turn_uuid LIKE 'codex:%'").fetchall())
    # exec's JS body is credited to the inner tools it calls
    assert tools == ["apply_patch", "exec_command", "shell_command"]
    prompts = dict(conn.execute(
        "SELECT is_prompt, COUNT(*) FROM user_turns WHERE uuid LIKE 'codex-user:%' GROUP BY 1"
    ).fetchall())
    assert prompts == {True: 2, False: 1}   # guardian task isn't a human prompt


def test_stray_tmp_jsonl_is_ignored(env):
    ingest.incremental(roots=_roots(env))
    conn = db.connect()
    files = {r[0] for r in conn.execute("SELECT source_file FROM ingest_log").fetchall()}
    assert not any(".tmp" in f for f in files)


def test_incremental_resume_mid_file_keeps_context(env, tmp_path):
    lines = modern_rollout(thread="thread-split")
    f = env["day"] / "rollout-2026-09-01T13-00-00-thread-split.jsonl"
    _write(f, lines[:6])          # header + first usage record
    ingest.incremental(roots=_roots(env))
    with f.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines[6:]) + "\n")
    stats = ingest.incremental(roots=_roots(env))
    assert stats.new_turns == 2
    conn = db.connect()
    rows = conn.execute(
        "SELECT model, project_label, reasoning_effort FROM turns "
        "WHERE uuid LIKE 'codex:resp-thread-split-%'").fetchall()
    assert len(rows) == 3
    # the resumed half still knows the model/cwd from the header it didn't re-read
    assert set(rows) == {("gpt-5.6-sol", "widget-factory", "xhigh")}


def test_partial_trailing_line_waits(env):
    f = env["day"] / "rollout-2026-09-01T14-00-00-thread-partial.jsonl"
    lines = modern_rollout(thread="thread-partial")
    # header + first usage record complete; the next record half-written
    f.write_text("\n".join(lines[:5]) + "\n" + lines[6][:40], encoding="utf-8")
    ingest.incremental(roots=_roots(env))
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM turns WHERE uuid LIKE 'codex:resp-thread-partial-%'").fetchone()[0] == 1
    conn.close()
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ingest.incremental(roots=_roots(env))
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM turns WHERE uuid LIKE 'codex:resp-thread-partial-%'").fetchone()[0] == 3
    assert conn.execute("SELECT SUM(malformed_lines) FROM ingest_log").fetchone()[0] <= 1


def test_archiving_a_thread_does_not_duplicate(env):
    ingest.incremental(roots=_roots(env))
    before = db.connect().execute("SELECT COUNT(*), COUNT(DISTINCT uuid) FROM turns").fetchone()
    src = env["day"] / "rollout-2026-09-01T12-01-40-thread-modern.jsonl"
    arch = env["codex"] / "archived_sessions" / src.name
    arch.parent.mkdir(parents=True)
    shutil.move(src, arch)
    stats = ingest.incremental(roots=_roots(env))
    assert stats.new_turns == 0
    after = db.connect().execute("SELECT COUNT(*), COUNT(DISTINCT uuid) FROM turns").fetchone()
    assert after == before
    assert db.connect().execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == \
        db.connect().execute("SELECT COUNT(*) FROM (SELECT DISTINCT * FROM tool_calls)").fetchone()[0]


def test_rate_limit_samples_keep_change_points(env):
    ingest.incremental(roots=_roots(env))
    conn = db.connect()
    rows = conn.execute(
        "SELECT window_minutes, used_percent FROM rate_limit_samples "
        "WHERE session_id = 'thread-legacy' ORDER BY ts, window_minutes").fetchall()
    weekly = [u for w, u in rows if w == 10080]
    five = [u for w, u in rows if w == 300]
    assert weekly == [1.0, 2.0, 4.0]       # the repeated 2.0 reading collapsed
    assert five == [0.0, 5.0, 12.0]


def test_provider_lens_partitions_everything(env):
    ingest.incremental(roots=_roots(env))
    total = A.summary(A.connect_with_views(provider="all"))
    claude = A.summary(A.connect_with_views(provider="claude"))
    codex = A.summary(A.connect_with_views(provider="codex"))
    assert claude["turns"] == 3 and codex["turns"] == 7
    assert total["turns"] == claude["turns"] + codex["turns"]
    assert total["total_usd"] == pytest.approx(claude["total_usd"] + codex["total_usd"])
    tools = {r[0] for r in A.spend_by(A.connect_with_views(provider="claude"), "tool")}
    assert "exec_command" not in tools and "Bash" in tools
    with pytest.raises(ValueError):
        A.connect_with_views(provider="gemini")


def test_env_var_sets_default_lens(env, monkeypatch):
    ingest.incremental(roots=_roots(env))
    monkeypatch.setenv("TOKMON_PROVIDER", "codex")
    assert A.summary(A.connect_with_views())["turns"] == 7


def test_quota_inference_ignores_codex(env):
    ingest.incremental(roots=_roots(env))
    q = A.quota_inference(A.connect_with_views(provider="all"))
    assert q["data_range"]["n_turns"] == 3


def test_codex_pricing_and_aliases():
    periods = P.load_rate_periods()
    before = P.rate_for_at("codex-auto-review", date(2026, 7, 1), periods)
    after = P.rate_for_at("codex-auto-review", date(2026, 8, 1), periods)
    assert before == P.rate_for_at("gpt-5.4", date(2026, 7, 1), periods)
    assert after.input == pytest.approx(0.20) and after.output == pytest.approx(1.20)
    sol = P.rate_for("gpt-5.6-sol")
    assert (sol.input, sol.cache_read, sol.output) == (4.00, 0.40, 20.00)
    # unknown Codex models price as the Codex default, not as Sonnet
    assert P.rate_for("gpt-9-imaginary") == P.rate_for("gpt-5.6-sol")
    assert P.provider_for_model("claude-opus-4-8") == "claude"


def test_codex_turn_cost_uses_openai_rates(env):
    ingest.incremental(roots=_roots(env))
    conn = A.connect_with_views(provider="codex")
    usd = conn.execute(
        "SELECT total_usd FROM v_turn_cost WHERE uuid = 'codex:resp-thread-modern-0'").fetchone()[0]
    # gpt-5.6-sol: 1000 uncached in @4, 4000 cached @0.4, 300 out @20 per Mtok
    assert usd == pytest.approx((1000 * 4 + 4000 * 0.4 + 300 * 20) / 1e6)


def test_codex_autodiscovery(tmp_path, monkeypatch):
    base = tmp_path / "sync" / "laptop"
    (base / ".claude" / "projects").mkdir(parents=True)
    (base / ".codex" / "sessions").mkdir(parents=True)
    cfg = cfg_mod.Config(default_projects_dir=tmp_path / "nowhere" / ".claude" / "projects",
                         default_host="pi")
    cfg_mod.add_root(cfg, base / ".claude" / "projects", "laptop")
    roots = [(r.host, r.provider) for r in cfg.all_roots()]
    assert ("laptop", "codex") in roots
    # an explicit codex root isn't duplicated by discovery
    cfg_mod.add_root(cfg, base / ".codex", "laptop", provider="codex")
    assert sum(1 for r in cfg.all_roots() if r.provider == "codex" and r.host == "laptop") == 1
    cfg.codex_autodiscover = False
    cfg.extra_roots = [r for r in cfg.extra_roots if r.provider == "claude"]
    assert all(r.provider == "claude" for r in cfg.all_roots())


def test_config_roundtrip_keeps_provider(tmp_path):
    cfg = cfg_mod.Config()
    cfg_mod.add_root(cfg, tmp_path / "x" / ".codex", "box", provider="codex")
    cfg.codex_autodiscover = False
    path = tmp_path / "config.toml"
    cfg_mod.save(cfg, path)
    loaded = cfg_mod.load(path)
    assert loaded.extra_roots[0].provider == "codex"
    assert loaded.codex_autodiscover is False


def test_versus_scorecard(env):
    ingest.incremental(roots=_roots(env))
    v = V.versus(A.connect_with_views(provider="claude"))   # lens must not narrow it
    assert v["stats"]["claude"]["turns"] == 3 and v["stats"]["codex"]["turns"] == 7
    assert v["stats"]["codex"]["prompts"] == 2
    assert v["exclusive_projects"] == {"claude": 1, "codex": 1}
    cats = {r["category"] for r in v["tool_mix"]}
    assert {"search", "git", "edit"} <= cats
    keys = {r["key"] for r in v["scorecard"]}
    assert {"usd_per_prompt", "cache_hit_pct"} <= keys


@pytest.mark.parametrize("name,preview,expected", [
    ("Bash", '{"command": "git log --oneline"}', "git"),
    ("exec_command", '{"cmd":"rg -n foo"}', "search"),
    ("exec_command", 'const r = await tools.exec_command({cmd:"sed -n 1,20p a.py"})', "read"),
    ("shell_command", '{"command":"bash -lc \\"pytest -q\\""}', "test"),
    ("Read", "{}", "read"),
    ("apply_patch", "", "edit"),
    ("mcp__github__search", "", "mcp"),
    ("spawn_agent", "", "delegate"),
])
def test_tool_taxonomy(name, preview, expected):
    assert V.tool_category(name, preview) == expected


def test_handoffs_detect_switches():
    t = datetime(2026, 9, 1, 9)
    sessions = [
        ("claude", "c1", "proj", t, t + timedelta(minutes=30), 5.0),
        ("codex", "x1", "proj", t + timedelta(minutes=45), t + timedelta(minutes=50), 1.0),
        ("claude", "c2", "proj", t + timedelta(hours=10), t + timedelta(hours=11), 1.0),
        ("codex", "x2", "other", t, t + timedelta(minutes=5), 1.0),
    ]
    h = V._handoffs(sessions, gap_minutes=120)
    assert h["claude_to_codex"] == 1
    assert h["codex_to_claude"] == 0   # 10h later is outside the gap


def test_codex_limits_implied_capacity(env):
    ingest.incremental(roots=_roots(env))
    conn = A.connect_with_views()
    L = V.codex_limits(conn, now=T0 + timedelta(minutes=10))
    weekly = [w for w in L["windows"] if w["window"] == "weekly"]
    assert weekly and all(w["implied_full_usd"] and w["implied_full_usd"] > 0 for w in weekly)
    # meter can't go backwards: envelope max is the highest reading
    assert max(w["max_used_pct"] for w in weekly) == pytest.approx(10.0)
    assert any(c["window"] == "weekly" for c in L["current"])


def test_claude_prompt_classification():
    I = ingest
    assert I._is_human_prompt({"message": {"content": "fix the bug"}})
    assert not I._is_human_prompt({"message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    assert not I._is_human_prompt({"isMeta": True, "message": {"content": "skill body"}})
    assert not I._is_human_prompt({"message": {"content": "<command-name>/model</command-name>"}})
    assert not I._is_human_prompt({"message": {"content": [
        {"type": "text", "text": "[Request interrupted by user for tool use]"}]}})
    assert I._is_human_prompt({"message": {"content": [
        {"type": "image", "source": {}}, {"type": "text", "text": "see this?"}]}})


def test_push_includes_codex_dirs(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    (home / "sessions").mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    srcs = S.codex_sources(home)
    assert [sub for _, sub in srcs] == ["sessions", "archived_sessions"]
    t = S.SyncTarget(pi_user="pi", pi_host="pi", pi_path="/home/pi")
    dest = t.remote_codex("sessions")
    assert dest.endswith("/.codex/sessions/") and "/sync/" in dest
    cmd = S.build_rsync_cmd(t, source=str(home / "sessions"), dest=dest)
    assert cmd[-1] == f"pi@pi:{dest}"
    assert "--include=*.jsonl" in cmd


def test_server_provider_param(env):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from tokmon.server import app
    ingest.incremental(roots=_roots(env))
    client = TestClient(app)
    provs = {r["provider"]: r["turns"] for r in client.get("/api/providers").json()}
    assert provs == {"claude": 3, "codex": 7}
    assert client.get("/api/summary?provider=codex").json()["turns"] == 7
    assert client.get("/api/summary?provider=nope").status_code == 400
    v = client.get("/api/versus?provider=codex").json()
    assert v["stats"]["claude"] is not None
    assert client.get("/api/codex_limits").status_code == 200


def test_cache_matches_live_views(env):
    ingest.incremental(roots=_roots(env))
    cached = A.connect_with_views(provider="all")
    assert cached.execute("SELECT fresh FROM _cache_state").fetchone()[0] is True
    live = A.connect_with_views(provider="all")
    A.apply_lens(live, "all", use_cache=False)
    assert live.execute("SELECT fresh FROM _cache_state").fetchone()[0] is False
    assert A.summary(cached) == pytest.approx(A.summary(live))
    # Parallel aggregation can sum floats in a different order, so compare
    # money to 9 decimals rather than bit-for-bit.
    def norm(rows):
        return sorted(tuple(round(v, 9) if isinstance(v, float) else v for v in r) for r in rows)
    for dim in ("project", "model", "tool", "provider", "session"):
        assert norm(A.spend_by(cached, dim)) == norm(A.spend_by(live, dim))
    vc, vl = V.versus(cached), V.versus(live)
    assert vc["tool_mix"] == vl["tool_mix"]
    rnd = lambda d: {k: round(v, 9) if isinstance(v, float) else v for k, v in d.items()}
    assert {p: rnd(x) for p, x in vc["stats"].items()} == {p: rnd(x) for p, x in vl["stats"].items()}


def test_cache_goes_stale_on_new_turns_and_price_changes(env, tmp_path, monkeypatch):
    ingest.incremental(roots=_roots(env))
    conn = A.connect_with_views()
    assert A.cache_is_fresh(conn)
    # new data that the cache hasn't seen yet
    f = env["day"] / "rollout-2026-09-01T15-00-00-thread-new.jsonl"
    _write(f, modern_rollout(thread="thread-new"))
    ingest.incremental(roots=_roots(env))           # ingest rebuilds it
    assert A.cache_is_fresh(A.connect_with_views())
    conn = db.connect()
    conn.execute("DELETE FROM turns WHERE uuid = 'codex:resp-thread-new-0'")
    conn.close()
    assert not A.cache_is_fresh(A.connect_with_views())
    # an edited price table also invalidates it
    ingest.incremental(roots=_roots(env))
    A.refresh_cache(db.connect(), force=True)
    assert A.cache_is_fresh(A.connect_with_views())
    toml = tmp_path / "pricing.toml"
    toml.write_text('[models."gpt-5.6-sol"]\ninput = 99.0\noutput = 99.0\n')
    monkeypatch.setattr(P, "DEFAULT_PRICING_PATH", toml)
    assert not A.cache_is_fresh(A.connect_with_views())
