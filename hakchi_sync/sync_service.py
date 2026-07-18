from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .config import AppConfig, GameEntry
from .hakchi_client import HakchiClient, HakchiError, SaveNotFoundError
from .romm_client import RomMApiError, RomMClient
from .state_codec import StateDecodeError, decode_savestate

logger = logging.getLogger("hakchi_sync")


class SyncStatus(Enum):
    UPLOADED = "uploaded"
    DRY_RUN = "dry-run"
    SKIPPED_NO_DATA = "skipped (nothing on device)"
    FAILED = "failed"


@dataclass(frozen=True)
class SyncResult:
    game: GameEntry
    kind: str  # "sram" or "state"
    status: SyncStatus
    detail: str = ""


class SaveSyncService:
    """Pulls each configured game's cartridge.sram and latest suspend-point
    state off the hakchi and uploads them to RomM. Failures on one game (or
    one artifact) don't stop the rest.
    """

    def __init__(
        self,
        hakchi_client: HakchiClient,
        romm_client: RomMClient,
        config: AppConfig,
        dry_run: bool = False,
    ):
        self._hakchi = hakchi_client
        self._romm = romm_client
        self._config = config
        self._dry_run = dry_run

    def sync_game(self, game: GameEntry) -> list[SyncResult]:
        return [self._sync_sram(game), self._sync_state(game)]

    def _sync_sram(self, game: GameEntry) -> SyncResult:
        tag = f"[{game.hakchi_code}] {game.label} (sram)"

        try:
            data = self._hakchi.read_cartridge_sram(game.hakchi_code)
        except SaveNotFoundError:
            logger.info("%s: no cartridge.sram on device, skipping", tag)
            return SyncResult(game, "sram", SyncStatus.SKIPPED_NO_DATA)
        except HakchiError as exc:
            logger.error("%s: failed to read from device: %s", tag, exc)
            return SyncResult(game, "sram", SyncStatus.FAILED, str(exc))

        file_name = f"{game.hakchi_code}.sram"

        if self._dry_run:
            logger.info(
                "%s: DRY RUN - would upload %s (%d bytes) to rom_id=%d slot=%r",
                tag, file_name, len(data), game.rom_id, self._config.slot,
            )
            return SyncResult(game, "sram", SyncStatus.DRY_RUN, f"{len(data)} bytes")

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
            return SyncResult(game, "sram", SyncStatus.FAILED, str(exc))

        logger.info("%s: uploaded %s (%d bytes) -> asset id %d", tag, result.file_name, len(data), result.asset_id)
        return SyncResult(game, "sram", SyncStatus.UPLOADED, f"asset id {result.asset_id}")

    def _sync_state(self, game: GameEntry) -> SyncResult:
        tag = f"[{game.hakchi_code}] {game.label} (state)"

        try:
            savestate = self._hakchi.read_latest_savestate(game.hakchi_code)
        except HakchiError as exc:
            logger.error("%s: failed to read from device: %s", tag, exc)
            return SyncResult(game, "state", SyncStatus.FAILED, str(exc))

        if savestate is None:
            logger.info("%s: no suspend-point state on device, skipping", tag)
            return SyncResult(game, "state", SyncStatus.SKIPPED_NO_DATA)

        try:
            data = decode_savestate(savestate.data)
        except StateDecodeError as exc:
            logger.error("%s: could not decode state: %s", tag, exc)
            return SyncResult(game, "state", SyncStatus.FAILED, str(exc))

        file_name = f"{game.hakchi_code}.state"

        if self._dry_run:
            logger.info(
                "%s: DRY RUN - would upload %s (%d bytes, screenshot=%s) to rom_id=%d",
                tag, file_name, len(data), bool(savestate.screenshot), game.rom_id,
            )
            return SyncResult(game, "state", SyncStatus.DRY_RUN, f"{len(data)} bytes")

        try:
            result = self._romm.upload_state(
                rom_id=game.rom_id,
                file_name=file_name,
                data=data,
                emulator=game.emulator,
                screenshot=savestate.screenshot,
            )
        except RomMApiError as exc:
            logger.error("%s: upload failed: %s", tag, exc)
            return SyncResult(game, "state", SyncStatus.FAILED, str(exc))

        logger.info("%s: uploaded %s (%d bytes) -> asset id %d", tag, result.file_name, len(data), result.asset_id)
        return SyncResult(game, "state", SyncStatus.UPLOADED, f"asset id {result.asset_id}")

    def run(self, games: list[GameEntry] | None = None) -> list[SyncResult]:
        results = []
        for game in games if games is not None else self._config.games:
            results.extend(self.sync_game(game))
        return results
