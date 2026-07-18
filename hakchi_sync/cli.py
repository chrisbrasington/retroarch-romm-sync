from __future__ import annotations

import argparse
import logging
import sys

import paramiko

from .config import AppConfig, ConfigError, load_config
from .hakchi_client import HakchiClient
from .romm_client import RomMApiError, RomMClient
from .sync_service import SaveSyncService, SyncStatus

logger = logging.getLogger("hakchi_sync")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync",
        description="Sync SNES Mini (hakchi2-ce) save files into RomM.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="log what would be uploaded without uploading"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check that each configured rom_id resolves to the expected game in RomM, then exit",
    )
    parser.add_argument(
        "--game",
        metavar="HAKCHI_CODE",
        help="only process this one game (e.g. CLV-U-NRHVN), for testing a single mapping",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def _select_games(config: AppConfig, only_code: str | None):
    if only_code is None:
        return config.games

    game = config.find_game(only_code)
    if game is None:
        raise ConfigError(f"no game with hakchi_code {only_code!r} in config")
    return [game]


def _verify_mappings(config: AppConfig, games) -> bool:
    romm = RomMClient(config.romm_base_url, config.romm_api_token)
    all_ok = True

    print(f"{'HAKCHI CODE':<16} {'ROM ID':>7}  ROMM NAME (PLATFORM)")
    for game in games:
        try:
            rom = romm.get_rom_summary(game.rom_id)
            print(f"{game.hakchi_code:<16} {game.rom_id:>7}  {rom.name} ({rom.platform_display_name})")
        except RomMApiError as exc:
            all_ok = False
            print(f"{game.hakchi_code:<16} {game.rom_id:>7}  ERROR: {exc}")

    return all_ok


def _print_summary(results) -> int:
    counts = {status: 0 for status in SyncStatus}
    for result in results:
        counts[result.status] += 1

    print()
    print("Summary:")
    for status in SyncStatus:
        if counts[status]:
            print(f"  {status.value}: {counts[status]}")

    return 1 if counts[SyncStatus.FAILED] else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = load_config(args.config)
        games = _select_games(config, args.game)
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 2

    if args.verify_only:
        ok = _verify_mappings(config, games)
        return 0 if ok else 1

    try:
        with HakchiClient(
            host=config.hakchi_host,
            user=config.hakchi_user,
            port=config.hakchi_port,
            key_path=config.hakchi_key_path,
        ) as hakchi:
            romm = RomMClient(config.romm_base_url, config.romm_api_token)
            service = SaveSyncService(hakchi, romm, config, dry_run=args.dry_run)
            results = service.run(games)
    except (OSError, paramiko.SSHException) as exc:
        logger.error("could not reach hakchi at %s: %s", config.hakchi_host, exc)
        return 3

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
