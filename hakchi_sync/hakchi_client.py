from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass

import paramiko

_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)


class HakchiError(Exception):
    pass


class SaveNotFoundError(HakchiError):
    pass


@dataclass(frozen=True)
class InstalledGame:
    code: str
    name: str
    exec_line: str


@dataclass(frozen=True)
class SaveState:
    data: bytes  # still gzip/RZIP-wrapped, as stored on disk
    screenshot: bytes | None


def _load_private_key(path: str) -> paramiko.PKey:
    last_exc: Exception | None = None
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key_file(path)
        except paramiko.SSHException as exc:
            last_exc = exc
    raise HakchiError(f"could not load private key {path}: {last_exc}")


def _parse_desktop_fields(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    return fields


class HakchiClient:
    """Reads save files and game metadata off a hakchi2-ce SNES Mini over SSH.

    Uses paramiko's Transport directly (not the SFTP client - the device has
    no sftp-server binary) and runs shell commands over plain session
    channels instead.

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
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
    ):
        self._host = host
        self._user = user
        self._port = port
        self._key_path = key_path
        self._transport: paramiko.Transport | None = None

    def connect(self) -> None:
        transport = paramiko.Transport((self._host, self._port))
        try:
            transport.start_client(timeout=10)
            if self._key_path:
                transport.auth_publickey(self._user, _load_private_key(self._key_path))
            else:
                transport.auth_none(self._user)
        except BaseException:
            transport.close()
            raise
        self._transport = transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> HakchiClient:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def read_cartridge_sram(self, hakchi_code: str) -> bytes:
        path = posixpath.join(self.SAVE_ROOT, hakchi_code, "cartridge.sram")
        return self._read_file(path)

    def read_latest_savestate(self, hakchi_code: str) -> SaveState | None:
        """Returns the most recently written suspend-point state for a game
        (still gzip/RZIP-wrapped as stored on disk) plus its thumbnail
        screenshot if one exists, or None if it has never been suspended.
        """
        game_dir = posixpath.join(self.SAVE_ROOT, hakchi_code)
        command = (
            f"find {shlex.quote(game_dir)} -name savestate 2>/dev/null "
            "| while read -r f; do stat -c '%Y %n' \"$f\"; done "
            "| sort -n | tail -1 | cut -d' ' -f2-"
        )
        out, _, _ = self._exec(command)
        path = out.decode(errors="replace").strip()
        if not path:
            return None

        # path is .../suspendpointN/rollback/savestate - its screenshot is
        # the sibling .../suspendpointN/state.png.
        suspendpoint_dir = posixpath.dirname(posixpath.dirname(path))
        screenshot_path = posixpath.join(suspendpoint_dir, "state.png")
        try:
            screenshot = self._read_file(screenshot_path)
        except SaveNotFoundError:
            screenshot = None

        return SaveState(data=self._read_file(path), screenshot=screenshot)

    def list_installed_games(self) -> list[InstalledGame]:
        """Every game hakchi2-ce has installed, across every console it
        emulates, read from each game's own .desktop launcher file.
        """
        command = (
            f"find {self.GAMES_ROOT} -maxdepth 2 -iname '*.desktop' "
            "-exec sh -c 'echo ---GAME---; cat \"$1\"' _ {} \\;"
        )
        out, _, _ = self._exec(command)
        text = out.decode(errors="replace")

        games = []
        for block in text.split("---GAME---")[1:]:
            fields = _parse_desktop_fields(block)
            code = fields.get("Code")
            name = fields.get("Name")
            if code and name:
                games.append(InstalledGame(code=code, name=name, exec_line=fields.get("Exec", "")))
        return games

    def list_codes_with_cartridge_sram(self) -> set[str]:
        """hakchi_codes that have an actual battery save on the device."""
        command = (
            f"for d in {self.SAVE_ROOT}/*/; do "
            "[ -f \"$d/cartridge.sram\" ] && basename \"$d\"; done"
        )
        out, _, _ = self._exec(command)
        return {line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()}

    def list_codes_with_savestate(self) -> set[str]:
        """hakchi_codes that have at least one suspend-point state on the
        device, even if they have no cartridge.sram (e.g. games with no
        battery save at all, like many original Game Boy carts).
        """
        command = (
            f"for d in {self.SAVE_ROOT}/*/; do "
            'find "$d" -name savestate 2>/dev/null | grep -q . && basename "$d"; done'
        )
        out, _, _ = self._exec(command)
        return {line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()}

    def _read_file(self, remote_path: str) -> bytes:
        out, err, exit_status = self._exec(f"cat -- {shlex.quote(remote_path)}")
        if exit_status != 0:
            message = err.decode(errors="replace").strip()
            if "No such file" in message:
                raise SaveNotFoundError(f"{remote_path}: {message}")
            raise HakchiError(f"failed to read {remote_path}: {message}")
        return out

    def _exec(self, command: str) -> tuple[bytes, bytes, int]:
        if self._transport is None:
            raise HakchiError("not connected - call connect() first")

        channel = self._transport.open_session(timeout=10)
        try:
            channel.exec_command(command)
            out = channel.makefile("rb").read()
            err = channel.makefile_stderr("rb").read()
            exit_status = channel.recv_exit_status()
        finally:
            channel.close()

        return out, err, exit_status
