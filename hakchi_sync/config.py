from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GameEntry:
    hakchi_code: str
    rom_id: int
    display_name: str | None = None
    emulator: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.hakchi_code


@dataclass(frozen=True)
class AppConfig:
    romm_base_url: str
    romm_api_token: str
    hakchi_host: str
    hakchi_user: str
    hakchi_port: int
    hakchi_key_path: str | None
    slot: str
    autocleanup_limit: int
    games: list[GameEntry] = field(default_factory=list)

    def find_game(self, hakchi_code: str) -> GameEntry | None:
        return next((g for g in self.games if g.hakchi_code == hakchi_code), None)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    load_dotenv()

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    try:
        romm = raw["romm"]
        hakchi = raw["hakchi"]
    except KeyError as exc:
        raise ConfigError(f"missing required top-level config section: {exc}") from exc

    if not romm.get("base_url"):
        raise ConfigError("romm.base_url is required")
    if not hakchi.get("host"):
        raise ConfigError("hakchi.host is required")

    api_token = os.environ.get("ROMM_API_TOKEN")
    if not api_token:
        raise ConfigError(
            "ROMM_API_TOKEN is not set - put it in a .env file or export it "
            "(see .env.example)"
        )

    games = []
    for i, g in enumerate(raw.get("games", [])):
        if "hakchi_code" not in g or "rom_id" not in g:
            raise ConfigError(f"games[{i}] must have hakchi_code and rom_id")
        games.append(
            GameEntry(
                hakchi_code=g["hakchi_code"],
                rom_id=int(g["rom_id"]),
                display_name=g.get("display_name"),
                emulator=g.get("emulator"),
            )
        )
    if not games:
        raise ConfigError("config must list at least one game under 'games'")

    return AppConfig(
        romm_base_url=romm["base_url"].rstrip("/"),
        romm_api_token=api_token,
        hakchi_host=hakchi["host"],
        hakchi_user=hakchi.get("user", "root"),
        hakchi_port=int(hakchi.get("port", 22)),
        hakchi_key_path=os.path.expanduser(hakchi["key_path"]) if hakchi.get("key_path") else None,
        slot=raw.get("slot", "auto-sync"),
        autocleanup_limit=int(raw.get("autocleanup_limit", 3)),
        games=games,
    )
