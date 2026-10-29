#!/usr/bin/env python3
"""Fail-closed validation for the isolated observer unit's explicit TOML file."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

from bridge.state import BridgeState
from bridge.topics import resolve_topic_template
from config_loader import load_config, resolve_transport

_BLE_ADDRESS = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\Z")
_IATA = re.compile(r"[A-Z]{3}\Z")
_PLACEHOLDER = re.compile(r"REPLACE[_ -]?WITH", re.IGNORECASE)


class PreflightError(ValueError):
    """Safe-to-display configuration error; never contains config values."""


def validate_config(path: Path) -> None:
    """Validate the same single explicit config the service will pass to runtime."""
    try:
        if path.is_symlink() or not path.is_file():
            raise PreflightError("explicit config is missing or not a regular file")
        if path.stat().st_size == 0:
            raise PreflightError("explicit config is empty")
        config = load_config([str(path)])
    except PreflightError:
        raise
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise PreflightError("explicit config is unreadable or invalid TOML") from None

    general = config.get("general")
    if not isinstance(general, dict) or general.get("transport") != "ble":
        raise PreflightError('canonical [general].transport must be exactly "ble"')

    # Use the runtime resolver as well: it rejects invalid legacy values even
    # when canonical transport wins, and preserves canonical precedence.
    try:
        transport = resolve_transport(config)
    except (TypeError, ValueError):
        raise PreflightError("transport configuration is invalid") from None
    if transport != "ble":
        raise PreflightError('resolved transport must be exactly "ble"')

    ble = config.get("ble")
    if not isinstance(ble, dict):
        raise PreflightError("[ble] table is required")
    address = ble.get("address")
    if not isinstance(address, str) or not _BLE_ADDRESS.fullmatch(address):
        raise PreflightError("[ble].address must be a six-octet BLE address, not a placeholder")
    octets = bytes.fromhex(address.replace(":", ""))
    if not any(octets) or all(octet == 0xFF for octet in octets):
        raise PreflightError("[ble].address cannot be an all-zero or broadcast address")

    brokers = config.get("broker", [])
    if not isinstance(brokers, list):
        raise PreflightError("broker configuration must be an array of tables")
    topic_state = BridgeState({"general": general, "broker": brokers})
    for broker_index, broker in enumerate(brokers):
        if not isinstance(broker, dict):
            raise PreflightError("each broker entry must be a table")
        enabled = broker.get("enabled", False)
        if not isinstance(enabled, bool):
            raise PreflightError("broker enabled flag must be boolean")
        if not enabled:
            continue
        try:
            effective_iata = resolve_topic_template(topic_state, "{IATA}", broker_index)
        except (AttributeError, TypeError):
            raise PreflightError("enabled broker effective IATA cannot be resolved") from None
        if (
            not isinstance(effective_iata, str)
            or not _IATA.fullmatch(effective_iata)
            or effective_iata == "XXX"
        ):
            raise PreflightError("enabled broker requires an explicit non-placeholder three-letter IATA")
        server = broker.get("server")
        if not isinstance(server, str) or not server.strip():
            raise PreflightError("enabled broker requires an explicit endpoint")
        auth = broker.get("auth", {})
        if not isinstance(auth, dict):
            raise PreflightError("enabled broker auth must be a table")
        auth_method = auth.get("method", "none")
        if not isinstance(auth_method, str) or auth_method not in ("none", "password", "token"):
            raise PreflightError("enabled broker auth method is unsupported")
        if auth_method == "password":
            username = auth.get("username")
            password = auth.get("password")
            if (
                not isinstance(username, str)
                or not username.strip()
                or not isinstance(password, str)
                or not password
            ):
                raise PreflightError("enabled password-auth broker requires username and password")
        elif auth_method == "token":
            audience = auth.get("audience")
            if not isinstance(audience, str) or not audience.strip():
                raise PreflightError("enabled token-auth broker requires an audience")

        tls = broker.get("tls", {})
        if not isinstance(tls, dict):
            raise PreflightError("enabled broker TLS settings must be a table")
        if any(
            name in tls and not isinstance(tls[name], bool)
            for name in ("enabled", "verify")
        ):
            raise PreflightError("enabled broker TLS flags must be booleans")
        configured_values = (
            server, auth.get("audience"), auth.get("username"), auth.get("password"),
        )
        if any(isinstance(value, str) and _PLACEHOLDER.search(value) for value in configured_values):
            raise PreflightError("enabled broker contains a placeholder; configure it explicitly offline")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="one explicit TOML config file")
    args = parser.parse_args()
    try:
        validate_config(Path(args.config))
    except PreflightError as error:
        print(f"observer preflight: {error}", file=sys.stderr)
        return 2
    print("observer preflight: explicit BLE config validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
