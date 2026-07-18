from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml
from dotenv import load_dotenv

_DEVICE_TYPES = ("hakchi", "retroarch_ssh")
_AUTH_MODES = ("none", "publickey", "password")


class ConfigError(Exception):
    pass


class StateUploadPolicy(Enum):
    LATEST = "latest"
    ALL = "all"


@dataclass(frozen=True)
class GameEntry:
    game_id: str
    rom_id: int
    display_name: str | None = None
    emulator: str | None = None
    # Extra device-specific location hint (e.g. which saves_root subdirectory
    # this game's files actually live under) - only meaningful for some
    # device types, see DeviceClient.resolve_path_hint().
    path_hint: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.game_id


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    type: str
    host: str
    enabled: bool = True
    user: str = "root"
    port: int = 22
    auth: str = "none"
    key_path: str | None = None
    password: str | None = None
    state_upload_policy: StateUploadPolicy = StateUploadPolicy.LATEST
    slot: str = "auto-sync"
    autocleanup_limit: int = 3
    max_states: int | None = None
    roms_root: str | None = None
    saves_root: str | None = None
    states_root: str | None = None
    save_extension: str = "sram"
    state_glob: str = "{basename}.state*"
    rom_extensions: list[str] = field(default_factory=list)
    games: list[GameEntry] = field(default_factory=list)

    def find_game(self, game_id: str) -> GameEntry | None:
        return next((g for g in self.games if g.game_id == game_id), None)


@dataclass(frozen=True)
class AppConfig:
    romm_base_url: str
    romm_api_token: str
    devices: list[DeviceConfig] = field(default_factory=list)

    def find_device(self, device_id: str) -> DeviceConfig | None:
        return next((d for d in self.devices if d.id == device_id), None)


def _resolve_password(d: dict, device_id: str) -> str:
    password = d.get("password")
    if password:
        return str(password)

    env_var = d.get("password_env")
    if not env_var:
        raise ConfigError(
            f"devices[{device_id}]: auth is 'password' but neither 'password' "
            "nor 'password_env' is set"
        )
    value = os.environ.get(env_var)
    if not value:
        raise ConfigError(
            f"devices[{device_id}]: {env_var} is not set - put it in .env or export it"
        )
    return value


def _load_device(d: dict) -> DeviceConfig:
    device_id = d.get("id")
    if not device_id:
        raise ConfigError("every entry under 'devices' must have an 'id'")

    device_type = d.get("type")
    if device_type not in _DEVICE_TYPES:
        raise ConfigError(
            f"devices[{device_id}]: type must be one of {_DEVICE_TYPES}, got {device_type!r}"
        )

    if not d.get("host"):
        raise ConfigError(f"devices[{device_id}]: host is required")

    enabled = bool(d.get("enabled", True))

    auth = d.get("auth", "none")
    if auth not in _AUTH_MODES:
        raise ConfigError(f"devices[{device_id}]: auth must be one of {_AUTH_MODES}, got {auth!r}")

    # A disabled device is a placeholder - don't force its secrets (an SSH
    # key on disk, a password env var) to already exist just to load config.
    key_path = None
    password = None
    if auth == "publickey":
        if enabled and not d.get("key_path"):
            raise ConfigError(f"devices[{device_id}]: auth is 'publickey' but key_path is not set")
        if d.get("key_path"):
            key_path = os.path.expanduser(d["key_path"])
    elif auth == "password" and enabled:
        password = _resolve_password(d, device_id)

    if device_type == "retroarch_ssh":
        for field_name in ("roms_root", "saves_root", "states_root"):
            if not d.get(field_name):
                raise ConfigError(f"devices[{device_id}]: {field_name} is required for type retroarch_ssh")

    default_policy = StateUploadPolicy.ALL if device_type == "hakchi" else StateUploadPolicy.LATEST
    policy_raw = d.get("state_upload_policy")
    if policy_raw is None:
        state_upload_policy = default_policy
    else:
        try:
            state_upload_policy = StateUploadPolicy(policy_raw)
        except ValueError:
            valid = [p.value for p in StateUploadPolicy]
            raise ConfigError(
                f"devices[{device_id}]: state_upload_policy must be one of {valid}, got {policy_raw!r}"
            ) from None

    default_save_extension = "sram" if device_type == "hakchi" else "srm"

    games = []
    for i, g in enumerate(d.get("games", [])):
        if "game_id" not in g or "rom_id" not in g:
            raise ConfigError(f"devices[{device_id}].games[{i}] must have game_id and rom_id")
        games.append(
            GameEntry(
                game_id=g["game_id"],
                rom_id=int(g["rom_id"]),
                display_name=g.get("display_name"),
                emulator=g.get("emulator"),
                path_hint=g.get("path_hint"),
            )
        )

    return DeviceConfig(
        id=device_id,
        type=device_type,
        host=d["host"],
        enabled=enabled,
        user=d.get("user", "root"),
        port=int(d.get("port", 22)),
        auth=auth,
        key_path=key_path,
        password=password,
        state_upload_policy=state_upload_policy,
        slot=d.get("slot", "auto-sync"),
        autocleanup_limit=int(d.get("autocleanup_limit", 3)),
        max_states=int(d["max_states"]) if d.get("max_states") is not None else None,
        roms_root=d.get("roms_root"),
        saves_root=d.get("saves_root"),
        states_root=d.get("states_root"),
        save_extension=d.get("save_extension", default_save_extension),
        state_glob=d.get("state_glob", "{basename}.state*"),
        rom_extensions=list(d.get("rom_extensions", [])),
        games=games,
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    load_dotenv()

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    try:
        romm = raw["romm"]
    except KeyError as exc:
        raise ConfigError(f"missing required top-level config section: {exc}") from exc

    if not romm.get("base_url"):
        raise ConfigError("romm.base_url is required")

    api_token = os.environ.get("ROMM_API_TOKEN")
    if not api_token:
        raise ConfigError(
            "ROMM_API_TOKEN is not set - put it in a .env file or export it "
            "(see .env.example)"
        )

    device_dicts = raw.get("devices") or []
    if not device_dicts:
        raise ConfigError(
            "config must list at least one device under 'devices' "
            "(see config.example.yaml - the old flat hakchi:/games: schema is gone)"
        )

    devices = [_load_device(d) for d in device_dicts]

    seen_ids = set()
    for device in devices:
        if device.id in seen_ids:
            raise ConfigError(f"duplicate device id: {device.id!r}")
        seen_ids.add(device.id)

    return AppConfig(
        romm_base_url=romm["base_url"].rstrip("/"),
        romm_api_token=api_token,
        devices=devices,
    )
