#!/usr/bin/env python3
"""Convert a hakchi2-ce/RetroArch save state into the raw format RomM/EmulatorJS expects.

hakchi's Clover launcher stores suspend points as gzip(RZIP(RASTATE)):
  - outer layer: plain gzip (added by Clover when writing to /var/lib/clover/.../rollback/savestate)
  - middle layer: RetroArch's own RZIP chunked-zlib format
  - inner payload: a RASTATE-wrapped core save state (what RetroArch/EmulatorJS actually load)

Usage: convert_savestate.py <input savestate> <output .state file>
"""
import sys
import gzip
import zlib
import struct

RZIP_MAGIC = b"#RZIPv\x01#"


def rzip_decompress(data):
    if data[:8] != RZIP_MAGIC:
        raise ValueError(f"not an RZIP stream (magic={data[:8]!r})")

    chunk_size, total_size = struct.unpack("<IQ", data[8:20])
    pos = 20
    out = bytearray()

    while len(out) < total_size:
        comp_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        chunk = data[pos:pos + comp_len]
        pos += comp_len
        # Each chunk is its own complete, independent zlib stream (RetroArch
        # calls trans() with flush=true per chunk) - a shared decompressor
        # object across chunks produces garbage after the first chunk.
        out += zlib.decompressobj().decompress(chunk)

    if pos != len(data):
        raise ValueError(f"leftover bytes after decompression: {len(data) - pos}")
    if len(out) != total_size:
        raise ValueError(f"size mismatch: got {len(out)}, expected {total_size}")

    return bytes(out)


def convert(raw):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:8] == RZIP_MAGIC:
        raw = rzip_decompress(raw)
    return raw


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input savestate> <output .state file>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], "rb") as f:
        raw = f.read()

    result = convert(raw)

    if result[:7] != b"RASTATE":
        print(f"warning: output does not start with RASTATE magic (got {result[:7]!r}) - "
              "conversion may have failed or input format has changed", file=sys.stderr)

    with open(sys.argv[2], "wb") as f:
        f.write(result)

    print(f"wrote {len(result)} bytes to {sys.argv[2]}")


if __name__ == "__main__":
    main()
