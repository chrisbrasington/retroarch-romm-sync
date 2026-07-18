from __future__ import annotations

from dataclasses import dataclass

import requests


class RomMApiError(Exception):
    pass


@dataclass(frozen=True)
class RomSummary:
    id: int
    name: str
    platform_display_name: str


@dataclass(frozen=True)
class UploadResult:
    asset_id: int
    file_name: str


class RomMClient:
    """Thin wrapper around the bits of the RomM API this tool needs:
    looking up a rom for mapping verification, and uploading saves/states.
    """

    def __init__(self, base_url: str, api_token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_token}"

    def get_rom_summary(self, rom_id: int) -> RomSummary:
        resp = self._session.get(
            f"{self._base_url}/api/roms/{rom_id}/simple", timeout=self._timeout
        )
        self._raise_for_status(resp, f"fetching rom {rom_id}")
        data = resp.json()
        return RomSummary(
            id=data["id"],
            name=data.get("name") or data["fs_name"],
            platform_display_name=data.get("platform_display_name", "?"),
        )

    def upload_save(
        self,
        rom_id: int,
        file_name: str,
        data: bytes,
        *,
        emulator: str | None = None,
        slot: str | None = None,
        autocleanup: bool = False,
        autocleanup_limit: int = 10,
    ) -> UploadResult:
        params: dict[str, str | int] = {
            "rom_id": rom_id,
            "autocleanup": str(autocleanup).lower(),
            "autocleanup_limit": autocleanup_limit,
        }
        if emulator:
            params["emulator"] = emulator
        if slot:
            params["slot"] = slot

        resp = self._session.post(
            f"{self._base_url}/api/saves",
            params=params,
            files={"saveFile": (file_name, data)},
            timeout=self._timeout,
        )
        self._raise_for_status(resp, f"uploading save for rom {rom_id}")
        body = resp.json()
        return UploadResult(asset_id=body["id"], file_name=body["file_name"])

    def upload_state(
        self,
        rom_id: int,
        file_name: str,
        data: bytes,
        *,
        emulator: str | None = None,
    ) -> UploadResult:
        params: dict[str, str | int] = {"rom_id": rom_id}
        if emulator:
            params["emulator"] = emulator

        resp = self._session.post(
            f"{self._base_url}/api/states",
            params=params,
            files={"stateFile": (file_name, data)},
            timeout=self._timeout,
        )
        self._raise_for_status(resp, f"uploading state for rom {rom_id}")
        body = resp.json()
        return UploadResult(asset_id=body["id"], file_name=body["file_name"])

    @staticmethod
    def _raise_for_status(resp: requests.Response, action: str) -> None:
        if resp.status_code >= 400:
            raise RomMApiError(
                f"{action} failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
