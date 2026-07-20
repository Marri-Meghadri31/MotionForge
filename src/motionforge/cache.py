"""Version-aware atomic JSON caches with age and disk-budget eviction."""

from __future__ import annotations

import hashlib
import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from motionforge.constants import ENGINE_VERSION, SCHEMA_VERSION, TIMELINE_VERSION


def canonical_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {str(key): canonical_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_data(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def cache_key(namespace: str, value: Any, *, extra_versions: dict[str, Any] | None = None) -> str:
    envelope = {
        "namespace": namespace,
        "engineVersion": ENGINE_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "timelineVersion": TIMELINE_VERSION,
        "versions": extra_versions or {},
        "value": canonical_data(value),
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JsonCache:
    def __init__(self, root: Path, *, max_bytes: int = 512 * 1024 * 1024, max_age_days: int = 30) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_days * 86_400
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        if not key.isalnum() or len(key) != 64:
            raise ValueError("invalid cache key")
        directory = (self.root / namespace).resolve()
        if self.root.resolve() not in directory.parents:
            raise ValueError("invalid cache namespace")
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.json"

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        path = self._path(namespace, key)
        if not path.is_file():
            return None
        try:
            if time.time() - path.stat().st_mtime > self.max_age_seconds:
                path.unlink(missing_ok=True)
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            os.utime(path, None)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, namespace: str, key: str, value: BaseModel | dict[str, Any]) -> Path:
        path = self._path(namespace, key)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        payload = canonical_data(value)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
        self.cleanup()
        return path

    def cleanup(self) -> None:
        files: list[tuple[float, int, Path]] = []
        total = 0
        now = time.time()
        for path in self.root.rglob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > self.max_age_seconds:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            files.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= self.max_bytes:
            return
        for _, size, path in sorted(files):
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
            if total <= self.max_bytes:
                break
