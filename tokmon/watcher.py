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


def watch(projects_dir: Path | None = None):
    projects_dir = projects_dir or I.DEFAULT_PROJECTS_DIR
    if not projects_dir.exists():
        print(f"[tokmon.watch] no projects dir at {projects_dir}")
        return
    handler = _DebouncedHandler()
    obs = Observer()
    obs.schedule(handler, str(projects_dir), recursive=True)
    obs.start()
    print(f"[tokmon.watch] watching {projects_dir} (Ctrl-C to stop)")
    handler._run()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
