from __future__ import annotations

import gzip
import struct
import zlib

_RZIP_MAGIC = b"#RZIPv\x01#"
_RZIP_CHUNK_SIZE = 131072  # RetroArch's RZIP_DEFAULT_CHUNK_SIZE
_RZIP_COMPRESSION_LEVEL = 6  # RetroArch's RZIP_COMPRESSION_LEVEL


class StateDecodeError(Exception):
    pass


def decode_savestate(raw: bytes) -> bytes:
    """Unwrap a hakchi2-ce/RetroArch suspend-point file into the raw
    RASTATE-format state RetroArch/EmulatorJS actually load.

    Clover stores suspend points as gzip(RZIP(RASTATE)): an outer plain-gzip
    layer, wrapping RetroArch's own RZIP chunked-zlib format, wrapping the
    core's actual serialized state.
    """
    data = raw
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data[:8] == _RZIP_MAGIC:
        data = _rzip_decompress(data)
    return data


def encode_savestate(raw: bytes) -> bytes:
    """The inverse of decode_savestate: wraps a raw RASTATE payload back into
    the gzip(RZIP(...)) form hakchi2-ce expects to find on disk, so a state
    pulled from RomM can be written back to the device and resumed.
    """
    header = _RZIP_MAGIC + struct.pack("<IQ", _RZIP_CHUNK_SIZE, len(raw))
    body = bytearray()
    for offset in range(0, len(raw), _RZIP_CHUNK_SIZE):
        chunk = raw[offset : offset + _RZIP_CHUNK_SIZE]
        compressed = zlib.compress(chunk, _RZIP_COMPRESSION_LEVEL)
        body += struct.pack("<I", len(compressed))
        body += compressed
    return gzip.compress(header + bytes(body))


def _rzip_decompress(data: bytes) -> bytes:
    chunk_size, total_size = struct.unpack("<IQ", data[8:20])
    if chunk_size == 0:
        raise StateDecodeError("RZIP header declares a zero chunk size")

    pos = 20
    out = bytearray()

    while len(out) < total_size:
        if pos + 4 > len(data):
            raise StateDecodeError("truncated RZIP stream (missing chunk header)")
        comp_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        chunk = data[pos : pos + comp_len]
        pos += comp_len
        # Each chunk is its own complete, independent zlib stream (RetroArch
        # calls trans() with flush=true per chunk) - a shared decompressor
        # object across chunks produces garbage after the first chunk.
        out += zlib.decompressobj().decompress(chunk)

    if pos != len(data):
        raise StateDecodeError(f"leftover bytes after decompression: {len(data) - pos}")
    if len(out) != total_size:
        raise StateDecodeError(f"size mismatch: got {len(out)}, expected {total_size}")

    return bytes(out)
