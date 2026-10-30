"""Optional CoC setup uses real temporary files, never systemd or Bluetooth."""
import hashlib
import stat
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from installer import InstallerContext


def installation(tmp_path):
    ctx = InstallerContext(install_dir=str(tmp_path / "app"), config_dir=str(tmp_path / "config"))
    app, config = Path(ctx.install_dir), Path(ctx.config_dir)
    app.mkdir()
    (app / "mctomqtt.py").write_text("# installed\n")
    (app / ".install_type").write_text("systemd")
    (config / "config.d").mkdir(parents=True)
    (config / "config.toml").write_text('[general]\nsync_time = true\ntransport = "serial"\n')
    (config / "config.d/99-user.toml").write_text(
        '[general]\niata = "XXX"\nsync_time = false\n[[broker]]\nname = "existing"\nserver = "broker.invalid"\n')
    units = tmp_path / "units"
    units.mkdir()
    (units / "mctomqtt.service").write_text('[Service]\nUser=mctomqtt\nProtectSystem=strict\n')
    helper = tmp_path / "downloaded-helper"
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, 3, 62)
    helper.write_bytes(data)
    return ctx, units, helper, hashlib.sha256(data).hexdigest()


def configure(tmp_path, **kwargs):
    from installer.coc import configure_coc
    ctx, units, helper, digest = installation(tmp_path)
    before = (Path(ctx.config_dir) / "config.d/99-user.toml").read_bytes()
    result = configure_coc(ctx, address="02:00:00:00:00:01", helper=helper,
                           sha256=digest, machine="x86_64", unit_dir=units,
                           helper_dir=tmp_path / "libexec", **kwargs)
    return ctx, units, helper, result, before


def test_configure_preserves_user_brokers_and_clock_policy(tmp_path):
    ctx, units, helper, result, before = configure(tmp_path)
    assert (Path(ctx.config_dir) / "config.d/99-user.toml").read_bytes() == before
    profile = tomllib.loads(result["config"].read_text())
    assert profile["general"] == {"transport": "coc"}
    assert profile["coc"]["address"] == "02:00:00:00:00:01"
    assert profile["coc"]["helper"] == str(result["helper"])
    assert profile["coc"]["data_socket"] == "/run/mctomqtt-coc/data.sock"
    assert result["helper"].read_bytes() == helper.read_bytes()
    assert stat.S_IMODE(result["helper"].stat().st_mode) == 0o755
    assert stat.S_IMODE(result["config"].stat().st_mode) == 0o644
    dropin = result["dropin"].read_text()
    assert "RuntimeDirectory=mctomqtt-coc" in dropin
    assert "RuntimeDirectoryMode=0700" in dropin
    assert "ProtectSystem=" not in dropin and "ExecStart=" not in dropin
    assert "User=" not in dropin


@pytest.mark.parametrize("change", ["checksum", "machine", "address", "adapter", "symlink", "collision", "override", "root-service", "duplicate-user", "writable-parent"])
def test_invalid_setup_has_no_installation_side_effects(tmp_path, change):
    from installer.coc import configure_coc
    ctx, units, helper, digest = installation(tmp_path)
    address, adapter, machine = "02:00:00:00:00:01", "hci0", "x86_64"
    if change == "checksum": digest = "0" * 64
    if change == "machine": machine = "aarch64"
    if change == "address": address = 'bad\n[broker]'
    if change == "adapter": adapter = "../hci0"
    if change == "symlink":
        real = tmp_path / "real"
        helper.rename(real)
        helper.symlink_to(real)
    if change == "collision":
        (Path(ctx.config_dir) / "config.d/90-coc.toml").write_text("# operator-owned\n")
    if change == "override":
        (Path(ctx.config_dir) / "config.d/99-user.toml").write_text('[general]\ntransport="serial"\n')
    if change == "root-service":
        (units / "mctomqtt.service").write_text('[Service]\nUser=root\n')
    if change == "duplicate-user":
        (units / "mctomqtt.service").write_text('[Service]\nUser=mctomqtt\nUser=0\n')
    if change == "writable-parent":
        (tmp_path / "libexec").mkdir(mode=0o777)
        (tmp_path / "libexec").chmod(0o777)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        configure_coc(ctx, address=address, adapter=adapter, helper=helper,
                      sha256=digest, machine=machine, unit_dir=units, helper_dir=tmp_path / "libexec")
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert (tmp_path / "libexec").exists() == (change == "writable-parent")


