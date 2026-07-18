from __future__ import annotations

import posixpath
import shlex

from .config import StateUploadPolicy
from .device import InstalledGame, SaveState
from .ssh_transport import DeviceError, SaveNotFoundError, SSHSession


def _parse_desktop_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    return fields


class HakchiClient:
    """Reads save files and game metadata off a hakchi2-ce SNES Mini over SSH.

    hakchi2-ce's dropbear SSH server accepts unauthenticated ("none") root
    logins on its local link by default - the same fallback a plain
    `ssh root@hakchi` uses when no key matches - so no key_path is required
    unless the device has been hardened with real key-based auth.
    """

    SAVE_ROOT = "/var/lib/clover/profiles/0"
    # Every installed game's launcher metadata lives here regardless of which
    # console it emulates (NES/SNES/Game Boy/etc all share this one tree).
    GAMES_ROOT = "/var/lib/hakchi/games/snes-usa/000"

    def __init__(
        self,
        device_id: str,
        host: str,
        user: str = "root",
        port: int = 22,
        auth: str = "none",
        key_path: str | None = None,
        password: str | None = None,
    ):
        self.id = device_id
        self._ssh = SSHSession(
            host=host, user=user, port=port, auth=auth, key_path=key_path, password=password
        )

    def connect(self) -> None:
        self._ssh.connect()

    def close(self) -> None:
        self._ssh.close()

    def __enter__(self) -> HakchiClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- DeviceClient interface --------------------------------------------

    def list_installed_games(self) -> list[InstalledGame]:
        """Every game hakchi2-ce has installed, across every console it
        emulates, read from each game's own .desktop launcher file.
        """
        command = (
            f"find {self.GAMES_ROOT} -maxdepth 2 -iname '*.desktop' "
            "-exec sh -c 'echo ---GAME---; cat \"$1\"' _ {} \\;"
        )
        out, _, _ = self._ssh.exec(command)
        text = out.decode(errors="replace")

        games = []
        for block in text.split("---GAME---")[1:]:
            fields = _parse_desktop_fields(block)
            code = fields.get("Code")
            name = fields.get("Name")
            if code and name:
                games.append(InstalledGame(id=code, name=name))
        return games

    def list_ids_with_save(self) -> set[str]:
        """hakchi_codes that have an actual battery save on the device."""
        command = (
            f"for d in {self.SAVE_ROOT}/*/; do "
            "[ -f \"$d/cartridge.sram\" ] && basename \"$d\"; done"
        )
        out, _, _ = self._ssh.exec(command)
        return {line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()}

    def list_ids_with_state(self) -> set[str]:
        """hakchi_codes that have at least one suspend-point state on the
        device, even if they have no cartridge.sram (e.g. games with no
        battery save at all, like many original Game Boy carts).
        """
        command = (
            f"for d in {self.SAVE_ROOT}/*/; do "
            'find "$d" -name savestate 2>/dev/null | grep -q . && basename "$d"; done'
        )
        out, _, _ = self._ssh.exec(command)
        return {line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()}

    def resolve_path_hint(self, game_id: str) -> str | None:
        return None  # concept doesn't apply - hakchi_code alone locates everything

    def read_save(self, game_id: str, path_hint: str | None) -> bytes:
        return self.read_cartridge_sram(game_id)

    def read_states(
        self, game_id: str, path_hint: str | None, policy: StateUploadPolicy
    ) -> list[SaveState]:
        if policy is StateUploadPolicy.ALL:
            return self.read_all_savestates(game_id)
        latest = self.read_latest_savestate(game_id)
        return [latest] if latest is not None else []

    # --- hakchi-specific methods (also used directly by push.py) ----------

    def read_cartridge_sram(self, hakchi_code: str) -> bytes:
        path = posixpath.join(self.SAVE_ROOT, hakchi_code, "cartridge.sram")
        return self._ssh.read_file(path)

    def read_latest_savestate(self, hakchi_code: str) -> SaveState | None:
        """Returns the most recently written suspend-point state for a game
        (still gzip/RZIP-wrapped as stored on disk) plus its thumbnail
        screenshot if one exists, or None if it has never been suspended.
        """
        paths = self._find_savestate_paths(hakchi_code)
        if not paths:
            return None
        return self._read_savestate_at(paths[-1])

    def read_all_savestates(self, hakchi_code: str) -> list[SaveState]:
        """Every suspend-point state for a game, oldest to newest."""
        return [self._read_savestate_at(path) for path in self._find_savestate_paths(hakchi_code)]

    def write_cartridge_sram(self, hakchi_code: str, data: bytes) -> None:
        """Overwrites a game's battery save on the device."""
        path = posixpath.join(self.SAVE_ROOT, hakchi_code, "cartridge.sram")
        self.write_file(path, data)

    def write_savestate(self, hakchi_code: str, data: bytes) -> str:
        """Overwrites the current suspend point's state for a game (creating
        one at suspendpoint1 if the game has never been suspended before).
        `data` must already be gzip/RZIP-wrapped (see state_codec.encode_savestate).
        Returns the suspend-point directory written to.
        """
        paths = self._find_savestate_paths(hakchi_code)
        if paths:
            suspendpoint_dir = posixpath.dirname(posixpath.dirname(paths[-1]))
        else:
            suspendpoint_dir = posixpath.join(self.SAVE_ROOT, hakchi_code, "suspendpoint1")

        rollback_dir = posixpath.join(suspendpoint_dir, "rollback")
        _, err, exit_status = self._ssh.exec(f"mkdir -p -- {shlex.quote(rollback_dir)}")
        if exit_status != 0:
            raise DeviceError(f"failed to create {rollback_dir}: {err.decode(errors='replace').strip()}")

        self.write_file(posixpath.join(rollback_dir, "savestate"), data)
        return suspendpoint_dir

    def write_file(self, remote_path: str, data: bytes) -> None:
        self._ssh.write_file(remote_path, data)

    def _read_savestate_at(self, path: str) -> SaveState:
        # path is .../suspendpointN/rollback/savestate - its screenshot is
        # the sibling .../suspendpointN/state.png.
        suspendpoint_dir = posixpath.dirname(posixpath.dirname(path))
        slot_label = posixpath.basename(suspendpoint_dir)
        screenshot_path = posixpath.join(suspendpoint_dir, "state.png")
        try:
            screenshot = self._ssh.read_file(screenshot_path)
        except SaveNotFoundError:
            screenshot = None
        return SaveState(data=self._ssh.read_file(path), screenshot=screenshot, slot_label=slot_label)

    def _find_savestate_paths(self, hakchi_code: str) -> list[str]:
        """Every suspend-point `savestate` file for a game, oldest to newest."""
        game_dir = posixpath.join(self.SAVE_ROOT, hakchi_code)
        command = (
            f"find {shlex.quote(game_dir)} -name savestate 2>/dev/null "
            "| while read -r f; do stat -c '%Y %n' \"$f\"; done "
            "| sort -n | cut -d' ' -f2-"
        )
        out, _, _ = self._ssh.exec(command)
        return [line for line in out.decode(errors="replace").splitlines() if line.strip()]
