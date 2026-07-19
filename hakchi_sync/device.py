from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .config import DeviceConfig, StateUploadPolicy

__all__ = [
    "StateUploadPolicy",
    "InstalledGame",
    "SaveState",
    "DeviceClient",
    "build_device_client",
]


@dataclass(frozen=True)
class InstalledGame:
    id: str
    name: str
    # Full on-device path to the ROM file, when the device exposes one (only
    # meaningful for stock-RetroArch devices - hakchi2-ce's games aren't
    # loose ROM files you'd copy off by hand). Shown during --setup so an
    # unmatched game can be located/copied manually.
    full_path: str | None = None


@dataclass(frozen=True)
class SaveState:
    data: bytes  # still however the device stores it on disk (wrapped or not)
    screenshot: bytes | None
    slot_label: str  # distinguishes multiple states for the same game, e.g. "suspendpoint2", "state1", "auto"


@runtime_checkable
class DeviceClient(Protocol):
    """Everything sync_service.py and setup_wizard.py need from a device,
    regardless of what it actually is underneath (hakchi2-ce/Clover, or
    stock RetroArch over SSH).
    """

    id: str

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> DeviceClient: ...
    def __exit__(self, *exc_info) -> None: ...

    def list_installed_games(self) -> list[InstalledGame]: ...
    def list_ids_with_save(self) -> set[str]: ...
    def list_ids_with_state(self) -> set[str]: ...

    def resolve_path_hint(self, game_id: str) -> str | None:
        """Extra device-specific location info worth pinning in config.yaml
        for this game (e.g. which saves_root subdirectory it actually lives
        in). Returns None where the concept doesn't apply.
        """
        ...

    def read_save(self, game_id: str, path_hint: str | None) -> bytes: ...
    def read_states(
        self, game_id: str, path_hint: str | None, policy: StateUploadPolicy
    ) -> list[SaveState]: ...


def build_device_client(device: DeviceConfig) -> DeviceClient:
    if device.type == "hakchi":
        from .hakchi_client import HakchiClient

        return HakchiClient(
            device_id=device.id,
            host=device.host,
            user=device.user,
            port=device.port,
            auth=device.auth,
            key_path=device.key_path,
            password=device.password,
        )
    if device.type == "retroarch_ssh":
        from .retroarch_client import RetroArchSSHClient

        return RetroArchSSHClient(
            device_id=device.id,
            host=device.host,
            user=device.user,
            port=device.port,
            auth=device.auth,
            key_path=device.key_path,
            password=device.password,
            roms_root=device.roms_root,
            saves_root=device.saves_root,
            states_root=device.states_root,
            save_extension=device.save_extension,
            state_glob=device.state_glob,
            rom_extensions=device.rom_extensions,
        )
    raise ValueError(f"unknown device type: {device.type!r}")
