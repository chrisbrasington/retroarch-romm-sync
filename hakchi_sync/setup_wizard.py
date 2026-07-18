from __future__ import annotations

import re
from typing import Callable

from ruamel.yaml import YAML

from .device import DeviceClient, InstalledGame
from .romm_client import RomMApiError, RomMClient

_ROM_ID_RE = re.compile(r"/rom/(\d+)")


class _StopRequested(Exception):
    pass


def _ask(input_func: Callable[[str], str], prompt: str) -> str:
    """input_func() raises EOFError if stdin closes mid-prompt (piped input
    running out, a dropped terminal, Ctrl-D) - treat that the same as 'q'
    instead of crashing with a traceback. Anything already added via
    _add_mapping is written immediately, so nothing is lost either way.
    """
    try:
        return input_func(prompt).strip()
    except EOFError:
        raise _StopRequested from None


def parse_rom_id(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    match = _ROM_ID_RE.search(text)
    if match:
        return int(match.group(1))
    return int(text) if text.isdigit() else None


class SetupWizard:
    """Interactively fills in one device's `games:` list in config.yaml.

    For each game the device reports as installed (optionally limited to
    ones with an actual save on the device), it searches RomM by the
    game's own name and shows candidate matches to pick from - or you can
    paste a rom URL/ID directly, which gets looked up and shown for
    confirmation before it's saved. Either way you see RomM's own
    name/platform before anything is written to config.yaml. Works the
    same for every device type - it only talks to the DeviceClient
    interface, never a concrete device class.
    """

    def __init__(self, device: DeviceClient, romm: RomMClient, config_path: str, device_id: str):
        self._device = device
        self._romm = romm
        self._config_path = config_path
        self._device_id = device_id
        self._yaml = YAML()
        self._yaml.preserve_quotes = True

    def run(self, include_all: bool = False, input_func: Callable[[str], str] = input) -> int:
        with open(self._config_path) as f:
            raw = self._yaml.load(f)

        device_node = self._find_device_node(raw)
        existing_ids = {g["game_id"] for g in device_node.get("games", [])}
        installed = self._device.list_installed_games()
        with_saves = self._device.list_ids_with_save()
        with_states = self._device.list_ids_with_state()
        has_data = with_saves | with_states

        candidates = [
            game
            for game in installed
            if game.id not in existing_ids and (include_all or game.id in has_data)
        ]

        if not candidates:
            print("Nothing to add - every eligible game is already mapped for this device.")
            return 0

        print(f"{len(candidates)} game(s) to map.\n")

        added = 0
        for game in candidates:
            try:
                added += self._prompt_one(game, with_saves, with_states, raw, input_func)
            except _StopRequested:
                break

        print(
            f"\nSaved {added} new mapping(s) to {self._config_path}"
            if added
            else "\nNo mappings added."
        )

        return 0

    def _find_device_node(self, raw: dict) -> dict:
        for device_node in raw.get("devices", []):
            if device_node.get("id") == self._device_id:
                return device_node
        raise KeyError(f"no device with id {self._device_id!r} in {self._config_path}")

    def _prompt_one(
        self,
        game: InstalledGame,
        with_saves: set[str],
        with_states: set[str],
        raw: dict,
        input_func: Callable[[str], str],
    ) -> int:
        if game.id in with_saves:
            note = ""
        elif game.id in with_states:
            note = "  [no save file - state only]"
        else:
            note = "  [no save or state on device]"
        print(f"\n{game.name}  ({game.id}){note}")

        search_term = game.name
        while True:
            try:
                matches = self._romm.search_roms(search_term)
            except RomMApiError as exc:
                print(f"  search failed: {exc}")
                matches = []

            if matches:
                print(f"  RomM matches for {search_term!r}:")
                for i, rom in enumerate(matches, start=1):
                    print(f"    {i}) {rom.name} ({rom.platform_display_name}) [rom {rom.id}]")
            else:
                print(f"  no RomM matches for {search_term!r}")

            answer = _ask(
                input_func,
                "  pick a number, paste a rom URL/ID, type a new search term,"
                " blank to skip, 'q' to stop: ",
            )

            if answer.lower() == "q":
                raise _StopRequested
            if not answer:
                print("  skipped")
                return 0

            if answer.isdigit() and 1 <= int(answer) <= len(matches):
                rom = matches[int(answer) - 1]
                self._add_mapping(raw, game.id, rom)
                print(f"  added {game.id} -> rom {rom.id} ({rom.name})")
                return 1

            rom_id = parse_rom_id(answer)
            if rom_id is not None:
                try:
                    rom = self._romm.get_rom_summary(rom_id)
                except RomMApiError as exc:
                    print(f"  could not look up rom {rom_id}: {exc}")
                    continue

                confirm = _ask(
                    input_func,
                    f'  RomM says rom {rom_id} is "{rom.name}" ({rom.platform_display_name})'
                    " - use it? [Y/n] ",
                ).lower()
                if confirm in ("", "y", "yes"):
                    self._add_mapping(raw, game.id, rom)
                    print(f"  added {game.id} -> rom {rom_id}")
                    return 1
                if confirm == "q":
                    raise _StopRequested
                print("  not using that one")
                continue

            # anything else typed is treated as a new search term
            search_term = answer

    def _add_mapping(self, raw: dict, game_id: str, rom) -> None:
        device_node = self._find_device_node(raw)
        entry = {"game_id": game_id, "rom_id": rom.id, "display_name": rom.name}
        path_hint = self._device.resolve_path_hint(game_id)
        if path_hint:
            entry["path_hint"] = path_hint
        device_node.setdefault("games", []).append(entry)
        # Written immediately (not batched to the end) so a `q` mid-session,
        # a crash, or Ctrl-C doesn't lose mappings already confirmed.
        with open(self._config_path, "w") as f:
            self._yaml.dump(raw, f)