def test_repeated_setup_is_idempotent(tmp_path):
    from installer.coc import configure_coc
    ctx, units, helper, result, before = configure(tmp_path)
    repeated = configure_coc(ctx, address="02:00:00:00:00:01", helper=helper,
                             sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                             machine="x86_64", unit_dir=units, helper_dir=tmp_path / "libexec")
    assert result == repeated


def test_helper_release_names_and_checksum_validation():
    from installer.coc import release_asset, verify_helper
    assert release_asset("0.1.0", "armv7l") == (
        "https://github.com/throny/meshcoretomqtt/releases/download/coc-helper-v0.1.0/observer-coc-helper-armv7", "observer-coc-helper-armv7")
    with pytest.raises(ValueError): release_asset("../../latest", "x86_64")
    with pytest.raises(ValueError): release_asset("0.1.0", "riscv64")
    with pytest.raises(ValueError): verify_helper(b"#!/bin/sh\n", "0" * 64, "x86_64")


def test_non_systemd_install_is_not_modified(tmp_path):
    from installer.coc import configure_coc
    ctx, units, helper, digest = installation(tmp_path)
    (Path(ctx.install_dir) / ".install_type").write_text("docker")
    with pytest.raises(ValueError, match="systemd"):
        configure_coc(ctx, address="02:00:00:00:00:01", helper=helper, sha256=digest,
                      machine="x86_64", unit_dir=units, helper_dir=tmp_path / "libexec")


def test_optional_setup_help_does_not_need_root():
    result = subprocess.run([sys.executable, "-m", "installer", "configure-coc", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "--helper-version" in result.stdout and "--sha256" in result.stdout


def test_local_helper_requires_independent_digest_before_root_check():
    result = subprocess.run([sys.executable, "-m", "installer", "configure-coc",
                             "--address", "02:00:00:00:00:01", "--helper", "/missing"],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "--sha256" in result.stderr


def test_release_checksums_are_exact_and_unambiguous():
    from installer.coc import checksum_for, _https_release_url
    digest = "a" * 64
    assert checksum_for(f"{digest}  observer-coc-helper-armv7\n".encode(),
                        "observer-coc-helper-armv7") == digest
    for bad in [b"", (f"{digest}  other\n").encode(),
                (f"{digest}  helper\n{digest}  helper\n").encode()]:
        with pytest.raises(ValueError): checksum_for(bad, "helper")
    for url in ["http://github.com/file", "https://evil.invalid/file",
                "https://github.com@evil.invalid/file", "https://github.com:444/file"]:
        with pytest.raises(ValueError): _https_release_url(url)


@pytest.mark.parametrize("machine,elf_class,elf_machine,flags", [
    ("x86_64", 2, 62, 0), ("aarch64", 2, 183, 0), ("armv7l", 1, 40, 0x5000400),
])
def test_verified_helper_matches_supported_elf_abi(machine, elf_class, elf_machine, flags):
    from installer.coc import verify_helper
    data = bytearray(64)
    data[:6] = b"\x7fELF" + bytes([elf_class, 1])
    struct.pack_into("<HH", data, 16, 3, elf_machine)
    struct.pack_into("<I", data, 36, flags)
    verify_helper(data, hashlib.sha256(data).hexdigest(), machine)
    if machine == "armv7l":
        struct.pack_into("<I", data, 36, 0)
        with pytest.raises(ValueError, match="hard-float"):
            verify_helper(data, hashlib.sha256(data).hexdigest(), machine)


def test_helper_packaging_writes_downloadable_asset_and_checksums(tmp_path):
    _, _, helper, digest = installation(tmp_path)
    output = tmp_path / "release"
    result = subprocess.run([sys.executable, "coc-helper/package_release.py", "--binary", str(helper),
                             "--machine", "x86_64", "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (output / "observer-coc-helper-x86_64").read_bytes() == helper.read_bytes()
    assert (output / "SHA256SUMS").read_text() == digest + "  observer-coc-helper-x86_64\n"


def test_helper_packaging_rejects_wrong_architecture_without_output(tmp_path):
    _, _, helper, _ = installation(tmp_path)
    output = tmp_path / "release"
    result = subprocess.run([sys.executable, "coc-helper/package_release.py", "--binary", str(helper),
                             "--machine", "aarch64", "--output", str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
