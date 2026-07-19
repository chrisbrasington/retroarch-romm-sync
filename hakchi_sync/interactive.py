"""Interactive rom-centric pull/push tool.

    python -m hakchi_sync.interactive

Pick a rom (deduplicated across every device it's mapped on), then pull it
off a device into RomM or push it from RomM onto a device, one artifact
kind (save/state/both) at a time - a friendlier way to do one-off transfers
than remembering --device/--rom flags for the sync CLI (cli.py) and the
separate push tool (push.py).

Design note for future reuse: this module is ONLY the prompt loop. Every
actual operation it performs is a call into a plain function/class that
has no print()/input() of its own:
  - rom_browser.list_mapped_roms() builds the picker list (pure, no I/O)
  - sync_service.SaveSyncService.sync_kind() does the pull
  - push_service.PushService (via push.confirm_and_push_save/state, which
    only adds the y/N confirm prompt around it) does the push
A future non-terminal frontend (e.g. a web UI) could drive the same three
things directly without reusing anything in this file.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

import paramiko

from . import push
from .config import ConfigError, DeviceConfig, GameEntry, load_config
from .device import build_device_client
from .push_service import PushService
from .rom_browser import MappedRom, list_mapped_roms
from .romm_client import RomMClient
from .ssh_transport import DeviceError
from .sync_service import SaveSyncService, SyncStatus

_UNREACHABLE_EXCEPTIONS = (OSError, paramiko.SSHException, DeviceError)

_SUBTAB_BY_KIND = {"sram": "saves", "state": "states"}


def _romm_asset_url(romm: RomMClient, rom_id: int, kind: str) -> str:
    subtab = _SUBTAB_BY_KIND.get(kind, "saves")
    return f"{romm.base_url}/rom/{rom_id}?tab=save-data&subtab={subtab}"


class _Quit(Exception):
    """Raised to unwind out of the rom-level loop back to main()'s exit."""


def _pick_rom(roms: list[MappedRom], input_func: Callable[[str], str]) -> MappedRom | None:
    print("Mapped roms:")
    for i, rom in enumerate(roms, start=1):
        device_ids = ", ".join(device.id for device, _ in rom.mappings)
        print(f"  {i}) {rom.rom_id:>5}  {rom.display_name}  [{device_ids}]")

    while True:
        answer = input_func("\nPick a rom, or 'q' to quit: ").strip()
        if not answer or answer.lower() == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(roms):
            return roms[int(answer) - 1]
        print("not a valid choice")


def _pick_mapping(
    rom: MappedRom, verb: str, input_func: Callable[[str], str]
) -> tuple[DeviceConfig, GameEntry] | None:
    """Which (device, game) pair to act on, for a rom mapped on more than
    one device. Skips the prompt entirely when there's only one.
    """
    if len(rom.mappings) == 1:
        return rom.mappings[0]

    print(f"  {verb} which device?")
    for i, (device, _) in enumerate(rom.mappings, start=1):
        print(f"    {i}) {device.id}")

    while True:
        answer = input_func("  pick a number, or 'b' to go back: ").strip()
        if not answer or answer.lower() == "b":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(rom.mappings):
            return rom.mappings[int(answer) - 1]
        print("  not a valid choice")


def _pick_kind(verb: str, input_func: Callable[[str], str]) -> str | None:
    answer = input_func(f"  {verb} (s)ave, s(t)ate, (b)oth, or blank to cancel: ").strip().lower()
    if answer in ("s", "save"):
        return "sram"
    if answer in ("t", "state"):
        return "state"
    if answer in ("b", "both"):
        return "both"
    return None


def _do_pull(device: DeviceConfig, game: GameEntry, kind: str, romm: RomMClient) -> None:
    try:
        with build_device_client(device) as device_client:
            service = SaveSyncService(device_client, romm, device)
            for result in service.sync_kind(game, kind):
                detail = f": {result.detail}" if result.detail else ""
                print(f"  [{result.kind}] {result.status.value}{detail}")
                if result.status is SyncStatus.UPLOADED:
                    print(f"    {_romm_asset_url(romm, game.rom_id, result.kind)}")
    except _UNREACHABLE_EXCEPTIONS as exc:
        print(f"  could not reach {device.id}: {exc}")


def _do_push(
    device: DeviceConfig, game: GameEntry, kind: str, romm: RomMClient, input_func: Callable[[str], str]
) -> None:
    try:
        with build_device_client(device) as device_client:
            service = PushService(device_client, romm)
            if kind in ("sram", "both"):
                push.confirm_and_push_save(service, game, input_func)
            if kind in ("state", "both"):
                push.confirm_and_push_state(service, game, input_func)
    except _UNREACHABLE_EXCEPTIONS as exc:
        print(f"  could not reach {device.id}: {exc}")


def _run_for_rom(rom: MappedRom, romm: RomMClient, input_func: Callable[[str], str]) -> None:
    device_ids = ", ".join(device.id for device, _ in rom.mappings)

    while True:
        print(f"\n{rom.display_name} (rom {rom.rom_id})")
        print(f"  mapped on: {device_ids}")
        answer = input_func(
            "  1) pull (device -> RomM)   2) push (RomM -> device)   'b' back   'q' quit: "
        ).strip().lower()

        if answer in ("q", "quit"):
            raise _Quit
        if answer in ("b", "back", ""):
            return
        if answer not in ("1", "2"):
            print("  not a valid choice")
            continue

        mapping = _pick_mapping(rom, "Pull from" if answer == "1" else "Push to", input_func)
        if mapping is None:
            continue
        device, game = mapping

        kind = _pick_kind("Pull" if answer == "1" else "Push", input_func)
        if kind is None:
            continue

        if answer == "1":
            _do_pull(device, game, kind, romm)
        else:
            _do_push(device, game, kind, romm, input_func)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hakchi_sync.interactive",
        description="Interactively pull a save/state from a device into RomM, or push one from RomM to a device.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    roms = list_mapped_roms(config)
    if not roms:
        print("No games mapped yet - run `python -m hakchi_sync --setup` first.")
        return 1

    romm = RomMClient(config.romm_base_url, config.romm_api_token)

    while True:
        print()
        try:
            rom = _pick_rom(roms, input)
            if rom is None:
                print("bye")
                return 0
            _run_for_rom(rom, romm, input)
        except (_Quit, EOFError):
            # 'q' anywhere in the rom-level loop, or stdin closing mid-
            # prompt (piped input running out, a dropped terminal, Ctrl-D)
            # - exit cleanly rather than a raw traceback.
            print("\nbye")
            return 0


if __name__ == "__main__":
    sys.exit(main())
