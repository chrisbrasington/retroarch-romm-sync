from __future__ import annotations

import hashlib
import json
from pathlib import Path


class HashCache:
    """Tracks the content hash of the last successfully uploaded save/state
    per (hakchi_code, kind), so an unchanged one can be skipped without
    re-uploading it. Purely local bookkeeping - RomM exposes a content hash
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

    def unchanged(self, hakchi_code: str, kind: str, data: bytes) -> bool:
        return self._hashes.get(self._key(hakchi_code, kind)) == self._hash(data)

    def record(self, hakchi_code: str, kind: str, data: bytes) -> None:
        self._hashes[self._key(hakchi_code, kind)] = self._hash(data)

    def save(self) -> None:
        with self._path.open("w") as f:
            json.dump(self._hashes, f, indent=2, sort_keys=True)

    @staticmethod
    def _key(hakchi_code: str, kind: str) -> str:
        return f"{hakchi_code}:{kind}"

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()
