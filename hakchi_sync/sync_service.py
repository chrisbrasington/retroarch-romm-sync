from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .config import AppConfig, GameEntry
from .hakchi_client import HakchiClient, HakchiError, SaveNotFoundError
from .romm_client import RomMApiError, RomMClient

logger = logging.getLogger("hakchi_sync")


class SyncStatus(Enum):
    UPLOADED = "uploaded"
    DRY_RUN = "dry-run"
    SKIPPED_NO_SAVE = "skipped (no save on device)"
    FAILED = "failed"


@dataclass(frozen=True)
class SyncResult:
    game: GameEntry
    status: SyncStatus
    detail: str = ""


class SaveSyncService:
    """Pulls each configured game's cartridge.sram off the hakchi and
    uploads it to RomM. Failures on one game don't stop the others.
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

    def sync_game(self, game: GameEntry) -> SyncResult:
        try:
            data = self._hakchi.read_cartridge_sram(game.hakchi_code)
        except SaveNotFoundError:
            logger.info("[%s] %s: no cartridge.sram on device, skipping", game.hakchi_code, game.label)
            return SyncResult(game, SyncStatus.SKIPPED_NO_SAVE)
        except HakchiError as exc:
            logger.error("[%s] %s: failed to read save from device: %s", game.hakchi_code, game.label, exc)
            return SyncResult(game, SyncStatus.FAILED, str(exc))

        file_name = f"{game.hakchi_code}.sram"

        if self._dry_run:
            logger.info(
                "[%s] %s: DRY RUN - would upload %s (%d bytes) to rom_id=%d slot=%r",
                game.hakchi_code, game.label, file_name, len(data), game.rom_id, self._config.slot,
            )
            return SyncResult(game, SyncStatus.DRY_RUN, f"{len(data)} bytes")

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
            logger.error("[%s] %s: upload failed: %s", game.hakchi_code, game.label, exc)
            return SyncResult(game, SyncStatus.FAILED, str(exc))

        logger.info(
            "[%s] %s: uploaded %s (%d bytes) -> save id %d",
            game.hakchi_code, game.label, result.file_name, len(data), result.save_id,
        )
        return SyncResult(game, SyncStatus.UPLOADED, f"save id {result.save_id}")

    def run(self, games: list[GameEntry] | None = None) -> list[SyncResult]:
        return [self.sync_game(game) for game in (games if games is not None else self._config.games)]
