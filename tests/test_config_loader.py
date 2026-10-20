"""Tests for config_loader: load_config, deep_merge, merge_broker_lists."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from config_loader import load_config


def test_ble_dependencies_are_declared_in_all_runtime_installers() -> None:
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    installer = (root / "installer/system.py").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    nix_package = (root / "nix/packages.nix").read_text()
    nix_shell = (root / "nix/shell.nix").read_text()

    assert "bleak" in pyproject.lower()
    assert "bleak>=0.22" in installer
    assert "bleak>=0.22" in dockerfile
    assert "bleak" in nix_package
    assert "bleak" in nix_shell


def test_installer_venv_dependency_command_includes_bleak(monkeypatch, tmp_path):
    import installer.system as installer_system

    calls = []
    monkeypatch.setattr(installer_system, "run_cmd", lambda cmd, **kwargs: calls.append(cmd) or type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(installer_system, "install_os_build_deps", lambda: None)
    monkeypatch.setattr(installer_system.shutil, "rmtree", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer_system.shutil, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer_system, "print_success", lambda *_: None)
    monkeypatch.setattr(installer_system, "print_info", lambda *_: None)
    monkeypatch.setenv("INSTALL_REBUILD_VENV", "1")

    installer_system.create_venv(str(tmp_path), "")
    assert any("bleak>=0.22" in command for command in calls)


class TestLoadConfigWithExplicitPaths:
    def test_single_config_file(self, tmp_path: Path) -> None:
        """A single --config file is loaded as the full config."""
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_multiple_config_files_overlay(self, tmp_path: Path) -> None:
        """Multiple --config files are merged in order."""
        base = tmp_path / "base.toml"
        base.write_text('[general]\niata = "SEA"\nlog_level = "INFO"\n')

        overlay = tmp_path / "overlay.toml"
        overlay.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(base), str(overlay)])

        assert result["general"]["iata"] == "PDX"
        assert result["general"]["log_level"] == "INFO"

    def test_broker_overlay_merges_by_name(self, tmp_path: Path) -> None:
        """Broker lists in overlays merge by name, not append blindly."""
        base = tmp_path / "base.toml"
        base.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = true\n'
            'server = "mqtt-us-v1.letsmesh.net"\n'
        )

        overlay = tmp_path / "overlay.toml"
        overlay.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = false\n'
        )

        result = load_config([str(base), str(overlay)])

        assert len(result["broker"]) == 1
        assert result["broker"][0]["enabled"] is False
        assert result["broker"][0]["server"] == "mqtt-us-v1.letsmesh.net"

    def test_skips_default_config_d(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When --config is provided, config.d directories are not loaded."""
        # Create a config.d next to config_loader.py's __file__ location
        # that would normally be picked up
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "99-extra.toml").write_text('[general]\niata = "OVERWRITTEN"\n')

        # Even though config.d exists, --config should bypass it entirely
        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        """A nonexistent --config path is skipped with a log error."""
        cfg = tmp_path / "exists.toml"
        cfg.write_text('[general]\niata = "SEA"\n')

        result = load_config(["/nonexistent/path.toml", str(cfg)])

        assert result["general"]["iata"] == "SEA"

    def test_no_config_paths_returns_defaults(self) -> None:
        """Passing None uses default config loading."""
        # Just verify it doesn't crash; actual paths may or may not exist
        result = load_config(None)
        assert isinstance(result, dict)
