"""Filesystem watcher: incremental ingest on JSONL writes."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import ingest as I


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, debounce_s: float = 0.5):
        self.debounce_s = debounce_s
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _kick(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_s, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self):
        try:
            stats = I.incremental()
            if stats.new_turns or stats.new_user_turns:
                print(
                    f"[tokmon.watch] +{stats.new_turns} turns, "
                    f"+{stats.new_user_turns} user, "
                    f"+{stats.new_tool_calls} tools "
                    f"({stats.files_with_new_data}/{stats.files_scanned} files)",
                    flush=True,
                )
        except Exception as e:
            print(f"[tokmon.watch] ingest error: {e}", flush=True)

    def on_any_event(self, event: FileSystemEvent) -> None:
        path = getattr(event, "dest_path", None) or event.src_path
        if path and path.endswith(".jsonl"):
            self._kick()


def _watch_dirs(projects_dir: Path | None) -> list[Path]:
    """Directories to observe. For a Codex home, only sessions/ and
    archived_sessions/: the rest of ~/.codex is SQLite state that churns on
    every keystroke and would keep the debouncer permanently armed."""
    if projects_dir is not None:
        return [projects_dir]
    from . import config as cfg_mod
    dirs: list[Path] = []
    for path, _host, provider in cfg_mod.iter_roots(cfg_mod.load().all_roots()):
        if provider == "codex":
            subs = [path / "sessions", path / "archived_sessions"]
            dirs.extend(d for d in subs if d.is_dir())
            if not any(d.is_dir() for d in subs):
                dirs.append(path)
        else:
            dirs.append(path)
    return dirs


def watch(projects_dir: Path | None = None):
    dirs = [d for d in _watch_dirs(projects_dir) if d.exists()]
    if not dirs:
        print(f"[tokmon.watch] nothing to watch (no projects dir at {projects_dir or I.DEFAULT_PROJECTS_DIR})")
        return
    handler = _DebouncedHandler()
    obs = Observer()
    for d in dirs:
        obs.schedule(handler, str(d), recursive=True)
    obs.start()
    for d in dirs:
        print(f"[tokmon.watch] watching {d}")
    print("[tokmon.watch] Ctrl-C to stop")
    handler._run()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
