"""OS-managed writable paths; runtime never depends on the repository layout."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    cache: Path
    jobs: Path
    exports: Path
    logs: Path
    database: Path

    def ensure(self) -> "AppPaths":
        for path in (self.root, self.cache, self.jobs, self.exports, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self


def app_paths(root: str | Path | None = None) -> AppPaths:
    override = root or os.environ.get("MOTIONFORGE_DATA_DIR")
    if override:
        base = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "MotionForge"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "MotionForge"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "motionforge"
    return AppPaths(
        root=base,
        cache=base / "cache",
        jobs=base / "jobs",
        exports=base / "exports",
        logs=base / "logs",
        database=base / "jobs.sqlite3",
    )
