from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import GameEntry
from .device import DeviceClient
from .romm_client import AssetSummary, RomMApiError, RomMClient
from .ssh_transport import DeviceError


class PushStatus(Enum):
    PUSHED = "pushed"
    FAILED = "failed"


@dataclass(frozen=True)
class PushResult:
    device_id: str
    game: GameEntry
    kind: str  # "sram" or "state"
    status: PushStatus
    detail: str = ""


class PushService:
    """Downloads a save/state from RomM and writes it to a device (the
    opposite direction from SaveSyncService). Finding the candidate asset
    (latest_save/latest_state) is split from actually pushing it
    (push_save/push_state) so a caller can show/confirm the candidate
    first - push.py's CLI confirms via a y/N prompt; a future non-CLI
    caller (e.g. a web UI) would confirm through its own UI instead.
    Nothing in this class prints or reads input, so it's safe to call from
    anywhere.
    """

    def __init__(self, device_client: DeviceClient, romm_client: RomMClient):
        self._device = device_client
        self._romm = romm_client

    def latest_save(self, game: GameEntry) -> AssetSummary | None:
        saves = self._romm.list_saves(game.rom_id)
        return max(saves, key=lambda a: a.updated_at) if saves else None

    def latest_state(self, game: GameEntry) -> AssetSummary | None:
        states = self._romm.list_states(game.rom_id)
        return max(states, key=lambda a: a.updated_at) if states else None

    def push_save(self, game: GameEntry, asset: AssetSummary) -> PushResult:
        try:
            data = self._romm.download_save(asset.id)
            self._device.write_save(game.game_id, game.path_hint, data)
        except (RomMApiError, DeviceError) as exc:
            return PushResult(self._device.id, game, "sram", PushStatus.FAILED, str(exc))
        return PushResult(
            self._device.id, game, "sram", PushStatus.PUSHED, f"{len(data)} bytes to the device's save file"
        )

    def push_state(self, game: GameEntry, asset: AssetSummary) -> PushResult:
        try:
            data = self._romm.download_state(asset.id)
            path = self._device.write_state(game.game_id, game.path_hint, data)
        except (RomMApiError, DeviceError) as exc:
            return PushResult(self._device.id, game, "state", PushStatus.FAILED, str(exc))
        return PushResult(self._device.id, game, "state", PushStatus.PUSHED, f"{len(data)} bytes to {path}")
