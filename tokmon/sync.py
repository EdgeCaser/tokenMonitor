"""Client-side: rsync ~/.claude/projects/ (and ~/.codex sessions) to the Pi.

Reads ~/.tokmon/sync.toml (or env vars) to find the Pi, builds an rsync command
that copies only .jsonl files, and runs it. Used by `tokmon push` and called
from launchd / cron.

Config file shape (~/.tokmon/sync.toml):

    pi_user = "pi"
    pi_host = "raspberrypi"
    pi_path = "/home/pi"                # ~ on the Pi
    sync_subpath = "sync"               # final dest is pi_path/sync/<this-host>/.claude/projects/

Codex rollouts go to pi_path/sync/<this-host>/.codex/{sessions,archived_sessions}/,
which the Pi's ingest picks up automatically as the sibling of .claude/projects.
"""

from __future__ import annotations

import os
import re
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


def _no_window_kwargs() -> dict:
    """subprocess kwargs that suppress a console window on Windows.

    Without this, each ssh/rsync child launched from a scheduled task flashes
    its own console window on the desktop. CREATE_NO_WINDOW runs the console
    child with no window while still inheriting stdout/stderr, so manual
    `tokmon push` runs in a terminal keep showing output. No-op on POSIX.

    stdin is pinned to DEVNULL alongside it: without a console, there's no
    valid console stdin handle to inherit, and the MSYS2 rsync's nested `-e
    ssh` child corrupts the rsync protocol stream trying to duplicate that
    handle (rsync exit 12) rather than failing cleanly. An explicit DEVNULL
    sidesteps the bad handle.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW,
                 "stdin": subprocess.DEVNULL}
    return {}


@dataclass(frozen=True)
class SyncTarget:
    pi_user: str
    pi_host: str
    pi_path: str          # absolute path on the Pi (e.g. /home/pi)
    sync_subpath: str = "sync"

    @property
    def remote_host_root(self) -> str:
        """<pi_path>/<sync_subpath>/<this-host>/

        Hostname is lowercased — Linux paths are case-sensitive and tokmon
        ingest treats the directory name as the host label.
        """
        h = socket.gethostname().split(".")[0].lower()
        return f"{self.pi_path.rstrip('/')}/{self.sync_subpath}/{h}/"

    @property
    def remote_root(self) -> str:
        """Full remote path: <pi_path>/<sync_subpath>/<this-host>/.claude/projects/"""
        return f"{self.remote_host_root}.claude/projects/"

    def remote_codex(self, subdir: str) -> str:
        """<pi_path>/<sync_subpath>/<this-host>/.codex/<subdir>/"""
        return f"{self.remote_host_root}.codex/{subdir}/"

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
    rsh: str | None = None,
    dest: str | None = None,
) -> list[str]:
    """Construct the rsync invocation. Pure — for tests.

    `dest` overrides the remote path (default: the Claude projects root).
    """
    cmd = ["rsync", "-a", "--partial",
           "--include=*/",
           "--include=*.jsonl",
           "--exclude=*"]
    if dry_run:
        cmd.append("--dry-run")
    if rsh:
        cmd.extend(["-e", rsh])
    elif ssh_options:
        cmd.extend(["-e", "ssh " + " ".join(ssh_options)])
    cmd.append(f"{source.rstrip('/') if isinstance(source, str) else str(source).rstrip('/')}/")
    cmd.append(f"{target.ssh_dest}:{dest or target.remote_root}")
    return cmd


def _default_rsh(profile: Path | None = None) -> str | None:
    """Remote shell rsync should use, or None to let rsync pick `ssh` from PATH.

    On Windows the rsync we ship is the MSYS2 build, and it cannot drive the
    native Windows OpenSSH for its binary protocol (the stream closes with
    0 bytes, rsync error 12). Point it at the MSYS2 ssh instead.

    MSYS2 does not reliably resolve HOME to %USERPROFILE%: with the stock
    `db_home: cygwin` setting its ssh looks in /home/<user>/.ssh (inside the
    MSYS2 install),
    finds no key and gets "Permission denied (publickey)", which surfaces
    as the same rsync error 12. So hand it the Windows profile's ssh config,
    keys and known_hosts explicitly.
    """
    if os.name != "nt":
        return None
    parts = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    ssh_dir = (profile or Path(os.environ.get("USERPROFILE") or Path.home())) / ".ssh"
    if (ssh_dir / "config").is_file():
        parts += ["-F", _to_rsync_source(ssh_dir / "config")]
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        if (ssh_dir / name).is_file():
            parts += ["-i", _to_rsync_source(ssh_dir / name)]
    parts += ["-o", f"UserKnownHostsFile={_to_rsync_source(ssh_dir / 'known_hosts')}"]
    return " ".join(parts)


def _to_rsync_source(source: Path) -> str:
    """Render a local source path for rsync.

    On Windows the rsync we ship is the MSYS2/Cygwin build, which reads a path
    like ``C:\\Users\\me`` as a remote host named ``C``. Convert a Windows path
    to a cygwin path (``C:\\Users\\me`` -> ``/c/Users/me``) so the local source
    is unambiguous. On POSIX the path is returned unchanged.
    """
    if os.name != "nt":
        return str(source)
    s = str(source)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return "/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return s.replace("\\", "/")


def _ensure_remote_dir(target: SyncTarget, verbose: bool = False,
                       extra: list[str] | None = None) -> int:
    """Pre-create the remote destination directory via SSH.

    Works around macOS's bundled rsync 2.6.9 not supporting --mkpath.

    ConnectTimeout/ServerAlive options and a hard subprocess timeout guard
    against a mid-handshake connection reset (e.g. a Tailscale coordination
    outage) leaving this ssh hung indefinitely — a hang here blocks every
    subsequent scheduled push forever, since the parent process keeps the
    redirected sync.log handle open the whole time.
    """
    cmd = ["ssh",
           "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=5",
           "-o", "ServerAliveCountMax=2",
           target.ssh_dest,
           "mkdir -p " + " ".join([target.remote_root, *(extra or [])])]
    if verbose:
        print("running:", " ".join(cmd), file=sys.stderr)
    try:
        return subprocess.run(cmd, timeout=30, **_no_window_kwargs()).returncode
    except subprocess.TimeoutExpired:
        print("tokmon push: ssh mkdir timed out after 30s", file=sys.stderr)
        return 124


CODEX_SUBDIRS = ("sessions", "archived_sessions")


def codex_sources(codex_home: Path | None = None) -> list[tuple[Path, str]]:
    """(local dir, remote subdir) pairs for this machine's Codex rollouts."""
    home = codex_home or Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return [(home / sub, sub) for sub in CODEX_SUBDIRS if (home / sub).is_dir()]


