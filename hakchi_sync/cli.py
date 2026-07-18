from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import paramiko

from .config import AppConfig, ConfigError, DeviceConfig, GameEntry, load_config
from .device import build_device_client
from .hash_cache import HashCache
from .romm_client import RomMApiError, RomMClient
from .setup_wizard import SetupWizard
from .ssh_transport import DeviceError
from .sync_service import SaveSyncService, SyncStatus

logger = logging.getLogger("hakchi_sync")

_UNREACHABLE_EXCEPTIONS = (OSError, paramiko.SSHException, DeviceError)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync",
        description="Sync save files from your handhelds (hakchi2-ce, stock RetroArch over SSH) into RomM.",
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
        "--setup",
        action="store_true",
        help="interactively add game mappings to config.yaml, then exit",
    )
    parser.add_argument(
        "--all-roms",
        action="store_true",
        help="with --setup, also offer games that have no save file on the device yet",
    )
    parser.add_argument(
        "--device",
        metavar="DEVICE_ID",
        help="only process this one device (its 'id' in config.yaml, e.g. snes_mini, rg34xx). "
        "Omit to process every enabled device - unreachable ones (e.g. a powered-off WiFi "
        "handheld) are logged as a warning and skipped, not a hard failure.",
    )
    parser.add_argument(
        "--game",
        metavar="GAME_ID",
        help="only process this one game (requires --device, since game ids aren't unique across devices)",
    )
    parser.add_argument(
        "--hash-cache",
        help=(
            "path to the local file tracking last-uploaded save/state hashes, so "
            "unchanged ones are skipped without re-uploading (default: alongside "
            "--config, named .hakchi_sync_cache.json)"
        ),
    )
    parser.add_argument(
        "--no-hash-cache",
        action="store_true",
        help="disable unchanged-skip detection and always upload",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def _select_devices(config: AppConfig, only_id: str | None) -> list[DeviceConfig]:
    if only_id is None:
        return [d for d in config.devices if d.enabled]

    device = config.find_device(only_id)
    if device is None:
        raise ConfigError(f"no device with id {only_id!r} in config")
    return [device]


def _select_games(device: DeviceConfig, only_game_id: str | None) -> list[GameEntry]:
    if only_game_id is None:
        return device.games

    game = device.find_game(only_game_id)
    if game is None:
        raise ConfigError(f"no game with game_id {only_game_id!r} on device {device.id!r}")
    return [game]


def _verify_mappings(romm: RomMClient, device: DeviceConfig, games: list[GameEntry]) -> bool:
    all_ok = True

    print(f"\n{device.id}:")
    print(f"  {'GAME ID':<24} {'ROM ID':>7}  ROMM NAME (PLATFORM)")
    for game in games:
        try:
            rom = romm.get_rom_summary(game.rom_id)
            print(f"  {game.game_id:<24} {game.rom_id:>7}  {rom.name} ({rom.platform_display_name})")
        except RomMApiError as exc:
            all_ok = False
            print(f"  {game.game_id:<24} {game.rom_id:>7}  ERROR: {exc}")

    return all_ok


def _print_summary(results, skipped_devices: list[str]) -> int:
    counts: dict[tuple[str, str, SyncStatus], int] = {}
    for result in results:
        key = (result.device_id, result.kind, result.status)
        counts[key] = counts.get(key, 0) + 1

    print()
    print("Summary:")
    for key in sorted(counts, key=lambda k: (k[0], k[1], k[2].value)):
        device_id, kind, status = key
        print(f"  [{device_id}] {kind} {status.value}: {counts[key]}")

    if skipped_devices:
        print(f"  (unreachable, skipped: {', '.join(skipped_devices)})")

    any_failed = any(status is SyncStatus.FAILED for _, _, status in counts)
    return 1 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 2

    if args.game and not args.device:
        logger.error("--game requires --device, since game ids aren't unique across devices")
        return 2

    try:
        devices = _select_devices(config, args.device)
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 2

    if not devices:
        logger.error("no enabled devices to process")
        return 2

    romm = RomMClient(config.romm_base_url, config.romm_api_token)

    if args.setup:
        for device_config in devices:
            print(f"\n=== {device_config.id} ===")
            try:
                with build_device_client(device_config) as device:
                    wizard = SetupWizard(device, romm, args.config, device_config.id)
                    wizard.run(include_all=args.all_roms)
            except _UNREACHABLE_EXCEPTIONS as exc:
                logger.warning("device %s unreachable, skipping: %s", device_config.id, exc)
        return 0

    try:
        games_by_device = {d.id: _select_games(d, args.game) for d in devices}
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 2

    if args.verify_only:
        ok = True
        for device_config in devices:
            ok = _verify_mappings(romm, device_config, games_by_device[device_config.id]) and ok
        return 0 if ok else 1

    hash_cache = None
    if not args.no_hash_cache:
        cache_path = args.hash_cache or (Path(args.config).parent / ".hakchi_sync_cache.json")
        hash_cache = HashCache(cache_path)

    results = []
    skipped_devices = []

    for device_config in devices:
        try:
            with build_device_client(device_config) as device:
                service = SaveSyncService(
                    device, romm, device_config, dry_run=args.dry_run, hash_cache=hash_cache
                )
                results.extend(service.run(games_by_device[device_config.id]))
        except _UNREACHABLE_EXCEPTIONS as exc:
            logger.warning("device %s unreachable, skipping: %s", device_config.id, exc)
            skipped_devices.append(device_config.id)

    if hash_cache and not args.dry_run:
        hash_cache.save()

    return _print_summary(results, skipped_devices)


if __name__ == "__main__":
    sys.exit(main())
