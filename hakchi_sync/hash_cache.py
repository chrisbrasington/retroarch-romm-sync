from __future__ import annotations

import hashlib
import json
from pathlib import Path


class HashCache:
    """Tracks the content hash of the last successfully uploaded save/state
    per (device_id, game_id, kind[, slot_label]), so an unchanged one can be
    skipped without re-uploading it. Purely local bookkeeping - RomM exposes
    a content hash
    for saves but not for states, so this covers both the same way instead
    of relying on the API (which would mean downloading the state to hash
    it, exactly what this is meant to avoid).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._hashes: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        with self._path.open() as f:
            return json.load(f)

    def unchanged(
        self, device_id: str, game_id: str, kind: str, data: bytes, slot_label: str | None = None
    ) -> bool:
        return self._hashes.get(self._key(device_id, game_id, kind, slot_label)) == self._hash(data)

    def record(
        self, device_id: str, game_id: str, kind: str, data: bytes, slot_label: str | None = None
    ) -> None:
        self._hashes[self._key(device_id, game_id, kind, slot_label)] = self._hash(data)

    def save(self) -> None:
        with self._path.open("w") as f:
            json.dump(self._hashes, f, indent=2, sort_keys=True)

    @staticmethod
    def _key(device_id: str, game_id: str, kind: str, slot_label: str | None) -> str:
        key = f"{device_id}:{game_id}:{kind}"
        return f"{key}:{slot_label}" if slot_label else key

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()
