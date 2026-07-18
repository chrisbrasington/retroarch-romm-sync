from __future__ import annotations

import posixpath
import shlex

from .config import StateUploadPolicy
from .device import InstalledGame, SaveState
from .ssh_transport import SaveNotFoundError, SSHSession


class RetroArchSSHClient:
    """Reads save files off a stock-RetroArch device (e.g. Anbernic stock
    firmware, OnionOS on Miyoo Mini) over SSH.

    Unlike hakchi2-ce, these have no Clover layout and no short game code -
    games are identified by their ROM's base filename (stem), and
    saves/states live under a per-device subdirectory (a "system" folder on
    Anbernic, a RetroArch "core name" folder on OnionOS) that isn't
    reliably derivable from ROM discovery alone. Rather than modeling that
    as a per-brand distinction, this client discovers the subdirectory
    empirically per game (see resolve_path_hint) - Anbernic vs OnionOS is
    purely a matter of which roots/globs a device's config block sets, not
    a different class.
    """

    def __init__(
        self,
        device_id: str,
        host: str,
        user: str = "root",
        port: int = 22,
        auth: str = "none",
        key_path: str | None = None,
        password: str | None = None,
        roms_root: str = "",
        saves_root: str = "",
        states_root: str = "",
        save_extension: str = "srm",
        state_glob: str = "{basename}.state*",
        rom_extensions: list[str] | None = None,
    ):
        self.id = device_id
        self._ssh = SSHSession(
            host=host, user=user, port=port, auth=auth, key_path=key_path, password=password
        )
        self._roms_root = roms_root
        self._saves_root = saves_root
        self._states_root = states_root
        self._save_extension = save_extension
        self._state_glob = state_glob
        self._rom_extensions = rom_extensions or []

    def connect(self) -> None:
        self._ssh.connect()

    def close(self) -> None:
        self._ssh.close()

    def __enter__(self) -> RetroArchSSHClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- DeviceClient interface --------------------------------------------

    def list_installed_games(self) -> list[InstalledGame]:
        name_filter = ""
        if self._rom_extensions:
            parts = " -o ".join(f"-iname '*.{ext.lstrip('.')}'" for ext in self._rom_extensions)
            name_filter = f"\\( {parts} \\)"
        command = f"find {shlex.quote(self._roms_root)} -type f {name_filter} 2>/dev/null"
        out, _, _ = self._ssh.exec(command)

        games: dict[str, InstalledGame] = {}
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            stem = posixpath.splitext(posixpath.basename(line))[0]
            games.setdefault(stem, InstalledGame(id=stem, name=stem))
        return list(games.values())

    def list_ids_with_save(self) -> set[str]:
        return self._stems_under(self._saves_root)

    def list_ids_with_state(self) -> set[str]:
        return self._stems_under(self._states_root)

    def resolve_path_hint(self, game_id: str) -> str | None:
        subdirs = self._matching_subdirs(self._saves_root, game_id) | self._matching_subdirs(
            self._states_root, game_id
        )
        if len(subdirs) == 1:
            return next(iter(subdirs))
        if len(subdirs) > 1:
            print(
                f"  warning: {game_id!r} matches multiple save/state subdirectories "
                f"{sorted(subdirs)} - set path_hint explicitly in config.yaml"
            )
        return None

    def read_save(self, game_id: str, path_hint: str | None) -> bytes:
        save_dir = self._resolved_dir(self._saves_root, path_hint)
        path = posixpath.join(save_dir, f"{game_id}.{self._save_extension}")
        return self._ssh.read_file(path)

    def read_states(
        self, game_id: str, path_hint: str | None, policy: StateUploadPolicy
    ) -> list[SaveState]:
        state_dir = self._resolved_dir(self._states_root, path_hint)
        paths = self._find_state_paths(state_dir, game_id)
        if not paths:
            return []
        if policy is StateUploadPolicy.LATEST:
            paths = [self._pick_latest_state_path(paths, game_id)]
        return [self._read_state_at(path, game_id) for path in paths]

    @staticmethod
    def _pick_latest_state_path(paths: list[str], game_id: str) -> str:
        # Many of these handhelds have no battery-backed RTC, so each boot's
        # file mtimes are relative to whatever arbitrary clock value the
        # device came up with that session, not real wall-clock time -
        # comparing mtimes across different play sessions/boots is
        # unreliable (confirmed live: a manual numbered slot from an older
        # session had a *later* raw mtime than the auto-save slot from a
        # more recent one). RetroArch's "auto" save-state slot is written
        # every time a game is suspended/exited, which in practice is the
        # most reliable signal for "last played" - prefer it outright over
        # the mtime-sorted comparison when it exists.
        auto_name = f"{game_id}.state.auto".lower()
        auto_path = next((p for p in paths if posixpath.basename(p).lower() == auto_name), None)
        return auto_path or paths[-1]

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _resolved_dir(root: str, path_hint: str | None) -> str:
        return posixpath.join(root, path_hint) if path_hint else root

    def _stems_under(self, root: str) -> set[str]:
        command = f"find {shlex.quote(root)} -type f 2>/dev/null"
        out, _, _ = self._ssh.exec(command)
        stems = set()
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if line:
                stems.add(posixpath.splitext(posixpath.basename(line))[0])
        return stems

    def _matching_subdirs(self, root: str, game_id: str) -> set[str]:
        command = f"find {shlex.quote(root)} -type f -iname {shlex.quote(game_id + '.*')} 2>/dev/null"
        out, _, _ = self._ssh.exec(command)
        subdirs = set()
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            rel = posixpath.relpath(posixpath.dirname(line), root)
            subdirs.add("" if rel == "." else rel)
        return subdirs

    def _find_state_paths(self, state_dir: str, game_id: str) -> list[str]:
        pattern = self._state_glob.format(basename=game_id)
        # RetroArch (confirmed live on OnionOS) writes each state's thumbnail
        # as a sibling "<state filename>.png" - e.g. "Foo.state.auto" plus
        # "Foo.state.auto.png". Both match a naive "{basename}.state*" glob,
        # and the .png is written a moment after the state itself, so
        # without excluding it, "latest" picks the thumbnail instead of the
        # actual state. Thumbnails are still found separately, per real
        # state file, in _read_state_at().
        command = (
            f"find {shlex.quote(state_dir)} -maxdepth 1 -type f -iname {shlex.quote(pattern)} "
            "! -iname '*.png' 2>/dev/null "
            "| while read -r f; do stat -c '%Y %n' \"$f\"; done "
            "| sort -n | cut -d' ' -f2-"
        )
        out, _, _ = self._ssh.exec(command)
        return [line for line in out.decode(errors="replace").splitlines() if line.strip()]

    def _read_state_at(self, path: str, game_id: str) -> SaveState:
        filename = posixpath.basename(path)
        slot_label = filename[len(game_id):].lstrip(".") or "state"
        # Thumbnail naming isn't confirmed for these firmwares - try the
        # RetroArch convention of appending .png to the full state filename,
        # tolerate it not existing.
        screenshot_path = f"{path}.png"
        try:
            screenshot = self._ssh.read_file(screenshot_path)
        except SaveNotFoundError:
            screenshot = None
        return SaveState(data=self._ssh.read_file(path), screenshot=screenshot, slot_label=slot_label)
