"""Installer runtime command construction; no install, mocks or subprocesses."""
from pathlib import Path

from installer.system import runtime_dependency_install_command


def test_ble_dependencies_are_declared_in_all_runtime_installers():
    root = Path(__file__).parents[1]
    assert "bleak" in (root / "pyproject.toml").read_text().lower()
    assert "bleak>=0.22" in (root / "installer/system.py").read_text()
    assert "bleak>=0.22" in (root / "Dockerfile").read_text()
    for name in ("packages.nix", "shell.nix"):
        assert "bleak" in (root / "nix" / name).read_text()


def test_installer_venv_dependency_command_includes_bleak(tmp_path):
    assert runtime_dependency_install_command(str(tmp_path / "venv")) == [
        str(tmp_path / "venv/bin/pip"), "install", "--quiet", "pyserial",
        "paho-mqtt", "ed25519-orlp", "bleak>=0.22",
    ]