def push(
    target: SyncTarget | None = None,
    source: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    include_codex: bool = True,
) -> int:
    """Execute the push. Returns the first non-zero rsync exit code, else 0.

    Pushes ~/.claude/projects, plus ~/.codex/sessions and
    ~/.codex/archived_sessions when present. An explicit `source` pushes
    only that directory (as the Claude root), as before.
    """
    target = target or load_target()
    explicit_source = source is not None
    source = source or (Path.home() / ".claude" / "projects")
    codex = [] if (explicit_source or not include_codex) else codex_sources()
    if not source.exists() and not codex:
        print(f"tokmon push: source {source} does not exist", file=sys.stderr)
        return 1
    if not dry_run:
        rc = _ensure_remote_dir(target, verbose=verbose,
                                extra=[target.remote_codex(sub) for _, sub in codex])
        if rc != 0:
            print(f"tokmon push: mkdir on remote failed (ssh exit {rc})", file=sys.stderr)
            return rc
    jobs: list[tuple[Path, str | None]] = []
    if source.exists():
        jobs.append((source, None))
    jobs.extend((local, target.remote_codex(sub)) for local, sub in codex)
    first_rc = 0
    for local, dest in jobs:
        cmd = build_rsync_cmd(target, _to_rsync_source(local), dry_run=dry_run,
                              rsh=_default_rsh(), dest=dest)
        if verbose:
            cmd.insert(1, "-v")
            print("running:", " ".join(cmd), file=sys.stderr)
        rc = subprocess.run(cmd, **_no_window_kwargs()).returncode
        if rc != 0:
            print(f"tokmon push: rsync of {local} failed (exit {rc})", file=sys.stderr)
            first_rc = first_rc or rc
    return first_rc
