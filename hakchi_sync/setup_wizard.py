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
    an actual save on the device), it searches RomM by the hakchi game's own
    name and shows candidate matches to pick from - or you can paste a rom
    URL/ID directly, which gets looked up and shown for confirmation before
    it's saved. Either way you see RomM's own name/platform before anything
    is written to config.yaml.
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
        with_states = self._hakchi.list_codes_with_savestate()
        has_data = with_saves | with_states

        candidates = [
            game
            for game in installed
            if game.code not in existing_codes and (include_all or game.code in has_data)
        ]

        if not candidates:
            print("Nothing to add - every eligible game is already in config.yaml.")
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

    def _prompt_one(
        self,
        game: InstalledGame,
        with_saves: set[str],
        with_states: set[str],
        raw: dict,
        input_func: Callable[[str], str],
    ) -> int:
        if game.code in with_saves:
            note = ""
        elif game.code in with_states:
            note = "  [no cartridge save - suspend-point state only]"
        else:
            note = "  [no save or state on device]"
        print(f"\n{game.name}  ({game.code}){note}")

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

            answer = input_func(
                "  pick a number, paste a rom URL/ID, type a new search term,"
                " blank to skip, 'q' to stop: "
            ).strip()

            if answer.lower() == "q":
                raise _StopRequested
            if not answer:
                print("  skipped")
                return 0

            if answer.isdigit() and 1 <= int(answer) <= len(matches):
                rom = matches[int(answer) - 1]
                self._add_mapping(raw, game.code, rom)
                print(f"  added {game.code} -> rom {rom.id} ({rom.name})")
                return 1

            rom_id = parse_rom_id(answer)
            if rom_id is not None:
                try:
                    rom = self._romm.get_rom_summary(rom_id)
                except RomMApiError as exc:
                    print(f"  could not look up rom {rom_id}: {exc}")
                    continue

                confirm = input_func(
                    f'  RomM says rom {rom_id} is "{rom.name}" ({rom.platform_display_name})'
                    " - use it? [Y/n] "
                ).strip().lower()
                if confirm in ("", "y", "yes"):
                    self._add_mapping(raw, game.code, rom)
                    print(f"  added {game.code} -> rom {rom_id}")
                    return 1
                if confirm == "q":
                    raise _StopRequested
                print("  not using that one")
                continue

            # anything else typed is treated as a new search term
            search_term = answer

    def _add_mapping(self, raw: dict, hakchi_code: str, rom) -> None:
        raw.setdefault("games", []).append(
            {"hakchi_code": hakchi_code, "rom_id": rom.id, "display_name": rom.name}
        )
        # Written immediately (not batched to the end) so a `q` mid-session,
        # a crash, or Ctrl-C doesn't lose mappings already confirmed.
        with open(self._config_path, "w") as f:
            self._yaml.dump(raw, f)
