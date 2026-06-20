"""Client-side: rsync ~/.claude/projects/ to the Pi.

Reads ~/.tokmon/sync.toml (or env vars) to find the Pi, builds an rsync command
that copies only .jsonl files, and runs it. Used by `tokmon push` and called
from launchd / cron.

Config file shape (~/.tokmon/sync.toml):

    pi_user = "ian"
    pi_host = "raspberrypi"
    pi_path = "/home/ian"               # ~ on the Pi
    sync_subpath = "sync"               # final dest is pi_path/sync/<this-host>/.claude/projects/
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_SYNC_CONFIG = Path.home() / ".tokmon" / "sync.toml"


@dataclass(frozen=True)
class SyncTarget:
    pi_user: str
    pi_host: str
    pi_path: str          # absolute path on the Pi (e.g. /home/ian)
    sync_subpath: str = "sync"

    @property
    def remote_root(self) -> str:
        """Full remote path: <pi_path>/<sync_subpath>/<this-host>/.claude/projects/

        Hostname is lowercased — Linux paths are case-sensitive and tokmon
        ingest treats the directory name as the host label.
        """
        h = socket.gethostname().split(".")[0].lower()
        return f"{self.pi_path.rstrip('/')}/{self.sync_subpath}/{h}/.claude/projects/"

    @property
    def ssh_dest(self) -> str:
        return f"{self.pi_user}@{self.pi_host}"


def load_target(path: Path | None = None) -> SyncTarget:
    """Resolution: env vars override the TOML file."""
    cfg: dict = {}
    p = path or DEFAULT_SYNC_CONFIG
    if p.exists():
        with p.open("rb") as f:
            cfg = tomllib.load(f)
    pi_user = os.environ.get("TOKMON_PI_USER", cfg.get("pi_user"))
    pi_host = os.environ.get("TOKMON_PI_HOST", cfg.get("pi_host"))
    pi_path = os.environ.get("TOKMON_PI_PATH", cfg.get("pi_path"))
    sync_subpath = os.environ.get(
        "TOKMON_SYNC_SUBPATH", cfg.get("sync_subpath", "sync")
    )
    missing = [n for n, v in (("pi_user", pi_user), ("pi_host", pi_host),
                              ("pi_path", pi_path)) if not v]
    if missing:
        raise SystemExit(
            f"tokmon push: missing config {missing}.\n"
            f"Set TOKMON_PI_USER / TOKMON_PI_HOST / TOKMON_PI_PATH, "
            f"or write {p}"
        )
    return SyncTarget(pi_user=pi_user, pi_host=pi_host, pi_path=pi_path,
                      sync_subpath=sync_subpath)


def save_target(target: SyncTarget, path: Path | None = None) -> None:
    p = path or DEFAULT_SYNC_CONFIG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'pi_user = "{target.pi_user}"\n'
        f'pi_host = "{target.pi_host}"\n'
        f'pi_path = "{target.pi_path}"\n'
        f'sync_subpath = "{target.sync_subpath}"\n'
    )


def build_rsync_cmd(
    target: SyncTarget,
    source: Path,
    ssh_options: list[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Construct the rsync invocation. Pure — for tests."""
    cmd = ["rsync", "-a", "--partial",
           "--include=*/",
           "--include=*.jsonl",
           "--exclude=*"]
    if dry_run:
        cmd.append("--dry-run")
    if ssh_options:
        cmd.extend(["-e", "ssh " + " ".join(ssh_options)])
    cmd.append(f"{source.rstrip('/') if isinstance(source, str) else str(source).rstrip('/')}/")
    cmd.append(f"{target.ssh_dest}:{target.remote_root}")
    return cmd


def _ensure_remote_dir(target: SyncTarget, verbose: bool = False) -> int:
    """Pre-create the remote destination directory via SSH.

    Works around macOS's bundled rsync 2.6.9 not supporting --mkpath.
    """
    cmd = ["ssh", target.ssh_dest, f"mkdir -p {target.remote_root}"]
    if verbose:
        print("running:", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd).returncode


def push(
    target: SyncTarget | None = None,
    source: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Execute the push. Returns rsync's exit code."""
    target = target or load_target()
    source = source or (Path.home() / ".claude" / "projects")
    if not source.exists():
        print(f"tokmon push: source {source} does not exist", file=sys.stderr)
        return 1
    if not dry_run:
        rc = _ensure_remote_dir(target, verbose=verbose)
        if rc != 0:
            print(f"tokmon push: mkdir on remote failed (ssh exit {rc})", file=sys.stderr)
            return rc
    cmd = build_rsync_cmd(target, source, dry_run=dry_run)
    if verbose:
        cmd.insert(1, "-v")
        print("running:", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd).returncode
