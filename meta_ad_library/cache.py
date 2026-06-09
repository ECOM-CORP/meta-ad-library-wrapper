"""Disk-backed cache of per-ad EU reach, keyed by ad_archive_id, with a TTL.

Reach is intrinsic to an ad (EU-wide), so caching it avoids re-fetching the same ad
across queries/scans — the single biggest way to cut requests and avoid Meta's rate
limit. Thread-safe (enrichment runs in parallel); writes are debounced + atomic.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class ReachCache:
    def __init__(self, path: str | Path, ttl_seconds: float) -> None:
        self.path = Path(path)
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._dirty = 0
        self._last_save = 0.0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        now = time.time()
        # Drop expired entries on load so the file stays bounded.
        self._data = {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and now - v.get("ts", 0) < self.ttl
        }

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._data.get(key)
            if entry and time.time() - entry["ts"] < self.ttl:
                return entry["reach"]
        return None

    def set(self, key: str, reach: dict) -> None:
        with self._lock:
            self._data[key] = {"reach": reach, "ts": time.time()}
            self._dirty += 1
            due = self._dirty >= 25 or time.time() - self._last_save > 10
        if due:
            self.save()

    def save(self) -> None:
        with self._lock:
            data = dict(self._data)
            self._dirty = 0
            self._last_save = time.time()
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass
