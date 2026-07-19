from __future__ import annotations

import argparse
import sys
from typing import Callable

import paramiko

from .config import AppConfig, ConfigError, DeviceConfig, GameEntry, load_config
from .device import DeviceClient, build_device_client
from .romm_client import AssetSummary, RomMApiError, RomMClient
from .ssh_transport import DeviceError


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync.push",
        description="Push a save and/or state from RomM down to a device for a mapped game.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--device",
        metavar="DEVICE_ID",
        default=None,
        help="device id in config.yaml to push to (required if more than one device is configured)",
    )
    parser.add_argument(
        "--rom",
        metavar="ROM_ID",
        type=int,
        default=None,
        help="push straight to the game mapped to this RomM rom_id, skipping the game picker "
        "(still asks save/state/both and a y/N confirm before touching the device)",
    )
    return parser.parse_args(argv)


def _resolve_device(config: AppConfig, device_id: str | None) -> DeviceConfig:
    if device_id is not None:
        device = config.find_device(device_id)
        if device is None:
            raise ConfigError(f"no device with id {device_id!r} in config")
        return device

    if len(config.devices) == 1:
        return config.devices[0]

    ids = ", ".join(d.id for d in config.devices)
    raise ConfigError(f"multiple devices configured - specify one with --device ({ids})")


def _resolve_game_by_rom(device: DeviceConfig, rom_id: int) -> GameEntry:
    game = device.find_game_by_rom_id(rom_id)
    if game is None:
        raise ConfigError(f"no game mapped to rom_id {rom_id} on device {device.id!r}")
    return game


def _pick_game(device: DeviceConfig, input_func: Callable[[str], str]) -> GameEntry | None:
    # Ignored entries have no rom_id - nothing in RomM to pull a save from.
    games = [g for g in device.games if not g.ignored]

    print("Mapped games:")
    for i, game in enumerate(games, start=1):
        print(f"  {i}) {game.label}  ({game.game_id}, rom {game.rom_id})")

    while True:
        answer = input_func("\nPick a number, or 'q' to quit: ").strip()
        if not answer or answer.lower() == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(games):
            return games[int(answer) - 1]
        print("not a valid choice")


def _pick_kind(input_func: Callable[[str], str]) -> str | None:
    answer = input_func("Push (s)ave, s(t)ate, (b)oth, or blank to cancel: ").strip().lower()
    if answer in ("s", "save"):
        return "save"
    if answer in ("t", "state"):
        return "state"
    if answer in ("b", "both"):
        return "both"
    return None


def _latest(assets: list[AssetSummary]) -> AssetSummary:
    return max(assets, key=lambda a: a.updated_at)


def _push_save(
    device: DeviceClient, romm: RomMClient, game: GameEntry, input_func: Callable[[str], str]
) -> None:
    try:
        saves = romm.list_saves(game.rom_id)
    except RomMApiError as exc:
        print(f"  could not list RomM saves: {exc}")
        return

    if not saves:
        print("  no save in RomM for this rom")
        return

    chosen = _latest(saves)
    print(f"  RomM save: {chosen.file_name} (slot={chosen.slot}, updated {chosen.updated_at})")

    confirm = input_func(
        f"  this OVERWRITES the current battery save on the device for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        data = romm.download_save(chosen.id)
        device.write_save(game.game_id, game.path_hint, data)
    except (RomMApiError, DeviceError) as exc:
        print(f"  push failed: {exc}")
        return

    print(f"  wrote {len(data)} bytes to the device's save file")


def _push_state(
    device: DeviceClient, romm: RomMClient, game: GameEntry, input_func: Callable[[str], str]
) -> None:
    try:
        states = romm.list_states(game.rom_id)
    except RomMApiError as exc:
        print(f"  could not list RomM states: {exc}")
        return

    if not states:
        print("  no state in RomM for this rom")
        return

    chosen = _latest(states)
    print(f"  RomM state: {chosen.file_name} (updated {chosen.updated_at})")

    confirm = input_func(
        f"  this OVERWRITES the current save state on the device for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        data = romm.download_state(chosen.id)
        path = device.write_state(game.game_id, game.path_hint, data)
    except (RomMApiError, DeviceError) as exc:
        print(f"  push failed: {exc}")
        return

    print(f"  wrote {len(data)} bytes to {path}")


def _push(device: DeviceClient, romm: RomMClient, game: GameEntry, input_func: Callable[[str], str]) -> None:
    kind = _pick_kind(input_func)
    if kind is None:
        print("  cancelled")
        return
    if kind in ("save", "both"):
        _push_save(device, romm, game, input_func)
    if kind in ("state", "both"):
        _push_state(device, romm, game, input_func)


def _run(
    device: DeviceClient,
    romm: RomMClient,
    device_config: DeviceConfig,
    input_func: Callable[[str], str],
    only_game: GameEntry | None,
) -> None:
    if only_game is not None:
        print()
        try:
            _push(device, romm, only_game, input_func)
        except EOFError:
            print("\nbye")
        return

    while True:
        print()
        try:
            game = _pick_game(device_config, input_func)
            if game is None:
                print("bye")
                return
            _push(device, romm, game, input_func)
        except EOFError:
            # stdin closed mid-prompt (piped input running out, a dropped
            # terminal, Ctrl-D) - exit the same as picking blank/'q' would,
            # not with a raw traceback.
            print("\nbye")
            return


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = load_config(args.config)
        device_config = _resolve_device(config, args.device)
        only_game = _resolve_game_by_rom(device_config, args.rom) if args.rom is not None else None
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not device_config.games:
        print(
            f"No games mapped for device {device_config.id!r} yet - run "
            f"`python -m hakchi_sync --setup --device {device_config.id}` first."
        )
        return 1

    try:
        with build_device_client(device_config) as device:
            romm = RomMClient(config.romm_base_url, config.romm_api_token)
            _run(device, romm, device_config, input, only_game)
    except (OSError, paramiko.SSHException, DeviceError) as exc:
        print(f"could not reach {device_config.id} at {device_config.host}: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
