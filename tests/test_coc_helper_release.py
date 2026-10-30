"""Release packaging must work without the optional onboarding installer."""
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest


@pytest.mark.parametrize("machine,elf_class,elf_machine,flags", [
    ("x86_64", 2, 62, 0), ("aarch64", 2, 183, 0), ("armv7l", 1, 40, 0x5000400),
])
def test_packager_is_standalone_and_verifies_architecture(tmp_path, machine, elf_class, elf_machine, flags):
    script = tmp_path / "package_release.py"
    shutil.copyfile(Path(__file__).parents[1] / "coc-helper/package_release.py", script)
    binary = tmp_path / "helper"
    data = bytearray(64)
    data[:6] = b"\x7fELF" + bytes([elf_class, 1])
    struct.pack_into("<HH", data, 16, 3, elf_machine)
    struct.pack_into("<I", data, 36, flags)
    binary.write_bytes(data)
    output = tmp_path / "assets"
    result = subprocess.run([sys.executable, "-I", str(script), "--binary", str(binary),
                             "--machine", machine, "--output", str(output)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    name = "observer-coc-helper-" + ("armv7" if machine == "armv7l" else machine)
    assert (output / name).read_bytes() == data
    assert (output / "SHA256SUMS").read_text() == hashlib.sha256(data).hexdigest() + "  " + name + "\n"


def test_packager_rejects_non_elf_before_creating_output(tmp_path):
    binary = tmp_path / "helper"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    output = tmp_path / "assets"
    script = Path(__file__).parents[1] / "coc-helper/package_release.py"
    result = subprocess.run([sys.executable, str(script), "--binary", str(binary),
                             "--machine", "x86_64", "--output", str(output)], capture_output=True)
    assert result.returncode != 0 and not output.exists()
