#!/usr/bin/env python3
"""Package prebuilt helpers and a standard SHA256SUMS file; never builds as root."""
import argparse
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer.coc import release_asset, verify_helper


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.binary.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    verify_helper(data, digest, args.machine)
    _, name = release_asset("0.1.0", args.machine)
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
