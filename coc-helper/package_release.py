#!/usr/bin/env python3
"""Validate prebuilt Linux helpers and write standard release checksums."""
import argparse
import hashlib
from pathlib import Path
import struct


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.binary.read_bytes()
    arches = {"x86_64": (2, 62, "x86_64"), "aarch64": (2, 183, "aarch64"),
              "armv7l": (1, 40, "armv7")}
    if args.machine not in arches:
        raise ValueError("Unsupported helper architecture")
    elf_class, machine, suffix = arches[args.machine]
    if (len(data) < 64 or data[:6] != b"\x7fELF" + bytes([elf_class, 1])
            or struct.unpack_from("<H", data, 18)[0] != machine
            or struct.unpack_from("<H", data, 16)[0] not in (2, 3)
            or (machine == 40 and not struct.unpack_from("<I", data, 36)[0] & 0x400)):
        raise ValueError("ELF architecture/ABI does not match the release asset")
    name = "observer-coc-helper-" + suffix
    if args.output.is_symlink():
        raise ValueError("Output must not be a symlink")
    asset = args.output / name
    if asset.exists() or asset.is_symlink():
        raise ValueError("Asset already exists")
    args.output.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(data)
    asset.chmod(0o755)
    assets = sorted(args.output.glob("observer-coc-helper-*"))
    checksums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in assets)
    (args.output / "SHA256SUMS").write_text(checksums)


if __name__ == "__main__":
    main()
