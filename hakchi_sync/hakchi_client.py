from __future__ import annotations

import posixpath
import shlex

import paramiko

_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)


class HakchiError(Exception):
    pass


class SaveNotFoundError(HakchiError):
    pass


def _load_private_key(path: str) -> paramiko.PKey:
    last_exc: Exception | None = None
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key_file(path)
        except paramiko.SSHException as exc:
            last_exc = exc
    raise HakchiError(f"could not load private key {path}: {last_exc}")


class HakchiClient:
    """Reads save files off a hakchi2-ce SNES Mini over SSH.

    Uses paramiko's Transport directly (not the SFTP client - the device has
    no sftp-server binary) and runs `cat` over a plain session channel.

    hakchi2-ce's dropbear SSH server accepts unauthenticated ("none") root
    logins on its local link by default - the same fallback a plain
    `ssh root@hakchi` uses when no key matches - so no key_path is required
    unless the device has been hardened with real key-based auth.
    """

    SAVE_ROOT = "/var/lib/clover/profiles/0"

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

    def _read_file(self, remote_path: str) -> bytes:
        if self._transport is None:
            raise HakchiError("not connected - call connect() first")

        channel = self._transport.open_session(timeout=10)
        try:
            channel.exec_command(f"cat -- {shlex.quote(remote_path)}")
            data = channel.makefile("rb").read()
            err = channel.makefile_stderr("rb").read().decode(errors="replace").strip()
            exit_status = channel.recv_exit_status()
        finally:
            channel.close()

        if exit_status != 0:
            if "No such file" in err:
                raise SaveNotFoundError(f"{remote_path}: {err}")
            raise HakchiError(f"failed to read {remote_path}: {err}")

        return data
