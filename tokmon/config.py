"""Persistent config for ingest roots and other per-user knobs.

Lives at ~/.tokmon/config.toml. Hand-editable; manipulated programmatically by
the `tokmon roots` CLI commands.

Each root has a provider: "claude" (a ~/.claude/projects tree) or "codex" (a
~/.codex home, or its sessions/ dir). Codex homes are also found without any
config: for every Claude root shaped like <base>/.claude/projects, a sibling
<base>/.codex is ingested under the same host label. That covers the local
machine (~/.codex) and the Pi's sync/<host>/ layout alike. Set
`codex_autodiscover = false` to turn it off.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path.home() / ".tokmon" / "config.toml"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PROVIDERS = ("claude", "codex")


@dataclass
class IngestRoot:
    """One source of transcripts."""

    path: Path
    host: str
    provider: str = "claude"

    def expanded(self) -> Path:
        return Path(os.path.expanduser(str(self.path))).resolve()


@dataclass
class Config:
    default_projects_dir: Path = field(default_factory=lambda: DEFAULT_PROJECTS_DIR)
    default_host: str = field(default_factory=socket.gethostname)
    extra_roots: list[IngestRoot] = field(default_factory=list)
    codex_autodiscover: bool = True

    def all_roots(self) -> list[IngestRoot]:
        """Default root + every extra root + auto-discovered Codex homes."""
        roots = [
            IngestRoot(path=self.default_projects_dir, host=self.default_host),
            *self.extra_roots,
        ]
        if self.codex_autodiscover:
            explicit = {(r.expanded(), r.host) for r in roots if r.provider == "codex"}
            for r in list(roots):
                if r.provider != "claude":
                    continue
                home = codex_home_for(r.path)
                if home is None:
                    continue
                cand = IngestRoot(path=home, host=r.host, provider="codex")
                if (cand.expanded(), cand.host) not in explicit:
                    explicit.add((cand.expanded(), cand.host))
                    roots.append(cand)
        return roots


def codex_home_for(claude_projects: Path) -> Path | None:
    """<base>/.claude/projects -> <base>/.codex, or None if not that shape."""
    p = Path(os.path.expanduser(str(claude_projects)))
    if p.name != "projects" or p.parent.name != ".claude":
        return None
    return p.parent.parent / ".codex"


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    cfg = Config()
    if "default_projects_dir" in raw:
        cfg.default_projects_dir = Path(raw["default_projects_dir"])
    if "default_host" in raw:
        cfg.default_host = raw["default_host"]
    if "codex_autodiscover" in raw:
        cfg.codex_autodiscover = bool(raw["codex_autodiscover"])
    for r in raw.get("roots", []):
        cfg.extra_roots.append(IngestRoot(
            path=Path(r["path"]), host=r["host"],
            provider=r.get("provider", "claude"),
        ))
    return cfg


def _toml_str(value: object) -> str:
    """Render a value as a TOML basic string, escaping backslashes and quotes.

    Critical on Windows: paths contain backslashes (C:\\Users\\...) that TOML
    would otherwise read as escape sequences (\\U -> invalid). A no-op for
    POSIX paths, which have no backslashes.
    """
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# tokmon config. Manipulate via `tokmon roots add/remove/list`.",
        "",
        f"default_projects_dir = {_toml_str(cfg.default_projects_dir)}",
        f"default_host = {_toml_str(cfg.default_host)}",
        f"codex_autodiscover = {'true' if cfg.codex_autodiscover else 'false'}",
        "",
    ]
    for r in cfg.extra_roots:
        lines.append("[[roots]]")
        lines.append(f"path = {_toml_str(r.path)}")
        lines.append(f"host = {_toml_str(r.host)}")
        if r.provider != "claude":
            lines.append(f"provider = {_toml_str(r.provider)}")
        lines.append("")
    path.write_text("\n".join(lines))


def add_root(cfg: Config, path: Path, host: str, provider: str = "claude") -> bool:
    """Append a root if it isn't already present. Returns True if added."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
    resolved = Path(os.path.expanduser(str(path))).resolve()
    for r in cfg.extra_roots:
        if r.expanded() == resolved and r.host == host and r.provider == provider:
            return False
    cfg.extra_roots.append(IngestRoot(path=resolved, host=host, provider=provider))
    return True


def remove_root(cfg: Config, path_or_host: str) -> int:
    """Drop any extra root whose path or host matches. Returns # removed."""
    resolved = Path(os.path.expanduser(path_or_host)).resolve()
    before = len(cfg.extra_roots)
    cfg.extra_roots = [
        r for r in cfg.extra_roots
        if r.host != path_or_host and r.expanded() != resolved
    ]
    return before - len(cfg.extra_roots)


def iter_roots(roots: Iterable[IngestRoot]) -> Iterable[tuple[Path, str, str]]:
    """Yield (resolved_path, host, provider) for every root that exists on disk."""
    for r in roots:
        p = r.expanded()
        if p.exists():
            yield p, r.host, r.provider
