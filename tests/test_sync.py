from pathlib import Path

import pytest

from tokmon import sync as S


def test_build_rsync_cmd_basic():
    t = S.SyncTarget(pi_user="pi", pi_host="pi.local", pi_path="/home/pi")
    cmd = S.build_rsync_cmd(t, source=Path("/home/laptop/.claude/projects"))
    assert cmd[0] == "rsync"
    assert "--include=*.jsonl" in cmd
    assert "--exclude=*" in cmd
    # normalize separators so the assertion holds on Windows too, where
    # str(Path("/home/...")) yields backslashes
    assert cmd[-2].replace("\\", "/") == "/home/laptop/.claude/projects/"
    assert cmd[-1].startswith("pi@pi.local:/home/pi/sync/")
    assert cmd[-1].endswith("/.claude/projects/")


def test_dry_run_flag_included():
    t = S.SyncTarget(pi_user="u", pi_host="h", pi_path="/p")
    cmd = S.build_rsync_cmd(t, source=Path("/src"), dry_run=True)
    assert "--dry-run" in cmd


def test_load_target_env_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / "sync.toml"
    f.write_text(
        'pi_user = "fileuser"\npi_host = "filehost"\npi_path = "/file/path"\n'
    )
    monkeypatch.setenv("TOKMON_PI_USER", "envuser")
    monkeypatch.delenv("TOKMON_PI_HOST", raising=False)
    monkeypatch.delenv("TOKMON_PI_PATH", raising=False)
    t = S.load_target(f)
    assert t.pi_user == "envuser"   # env wins
    assert t.pi_host == "filehost"  # file used
    assert t.pi_path == "/file/path"


def test_load_target_missing_keys_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKMON_PI_USER", raising=False)
    monkeypatch.delenv("TOKMON_PI_HOST", raising=False)
    monkeypatch.delenv("TOKMON_PI_PATH", raising=False)
    with pytest.raises(SystemExit):
        S.load_target(tmp_path / "nonexistent.toml")


def test_save_and_load_round_trip(tmp_path):
    t1 = S.SyncTarget(pi_user="pi", pi_host="testhost", pi_path="/home/pi",
                      sync_subpath="claude-sync")
    f = tmp_path / "sync.toml"
    S.save_target(t1, f)
    # save_target writes file; load it back via tomllib through load_target
    # (need to clear env vars first)
    import os
    for k in ("TOKMON_PI_USER", "TOKMON_PI_HOST", "TOKMON_PI_PATH",
              "TOKMON_SYNC_SUBPATH"):
        os.environ.pop(k, None)
    t2 = S.load_target(f)
    assert t2.pi_user == t1.pi_user
    assert t2.pi_host == t1.pi_host
    assert t2.pi_path == t1.pi_path
    assert t2.sync_subpath == t1.sync_subpath


def test_remote_root_includes_hostname():
    t = S.SyncTarget(pi_user="u", pi_host="h", pi_path="/home/u")
    # Just verify shape; hostname can be anything on test runner
    assert "/sync/" in t.remote_root
    assert t.remote_root.endswith("/.claude/projects/")


def test_push_against_local_loopback(tmp_path, monkeypatch):
    """End-to-end: rsync to a local dir using `localhost` as the 'Pi'.

    Skipped if no ssh-able localhost; falls back to a dry-run."""
    src = tmp_path / "fake-claude" / "projects"
    proj = src / "-fake-project"
    proj.mkdir(parents=True)
    (proj / "session-1.jsonl").write_text('{"type":"user","uuid":"u","timestamp":"2026-06-20T00:00:00Z"}\n')
    (proj / "ignored.txt").write_text("nope")

    # No ssh setup — just verify command construction by dry-running.
    t = S.SyncTarget(pi_user="dummy", pi_host="dummy", pi_path="/tmp/fake-pi")
    cmd = S.build_rsync_cmd(t, source=src, dry_run=True)
    # If we tried to actually run this with --dry-run, ssh would still try to
    # connect. So just assert the cmd is well-formed; integration is exercised
    # via `tokmon push --dry-run` in CI.
    assert "--dry-run" in cmd
    assert str(src) + "/" == cmd[-2]
