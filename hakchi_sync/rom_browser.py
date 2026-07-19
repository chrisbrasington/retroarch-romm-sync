from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig, DeviceConfig, GameEntry

__all__ = ["MappedRom", "list_mapped_roms"]


@dataclass(frozen=True)
class MappedRom:
    """One rom_id, deduplicated across every device that maps a game to it.

    Pure view over AppConfig - no device/RomM I/O - so it's safe to call
    from anywhere (a CLI prompt loop today, a future web endpoint later)
    without side effects.
    """

    rom_id: int
    display_name: str
    mappings: list[tuple[DeviceConfig, GameEntry]]


def list_mapped_roms(config: AppConfig) -> list[MappedRom]:
    """Every rom_id mapped to a game on at least one enabled device,
    deduplicated across devices, each with the (device, game) pairs that
    map to it. Ignored entries are excluded - there's nothing to pull/push
    for a game you've marked as not played on that device. Sorted by
    display name for a stable, scannable picker list.
    """
    by_rom_id: dict[int, MappedRom] = {}

    for device in config.devices:
        if not device.enabled:
            continue
        for game in device.games:
            if game.ignored:
                continue
            existing = by_rom_id.get(game.rom_id)
            if existing is None:
                by_rom_id[game.rom_id] = MappedRom(
                    rom_id=game.rom_id, display_name=game.label, mappings=[(device, game)]
                )
            else:
                existing.mappings.append((device, game))

    return sorted(by_rom_id.values(), key=lambda r: r.display_name.lower())
