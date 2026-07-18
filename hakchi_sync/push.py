from __future__ import annotations

import argparse
import sys
from typing import Callable

import paramiko

from .config import AppConfig, ConfigError, DeviceConfig, GameEntry, load_config
from .hakchi_client import HakchiClient
from .romm_client import AssetSummary, RomMApiError, RomMClient
from .ssh_transport import DeviceError
from .state_codec import encode_savestate


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync.push",
        description="Push a save and/or state from RomM down to a hakchi2-ce device for a mapped game.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--device",
        default=None,
        help="hakchi device id in config.yaml to push to (default: first device of type 'hakchi')",
    )
    return parser.parse_args(argv)


def _resolve_hakchi_device(config: AppConfig, device_id: str | None) -> DeviceConfig:
    """push.py only supports hakchi2-ce devices (write-back for the new
    stock-RetroArch devices isn't built yet), so it needs one specific
    'hakchi'-type device out of config.yaml's devices list.
    """
    if device_id is not None:
        device = config.find_device(device_id)
        if device is None:
            raise ConfigError(f"no device with id {device_id!r} in config")
        if device.type != "hakchi":
            raise ConfigError(
                f"device {device_id!r} is type {device.type!r}, not 'hakchi' - "
                "push only supports hakchi2-ce devices"
            )
        return device

    device = next((d for d in config.devices if d.type == "hakchi"), None)
    if device is None:
        raise ConfigError("no device of type 'hakchi' in config - push only supports hakchi2-ce devices")
    return device


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
    hakchi: HakchiClient, romm: RomMClient, game: GameEntry, input_func: Callable[[str], str]
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
        f"  this OVERWRITES the current cartridge.sram on the device for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        data = romm.download_save(chosen.id)
        hakchi.write_cartridge_sram(game.game_id, data)
    except (RomMApiError, DeviceError) as exc:
        print(f"  push failed: {exc}")
        return

    print(f"  wrote {len(data)} bytes to cartridge.sram on the device")


def _push_state(
    hakchi: HakchiClient, romm: RomMClient, game: GameEntry, input_func: Callable[[str], str]
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
        f"  this OVERWRITES the current suspend-point state on the device for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        raw = romm.download_state(chosen.id)
        encoded = encode_savestate(raw)
        suspendpoint_dir = hakchi.write_savestate(game.game_id, encoded)
    except (RomMApiError, DeviceError) as exc:
        print(f"  push failed: {exc}")
        return

    print(f"  wrote {len(raw)} bytes ({len(encoded)} bytes compressed) to {suspendpoint_dir}/rollback/savestate")


def _run(
    hakchi: HakchiClient, romm: RomMClient, device: DeviceConfig, input_func: Callable[[str], str]
) -> None:
    while True:
        print()
        try:
            game = _pick_game(device, input_func)
            if game is None:
                print("bye")
                return

            kind = _pick_kind(input_func)
            if kind is None:
                print("  cancelled")
                continue

            if kind in ("save", "both"):
                _push_save(hakchi, romm, game, input_func)
            if kind in ("state", "both"):
                _push_state(hakchi, romm, game, input_func)
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
        device = _resolve_hakchi_device(config, args.device)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not device.games:
        print(
            f"No games mapped for device {device.id!r} yet - run "
            f"`python -m hakchi_sync --setup --device {device.id}` first."
        )
        return 1

    try:
        with HakchiClient(
            device_id=device.id,
            host=device.host,
            user=device.user,
            port=device.port,
            auth=device.auth,
            key_path=device.key_path,
            password=device.password,
        ) as hakchi:
            romm = RomMClient(config.romm_base_url, config.romm_api_token)
            _run(hakchi, romm, device, input)
    except (OSError, paramiko.SSHException) as exc:
        print(f"could not reach {device.id} at {device.host}: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
