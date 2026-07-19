from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .config import DeviceConfig, GameEntry
from .device import DeviceClient, SaveState
from .hash_cache import HashCache
from .romm_client import RomMApiError, RomMClient
from .ssh_transport import DeviceError, SaveNotFoundError
from .state_codec import StateDecodeError, decode_savestate

logger = logging.getLogger("hakchi_sync")


class SyncStatus(Enum):
    UPLOADED = "uploaded"
    UNCHANGED = "unchanged, skipped"
    DRY_RUN = "dry-run"
    SKIPPED_NO_DATA = "skipped (nothing on device)"
    IGNORED = "ignored"
    FAILED = "failed"


@dataclass(frozen=True)
class SyncResult:
    device_id: str
    game: GameEntry
    kind: str  # "sram", "state", or "game" (a whole-game skip, e.g. ignored)
    status: SyncStatus
    detail: str = ""


class SaveSyncService:
    """Pulls one device's configured games' saves and states off it and
    uploads them to RomM. Failures on one game (or one artifact) don't stop
    the rest. One instance per device - cli.py builds a fresh one per
    configured device.
    """

    def __init__(
        self,
        device_client: DeviceClient,
        romm_client: RomMClient,
        device_config: DeviceConfig,
        dry_run: bool = False,
        hash_cache: HashCache | None = None,
        force: bool = False,
    ):
        self._device = device_client
        self._romm = romm_client
        self._config = device_config
        self._dry_run = dry_run
        self._hash_cache = hash_cache
        self._force = force

    def sync_game(self, game: GameEntry) -> list[SyncResult]:
        return self.sync_kind(game, "both")

    def sync_kind(self, game: GameEntry, kind: str) -> list[SyncResult]:
        """Pull just one artifact kind for a game ("sram", "state", or
        "both"). Lets a caller that only wants one thing (e.g.
        hakchi_sync.interactive's rom-centric "pull save only" action) get
        it without a whole-game sync_game() call doing more than asked.
        """
        if game.ignored:
            device_id = self._device.id
            logger.info(
                "[%s/%s] %s: ignored in config, skipping", device_id, game.game_id, game.label
            )
            return [SyncResult(device_id, game, "game", SyncStatus.IGNORED)]

        results: list[SyncResult] = []
        if kind in ("sram", "both"):
            results.append(self._sync_sram(game))
        if kind in ("state", "both"):
            results.extend(self._sync_states(game))
        return results

    def _sync_sram(self, game: GameEntry) -> SyncResult:
        device_id = self._device.id
        tag = f"[{device_id}/{game.game_id}] {game.label} (sram)"

        try:
            data = self._device.read_save(game.game_id, game.path_hint)
        except SaveNotFoundError:
            logger.info("%s: no save file on device, skipping", tag)
            return SyncResult(device_id, game, "sram", SyncStatus.SKIPPED_NO_DATA)
        except DeviceError as exc:
            logger.error("%s: failed to read from device: %s", tag, exc)
            return SyncResult(device_id, game, "sram", SyncStatus.FAILED, str(exc))

        if (
            not self._force
            and self._hash_cache
            and self._hash_cache.unchanged(device_id, game.game_id, "sram", data)
        ):
            logger.info("%s: unchanged since last sync, skipping", tag)
            return SyncResult(device_id, game, "sram", SyncStatus.UNCHANGED)

        file_name = f"{game.game_id}.{self._config.save_extension}"

        if self._dry_run:
            logger.info(
                "%s: DRY RUN - would upload %s (%d bytes) to rom_id=%d slot=%r",
                tag, file_name, len(data), game.rom_id, self._config.slot,
            )
            return SyncResult(device_id, game, "sram", SyncStatus.DRY_RUN, f"{len(data)} bytes")

        try:
            result = self._romm.upload_save(
                rom_id=game.rom_id,
                file_name=file_name,
                data=data,
                emulator=game.emulator,
                slot=self._config.slot,
                autocleanup=True,
                autocleanup_limit=self._config.autocleanup_limit,
            )
        except RomMApiError as exc:
            logger.error("%s: upload failed: %s", tag, exc)
            return SyncResult(device_id, game, "sram", SyncStatus.FAILED, str(exc))

        if self._hash_cache:
            self._hash_cache.record(device_id, game.game_id, "sram", data)

        logger.info(
            "%s: uploaded %s (%d bytes) -> asset id %d", tag, result.file_name, len(data), result.asset_id
        )
        return SyncResult(
            device_id, game, "sram", SyncStatus.UPLOADED, f"{result.file_name} (asset id {result.asset_id})"
        )

    def _sync_states(self, game: GameEntry) -> list[SyncResult]:
        device_id = self._device.id
        tag = f"[{device_id}/{game.game_id}] {game.label} (state)"

        try:
            states = self._device.read_states(
                game.game_id, game.path_hint, self._config.state_upload_policy
            )
        except DeviceError as exc:
            logger.error("%s: failed to read from device: %s", tag, exc)
            return [SyncResult(device_id, game, "state", SyncStatus.FAILED, str(exc))]

        if not states:
            logger.info("%s: no state on device, skipping", tag)
            return [SyncResult(device_id, game, "state", SyncStatus.SKIPPED_NO_DATA)]

        return [self._sync_one_state(game, state, tag) for state in states]

    def _sync_one_state(self, game: GameEntry, state: SaveState, tag: str) -> SyncResult:
        device_id = self._device.id

        try:
            data = decode_savestate(state.data)
        except StateDecodeError as exc:
            logger.error("%s [%s]: could not decode state: %s", tag, state.slot_label, exc)
            return SyncResult(device_id, game, "state", SyncStatus.FAILED, str(exc))

        if not self._force and self._hash_cache and self._hash_cache.unchanged(
            device_id, game.game_id, "state", data, state.slot_label
        ):
            logger.info("%s [%s]: unchanged since last sync, skipping", tag, state.slot_label)
            return SyncResult(device_id, game, "state", SyncStatus.UNCHANGED)

        file_name = f"{game.game_id}.{state.slot_label}.state"

        if self._dry_run:
            logger.info(
                "%s [%s]: DRY RUN - would upload %s (%d bytes, screenshot=%s) to rom_id=%d",
                tag, state.slot_label, file_name, len(data), bool(state.screenshot), game.rom_id,
            )
            return SyncResult(device_id, game, "state", SyncStatus.DRY_RUN, f"{len(data)} bytes")

        try:
            result = self._romm.upload_state(
                rom_id=game.rom_id,
                file_name=file_name,
                data=data,
                emulator=game.emulator,
                screenshot=state.screenshot,
            )
        except RomMApiError as exc:
            logger.error("%s [%s]: upload failed: %s", tag, state.slot_label, exc)
            return SyncResult(device_id, game, "state", SyncStatus.FAILED, str(exc))

        if self._hash_cache:
            self._hash_cache.record(device_id, game.game_id, "state", data, state.slot_label)

        logger.info(
            "%s [%s]: uploaded %s (%d bytes) -> asset id %d",
            tag, state.slot_label, result.file_name, len(data), result.asset_id,
        )
        return SyncResult(
            device_id, game, "state", SyncStatus.UPLOADED, f"{result.file_name} (asset id {result.asset_id})"
        )

    def run(self, games: list[GameEntry] | None = None) -> list[SyncResult]:
        results = []
        for game in games if games is not None else self._config.games:
            results.extend(self.sync_game(game))
        return results
