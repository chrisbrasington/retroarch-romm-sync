from __future__ import annotations

import argparse
import sys
from typing import Callable

import paramiko

from .config import AppConfig, ConfigError, GameEntry, load_config
from .hakchi_client import HakchiClient, HakchiError
from .romm_client import AssetSummary, RomMApiError, RomMClient
from .state_codec import encode_savestate


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync.push",
        description="Push a save and/or state from RomM down to the SNES Mini for a mapped game.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    return parser.parse_args(argv)


def _pick_game(config: AppConfig, input_func: Callable[[str], str]) -> GameEntry | None:
    print("Mapped games:")
    for i, game in enumerate(config.games, start=1):
        print(f"  {i}) {game.label}  ({game.hakchi_code}, rom {game.rom_id})")

    while True:
        answer = input_func("\nPick a number, or 'q' to quit: ").strip()
        if not answer or answer.lower() == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(config.games):
            return config.games[int(answer) - 1]
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
        f"  this OVERWRITES the current cartridge.sram on the SNES Mini for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        data = romm.download_save(chosen.id)
        hakchi.write_cartridge_sram(game.hakchi_code, data)
    except (RomMApiError, HakchiError) as exc:
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
        f"  this OVERWRITES the current suspend-point state on the SNES Mini for {game.label}"
        " - continue? [y/N] "
    ).strip().lower()
    if confirm not in ("y", "yes"):
        print("  cancelled")
        return

    try:
        raw = romm.download_state(chosen.id)
        encoded = encode_savestate(raw)
        suspendpoint_dir = hakchi.write_savestate(game.hakchi_code, encoded)
    except (RomMApiError, HakchiError) as exc:
        print(f"  push failed: {exc}")
        return

    print(f"  wrote {len(raw)} bytes ({len(encoded)} bytes compressed) to {suspendpoint_dir}/rollback/savestate")


def _run(hakchi: HakchiClient, romm: RomMClient, config: AppConfig, input_func: Callable[[str], str]) -> None:
    while True:
        print()
        game = _pick_game(config, input_func)
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not config.games:
        print("No games mapped in config.yaml yet - run `python -m hakchi_sync --setup` first.")
        return 1

    try:
        with HakchiClient(
            host=config.hakchi_host,
            user=config.hakchi_user,
            port=config.hakchi_port,
            key_path=config.hakchi_key_path,
        ) as hakchi:
            romm = RomMClient(config.romm_base_url, config.romm_api_token)
            _run(hakchi, romm, config, input)
    except (OSError, paramiko.SSHException) as exc:
        print(f"could not reach hakchi at {config.hakchi_host}: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
