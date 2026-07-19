from __future__ import annotations

import shlex
import socket

import paramiko

_CONNECT_TIMEOUT = 10  # seconds - bounds the raw TCP connect, not just the SSH handshake

_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)


class DeviceError(Exception):
    pass


class SaveNotFoundError(DeviceError):
    pass


def _load_private_key(path: str) -> paramiko.PKey:
    last_exc: Exception | None = None
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key_file(path)
        except paramiko.SSHException as exc:
            last_exc = exc
    raise DeviceError(f"could not load private key {path}: {last_exc}")


class SSHSession:
    """Runs shell commands and reads/writes files over a plain paramiko
    Transport, for devices with no sftp-server binary - reads use `cat`,
    writes use `cat >`, both over exec_command channels instead of SFTP.

    Shared by every device client (hakchi2-ce's HakchiClient and the generic
    RetroArchSSHClient) so auth handling (none/publickey/password) and the
    exec/read/write plumbing only live in one place.
    """

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        auth: str = "none",
        key_path: str | None = None,
        password: str | None = None,
    ):
        self._host = host
        self._user = user
        self._port = port
        self._auth = auth
        self._key_path = key_path
        self._password = password
        self._transport: paramiko.Transport | None = None

    def connect(self) -> None:
        # paramiko.Transport((host, port)) opens the socket itself with no
        # connect timeout - against a powered-off WiFi device that never
        # answers (as opposed to one that actively refuses the connection),
        # that hangs for the OS's default TCP timeout (often 60s+), not the
        # `timeout=` passed to start_client() below (which only bounds the
        # SSH banner exchange after the socket is already up). Connecting
        # the socket ourselves first bounds the whole thing.
        sock = socket.create_connection((self._host, self._port), timeout=_CONNECT_TIMEOUT)

        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=_CONNECT_TIMEOUT)
            if self._auth == "publickey":
                transport.auth_publickey(self._user, _load_private_key(self._key_path))
            elif self._auth == "password":
                transport.auth_password(self._user, self._password)
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

    def __enter__(self) -> SSHSession:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def read_file(self, remote_path: str) -> bytes:
        # Distinguish "file doesn't exist" (fine - callers like the
        # optional screenshot lookup tolerate this) from a real read
        # failure via a POSIX `[ -e ]` check and a dedicated exit-code
        # sentinel, rather than matching cat's stderr text. That text is
        # locale-dependent - confirmed live: an RG34xx unit reports
        # "没有那个文件或目录" instead of "No such file or directory",
        # which silently turned a missing (optional) screenshot into a
        # hard failure that killed the whole state upload.
        quoted = shlex.quote(remote_path)
        command = f"if [ -e {quoted} ]; then cat -- {quoted}; else exit 111; fi"
        out, err, exit_status = self.exec(command)
        if exit_status == 111:
            raise SaveNotFoundError(f"{remote_path}: no such file")
        if exit_status != 0:
            raise DeviceError(f"failed to read {remote_path}: {err.decode(errors='replace').strip()}")
        return out

    def write_file(self, remote_path: str, data: bytes) -> None:
        if self._transport is None:
            raise DeviceError("not connected - call connect() first")

        channel = self._transport.open_session(timeout=30)
        try:
            channel.exec_command(f"cat > {shlex.quote(remote_path)}")
            channel.sendall(data)
            channel.shutdown_write()
            err = channel.makefile_stderr("rb").read()
            exit_status = channel.recv_exit_status()
        finally:
            channel.close()

        if exit_status != 0:
            raise DeviceError(f"failed to write {remote_path}: {err.decode(errors='replace').strip()}")

    def exec(self, command: str) -> tuple[bytes, bytes, int]:
        if self._transport is None:
            raise DeviceError("not connected - call connect() first")

        channel = self._transport.open_session(timeout=10)
        try:
            channel.exec_command(command)
            out = channel.makefile("rb").read()
            err = channel.makefile_stderr("rb").read()
            exit_status = channel.recv_exit_status()
        finally:
            channel.close()

        return out, err, exit_status
