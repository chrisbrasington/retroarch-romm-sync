from __future__ import annotations

import re
from typing import Callable

from ruamel.yaml import YAML

from .hakchi_client import HakchiClient, InstalledGame
from .romm_client import RomMApiError, RomMClient

_ROM_ID_RE = re.compile(r"/rom/(\d+)")


class _StopRequested(Exception):
    pass


def parse_rom_id(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    match = _ROM_ID_RE.search(text)
    if match:
        return int(match.group(1))
    return int(text) if text.isdigit() else None


class SetupWizard:
    """Interactively fills in config.yaml's `games:` list.

    For each game hakchi2-ce has installed (optionally limited to ones with
    an actual save on the device) it asks for a RomM rom_id or rom URL,
    looks the rom up in RomM so you can confirm the mapping by name before
    it's saved, and appends it to config.yaml.
    """

    def __init__(self, hakchi: HakchiClient, romm: RomMClient, config_path: str):
        self._hakchi = hakchi
        self._romm = romm
        self._config_path = config_path
        self._yaml = YAML()
        self._yaml.preserve_quotes = True

    def run(self, include_all: bool = False, input_func: Callable[[str], str] = input) -> int:
        with open(self._config_path) as f:
            raw = self._yaml.load(f)

        existing_codes = {g["hakchi_code"] for g in raw.get("games", [])}
        installed = self._hakchi.list_installed_games()
        with_saves = self._hakchi.list_codes_with_cartridge_sram()

        candidates = [
            game
            for game in installed
            if game.code not in existing_codes and (include_all or game.code in with_saves)
        ]

        if not candidates:
            print("Nothing to add - every eligible game is already in config.yaml.")
            return 0

        print(f"{len(candidates)} game(s) to map. For each: paste a RomM rom URL or bare rom ID,")
        print("leave blank to skip, or type 'q' to stop.\n")

        added = 0
        for game in candidates:
            try:
                added += self._prompt_one(game, with_saves, raw, input_func)
            except _StopRequested:
                break

        if added:
            with open(self._config_path, "w") as f:
                self._yaml.dump(raw, f)
            print(f"\nSaved {added} new mapping(s) to {self._config_path}")
        else:
            print("\nNo mappings added.")

        return 0

    def _prompt_one(
        self,
        game: InstalledGame,
        with_saves: set[str],
        raw: dict,
        input_func: Callable[[str], str],
    ) -> int:
        save_note = "" if game.code in with_saves else "  [no save file on device]"
        print(f"{game.name}  ({game.code}){save_note}")

        answer = input_func("  RomM URL or rom ID: ").strip()
        if answer.lower() == "q":
            raise _StopRequested
        if not answer:
            print("  skipped\n")
            return 0

        rom_id = parse_rom_id(answer)
        if rom_id is None:
            print(f"  could not parse a rom ID out of {answer!r}, skipped\n")
            return 0

        try:
            rom = self._romm.get_rom_summary(rom_id)
        except RomMApiError as exc:
            print(f"  could not look up rom {rom_id}: {exc}\n")
            return 0

        confirm = input_func(
            f'  RomM says rom {rom_id} is "{rom.name}" ({rom.platform_display_name}) - use it? [Y/n] '
        ).strip().lower()
        if confirm not in ("", "y", "yes"):
            print("  skipped\n")
            return 0

        raw.setdefault("games", []).append(
            {"hakchi_code": game.code, "rom_id": rom_id, "display_name": rom.name}
        )
        print(f"  added {game.code} -> rom {rom_id}\n")
        return 1
