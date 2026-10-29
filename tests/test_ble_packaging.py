"""Offline artifact/consistency checks; never install or contact a device/service."""
from configparser import ConfigParser
from pathlib import Path
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

import pytest

from bridge.runner import _cleanup, handle_signal
from fakes import make_test_state
from test_ble_connection import FakeGatt, make_connection, close_ble_connections_after_test

ROOT = Path(__file__).parents[1]
REPO = ROOT.parents[1]


def service():
    unit = ConfigParser(interpolation=None)
    unit.read(ROOT / "systemd/meshcoretomqtt-ble.service")
    return unit


def test_unit_dedicated_identity_permissions_restart_and_journal_contract():
    unit = service()
    svc = unit["Service"]
    for key, expected in {
        "User": "mctomqtt", "Group": "mctomqtt", "UMask": "0077",
        "LimitCORE": "0", "Restart": "always", "RestartSec": "10",
        "KillSignal": "SIGTERM", "TimeoutStopSec": "30",
        "RuntimeDirectoryMode": "0700", "ReadOnlyPaths": "/etc/mctomqtt",
        "NoNewPrivileges": "true", "ProtectSystem": "strict",
        "ProtectHome": "true", "StandardOutput": "journal", "StandardError": "journal",
    }.items():
        assert svc[key] == expected
    assert "Install" not in unit
    assert "SupplementaryGroups" not in svc  # no blanket Bluetooth grant
    assert svc["ExecStart"] == "/opt/mctomqtt/venv/bin/python3 /opt/mctomqtt/mctomqtt.py"


def test_unit_verifies_only_in_isolated_root(tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze unavailable")
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    shutil.copy(ROOT / "systemd/meshcoretomqtt-ble.service", units)
    (units / "sysinit.target").write_text("[Unit]\nDescription=Isolated fixture\n")
    executable = tmp_path / "opt/mctomqtt/venv/bin/python3"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    result = subprocess.run(
        ["systemd-analyze", f"--root={tmp_path}", "verify", "meshcoretomqtt-ble.service"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_separate_observer_preflight_template_fails_closed_and_has_no_autostart(tmp_path):
    import tomllib

    config_path = ROOT / "config.ble-preflight.example.toml"
    config = tomllib.loads(config_path.read_text())
    assert config["general"]["transport"] == "ble"
    assert config["general"]["iata"] == "XXX"
    assert config["ble"]["address"] == "REPLACE_WITH_CONFIRMED_BONDED_BLE_ADDRESS"
    assert not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", config["ble"]["address"])
    brokers = config["broker"]
    assert {broker["name"] for broker in brokers} == {"meshmapper-placeholder", "letsmesh-placeholder"}
    assert all(broker["enabled"] is False for broker in brokers)
    assert all("REPLACE_WITH" in broker["server"] for broker in brokers)
    assert all("auth" not in broker or "password" not in broker["auth"] for broker in brokers)

    unit_text = (ROOT / "systemd/meshcore-observer-preflight.service").read_text()
    unit = ConfigParser(interpolation=None, strict=False)
    unit.read_string(unit_text)
    svc = unit["Service"]
    assert svc["User"] == svc["Group"] == "mctomqtt"
    assert svc["WorkingDirectory"] == "/opt/mctomqtt"
    assert svc["ExecStart"].endswith(
        "/opt/mctomqtt/mctomqtt.py --config /etc/mctomqtt/config.toml"
    )
    assert svc["RuntimeDirectory"] == "mctomqtt-ble"
    assert svc["ReadOnlyPaths"] == "/etc/mctomqtt"
    assert "--config /etc/mctomqtt/config.toml" in svc["ExecStart"]
    assert len(re.findall(r"^ExecStartPre=", unit_text, re.M)) == 1
    assert "preflight_config.py" in svc["ExecStartPre"]
    assert "--config /etc/mctomqtt/config.toml" in svc["ExecStartPre"]
    assert "/usr/bin/grep" not in unit_text and "/usr/bin/test" not in unit_text
    assert "Install" not in unit

    if shutil.which("systemd-analyze"):
        units = tmp_path / "etc/systemd/system"
        units.mkdir(parents=True)
        shutil.copy(ROOT / "systemd/meshcore-observer-preflight.service", units)
        (units / "sysinit.target").write_text("[Unit]\nDescription=Isolated fixture\n")
        for executable in (
            tmp_path / "opt/mctomqtt/venv/bin/python3",
        ):
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        config_fixture = tmp_path / "etc/mctomqtt/config.toml"
        config_fixture.parent.mkdir(parents=True)
        config_fixture.write_text(config_path.read_text())
        result = subprocess.run(
            ["systemd-analyze", f"--root={tmp_path}", "verify", "meshcore-observer-preflight.service"],
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def _run_declared_observer_preflight(config_text, config_path):
    config_path.write_text(config_text)
    unit_text = (ROOT / "systemd/meshcore-observer-preflight.service").read_text()
    commands = [
        shlex.split(line.partition("=")[2])
        for line in unit_text.splitlines()
        if line.startswith("ExecStartPre=")
    ]
    assert commands, "unit must declare a pre-start config validator"
    for argv in commands:
        if any("preflight_config.py" in arg for arg in argv):
            argv[0] = sys.executable
            argv[1] = str(ROOT / "preflight_config.py")
        argv[:] = [
            str(config_path) if arg == "/etc/mctomqtt/config.toml" else arg
            for arg in argv
        ]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        if result.returncode:
            return False, result.stdout + result.stderr
    return True, ""


def test_preflight_rejects_transport_and_address_outside_canonical_sections(tmp_path):
    wrong_section_transport = '''[general]
transport = "serial"
[serial]
transport = "ble"
[other]
address = "02:00:00:00:00:01"
'''
    wrong_section_address = '''[general]
transport = "ble"
[other]
address = "02:00:00:00:00:01"
'''
    accepted_configs = []
    for name, config in (
        ("misplaced-transport.toml", wrong_section_transport),
        ("misplaced-address.toml", wrong_section_address),
    ):
        accepted, output = _run_declared_observer_preflight(config, tmp_path / name)
        if accepted:
            accepted_configs.append(name)
    assert not accepted_configs, f"unsafe misplaced configs accepted: {accepted_configs}"


def _run_config_validator(config_text, config_path):
    config_path.write_text(config_text)
    return subprocess.run(
        [sys.executable, str(ROOT / "preflight_config.py"), "--config", str(config_path)],
        capture_output=True, text=True, timeout=10,
    )


def _enabled_broker_config(extra):
    return f'''[general]
transport = "ble"
iata = "SEA"
[ble]
address = "02:00:00:00:00:01"
[[broker]]
name = "offline-fixture"
enabled = true
server = "mqtt.example.invalid"
{extra}
'''


def _runtime_effective_iata(config_text):
    import tomllib

    from bridge.state import BridgeState
    from bridge.topics import resolve_topic_template

    state = BridgeState(tomllib.loads(config_text))
    return resolve_topic_template(state, "{IATA}", 0)


def test_config_validator_accepts_disabled_placeholders_only_with_valid_explicit_ble(tmp_path):
    config = (ROOT / "config.ble-preflight.example.toml").read_text().replace(
        "REPLACE_WITH_CONFIRMED_BONDED_BLE_ADDRESS", "02:00:00:00:00:01"
    )
    result = _run_config_validator(config, tmp_path / "explicit.toml")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("config", [
    '[general]\ntransport="serial"\n[ble]\naddress="02:00:00:00:00:01"\n',
    '[serial]\ntransport="ble"\n[ble]\naddress="02:00:00:00:00:01"\n',
    '[general]\ntransport="ble"\n[other]\naddress="02:00:00:00:00:01"\n',
    '[general]\ntransport="ble"\n[ble]\naddress="REPLACE_WITH_ADDRESS"\n',
    '[general]\ntransport="ble"\n[ble]\naddress="00:00:00:00:00:00"\n',
    'transport="bogus"\n[general]\ntransport="ble"\n[ble]\naddress="02:00:00:00:00:01"\n',
    '[general]\ntransport="ble"\n[ble]\naddress="02:00:00:00:00:01"\n[[broker]]\nenabled=true\nserver="REPLACE_WITH_ENDPOINT"\n',
    '[general\ntransport="ble"\n',
])
def test_config_validator_rejects_bad_or_misleading_explicit_config(tmp_path, config):
    result = _run_config_validator(config, tmp_path / "invalid.toml")
    assert result.returncode != 0
    assert "preflight" in (result.stdout + result.stderr).lower()


def test_config_validator_rejects_missing_and_empty_config(tmp_path):
    helper = ROOT / "preflight_config.py"
    missing = subprocess.run(
        [sys.executable, str(helper), "--config", str(tmp_path / "missing.toml")],
        capture_output=True, text=True, timeout=10,
    )
    assert missing.returncode != 0
    empty_path = tmp_path / "empty.toml"
    empty_path.write_text("")
    empty = subprocess.run(
        [sys.executable, str(helper), "--config", str(empty_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert empty.returncode != 0


def test_config_validator_preserves_runtime_canonical_transport_precedence(tmp_path):
    config = (ROOT / "config.ble-preflight.example.toml").read_text().replace(
        "REPLACE_WITH_CONFIRMED_BONDED_BLE_ADDRESS", "02:00:00:00:00:01"
    )
    config = 'transport = "serial"\n\n' + config
    result = _run_config_validator(config, tmp_path / "canonical-precedence.toml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_config_validator_rejects_enabled_broker_with_placeholder_iata(tmp_path):
    config = '''[general]
transport = "ble"
iata = "XXX"
[ble]
address = "02:00:00:00:00:01"
[[broker]]
name = "explicit-example"
enabled = true
server = "mqtt.example.invalid"
[broker.auth]
method = "token"
audience = "mqtt.example.invalid"
'''
    result = _run_config_validator(config, tmp_path / "placeholder-region.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


def test_config_validator_uses_general_iata_when_root_broker_iata_is_ignored(tmp_path):
    config = '''[general]
transport = "ble"
iata = "XXX"
[ble]
address = "02:00:00:00:00:01"
[[broker]]
name = "explicit-example"
enabled = true
iata = "SEA"
server = "mqtt.example.invalid"
[broker.auth]
method = "token"
    audience = "mqtt.example.invalid"
'''
    assert _runtime_effective_iata(config) == "XXX"
    result = _run_config_validator(config, tmp_path / "wrong-root-iata.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


def test_config_validator_rejects_effective_nested_iata_placeholder(tmp_path):
    config = '''[general]
transport = "ble"
iata = "SEA"
[ble]
address = "02:00:00:00:00:01"
[[broker]]
name = "explicit-example"
enabled = true
server = "mqtt.example.invalid"
[broker.topics]
iata = "XXX"
[broker.auth]
method = "token"
    audience = "mqtt.example.invalid"
'''
    assert _runtime_effective_iata(config) == "XXX"
    result = _run_config_validator(config, tmp_path / "nested-placeholder-iata.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


def test_config_validator_accepts_valid_nested_runtime_iata_override(tmp_path):
    config = '''[general]
transport = "ble"
iata = "XXX"
[ble]
address = "02:00:00:00:00:01"
[[broker]]
name = "explicit-example"
enabled = true
server = "mqtt.example.invalid"
[broker.topics]
iata = "SEA"
[broker.auth]
method = "token"
    audience = "mqtt.example.invalid"
'''
    assert _runtime_effective_iata(config) == "SEA"
    result = _run_config_validator(config, tmp_path / "nested-valid-iata.toml")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("method", ["SENTINEL-UNKNOWN-AUTH", "1", "false"])
def test_enabled_broker_rejects_unsupported_auth_method_without_echoing_it(tmp_path, method):
    config = _enabled_broker_config(f'[broker.auth]\nmethod = "{method}"\n')
    result = _run_config_validator(config, tmp_path / "unknown-auth.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()
    assert method not in result.stdout + result.stderr


@pytest.mark.parametrize("audience", [None, ""])
def test_enabled_token_broker_requires_nonempty_audience(tmp_path, audience):
    audience_line = "" if audience is None else f'audience = "{audience}"\n'
    config = _enabled_broker_config(f'[broker.auth]\nmethod = "token"\n{audience_line}')
    result = _run_config_validator(config, tmp_path / "missing-token-audience.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


@pytest.mark.parametrize("credentials", [
    'username = "observer"\n',
    'password = "fixture-only"\n',
    'username = ""\npassword = "fixture-only"\n',
    'username = "observer"\npassword = ""\n',
])
def test_enabled_password_broker_requires_nonempty_username_and_password(tmp_path, credentials):
    config = _enabled_broker_config(f'[broker.auth]\nmethod = "password"\n{credentials}')
    result = _run_config_validator(config, tmp_path / "missing-password-credentials.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


@pytest.mark.parametrize("tls_block", [
    'tls = "false"\n',
    '[broker.tls]\nenabled = "false"\n[broker.auth]\nmethod = "none"\n',
    '[broker.tls]\nverify = 1\n[broker.auth]\nmethod = "none"\n',
])
def test_enabled_broker_rejects_malformed_tls_flag_types(tmp_path, tls_block):
    config = _enabled_broker_config(tls_block)
    result = _run_config_validator(config, tmp_path / "malformed-tls.toml")
    assert result.returncode != 0
    assert "observer preflight" in result.stderr.lower()


def test_enabled_broker_accepts_explicit_none_auth(tmp_path):
    config = _enabled_broker_config('[broker.auth]\nmethod = "none"\n')
    result = _run_config_validator(config, tmp_path / "explicit-none.toml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_enabled_broker_preserves_explicit_tls_verification_override(tmp_path):
    config = _enabled_broker_config(
        '[broker.auth]\nmethod = "none"\n[broker.tls]\nenabled = true\nverify = false\n'
    )
    result = _run_config_validator(config, tmp_path / "explicit-tls-override.toml")
    assert result.returncode == 0, result.stdout + result.stderr


def test_sigterm_cleanup_and_transport_reopen_without_services(caplog):
    """Catch broken cleanup/reopen, not pretend to exercise systemd itself."""
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    state = make_test_state(device=connection)
    with caplog.at_level(logging.DEBUG):
        handle_signal(state, 15, None)
        _cleanup(state, threading.Thread())
    assert state.should_exit
    assert not connection.is_open
    assert not fake.is_connected
    assert not fake.callbacks
    connection.open()
    assert connection.is_open
    connection.close()
    assert not connection.is_open


@pytest.fixture
def staged_identity(tmp_path):
    # Resolve deployment principals at the OS boundary to this unprivileged
    # fixture's real uid/gid. Do not suppress chown: apply it to every real file,
    # and separately verify the exact requested production principals/paths.
    if not sys.platform.startswith("linux") or os.getuid() == 0:
        pytest.skip("requires an unprivileged Linux filesystem fixture")
    uid, gid = os.getuid(), os.getgid()
    svc = service()["Service"]
    assert svc["User"] == svc["Group"] == "mctomqtt"
    changes = []
    real_chown = shutil.chown

    def fixture_chown(path, user, group):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        assert user in ("root", svc["User"]) and group == svc["Group"]
        changes.append((Path(path), user, group))
        real_chown(path, uid, gid)

    return svc, uid, gid, changes, fixture_chown


@pytest.mark.parametrize("transport", ["serial", "ble"])
def test_staged_config_is_private_and_uses_unit_service_identity(tmp_path, staged_identity, transport):
    from config_loader import load_config
    from installer import system

    svc, uid, gid, changes, change_owner = staged_identity
    install = tmp_path / "opt/mctomqtt"
    config = tmp_path / "etc/mctomqtt"
    install.mkdir(parents=True)
    (config / "config.d").mkdir(parents=True)
    app = install / "mctomqtt.py"
    app.write_text("# fixture application\n")
    app.chmod(0o755)
    files = [config / "config.toml", config / "config.d/10-broker.toml",
             config / "config.d/99-user.toml"]
    files[0].write_text(f'[general]\ntransport = "{transport}"\n')
    for path in files[1:]:
        path.write_text('password = "fixture-only"\n')
        path.chmod(0o666)  # upgrades must repair old unsafe modes too
    contents = {path: path.read_bytes() for path in files}

    system.set_permissions(str(install), str(config), svc["User"], change_owner=change_owner)

    for path in (config, config / "config.d"):
        assert path.stat().st_mode & 0o777 == 0o750
    for path in files:
        assert path.stat().st_mode & 0o777 == 0o640
        assert path.read_bytes() == contents[path]
    for path in (config, config / "config.d", *files):
        assert (path, "root", svc["Group"]) in changes
        assert (path.stat().st_uid, path.stat().st_gid) == (uid, gid)
    assert (app, svc["User"], svc["Group"]) in changes
    assert app.stat().st_mode & 0o777 == 0o755  # preserve serial install executable
    assert load_config([str(files[0])])["general"]["transport"] == transport


def test_broker_config_edit_does_not_reopen_private_override(tmp_path, staged_identity, monkeypatch):
    from installer import InstallerContext, config

    svc, uid, gid, changes, change_owner = staged_identity
    config_dir = tmp_path / "etc/mctomqtt"
    config_d = config_dir / "config.d"
    config_d.mkdir(parents=True)
    secret = config_d / "99-user.toml"
    secret.write_text('password = "fixture-only"\n')
    secret.chmod(0o640)
    # Existing config + immediate Finish exercises only local file operations:
    # no discovery, broker preset downloads, serial detection or installer CLI.
    monkeypatch.setattr(config, "prompt_input", lambda *_args: "5")
    config.configure_mqtt_brokers(InstallerContext(config_dir=str(config_dir), svc_user=svc["User"]),
                                  change_owner=change_owner)
    assert secret.stat().st_mode & 0o777 == 0o640
    assert (secret.stat().st_uid, secret.stat().st_gid) == (uid, gid)
    assert (secret, "root", svc["Group"]) in changes


def test_entrypoint_sigterm_restart_cleans_up_and_redacts_output(tmp_path):
    """Run real entrypoint/runner in two fresh unprivileged child processes."""
    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("requires an unprivileged POSIX subprocess fixture")
    config = tmp_path / "fixture.toml"
    config.write_text('''[general]
transport = "ble"
sync_time = false
log_level = "DEBUG"
iata = "TST"
[topics]
status = "meshcore/{IATA}/{PUBLIC_KEY}/status"
[ble]
address = "02:00:00:00:00:01"
[remote_serial]
enabled = false
[[broker]]
name = "offline-fixture"
enabled = true
server = "fixture.invalid"
[broker.auth]
method = "password"
password = "SENTINEL-password-not-for-logs"
''')
    config.chmod(0o640)
    pids = []
    for attempt in range(2):
        events = tmp_path / f"events-{attempt}.txt"
        output = tmp_path / f"output-{attempt}.txt"
        with output.open("w") as captured:
            process = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "tests/helpers/offline_service.py"),
                 str(config), str(events)],
                cwd=tmp_path, stdout=captured, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                pids.append(process.pid)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if events.exists() and "ready\n" in events.read_text():
                        break
                    assert process.poll() is None, output.read_text()
                    time.sleep(0.02)
                else:
                    pytest.fail(f"child did not become ready: {output.read_text()}")
                process.terminate()  # real SIGTERM to the real entrypoint handler
                assert process.wait(timeout=15) == 0, output.read_text()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        assert events.read_text().splitlines() == [
            f"identity={os.getuid()}:{os.getgid()}", "transport-selected=ble", "ready", "offline-status",
            "broker-loop-stopped", "broker-disconnected", "transport-closed", "exited",
        ]
        text = output.read_text()
        assert "Transport: ble" in text
        assert "Received signal 15" in text and "Cleaning up..." in text
        assert "Traceback" not in text and "forbidden external I/O" not in text
        for secret in ("SENTINEL-password-not-for-logs", "cd" * 64, "SENTINEL-token-not-for-logs"):
            assert secret not in text
    assert pids[0] != pids[1]


def test_startup_unavailable_clean_exit_is_covered_by_unit_restart_policy(tmp_path):
    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("requires an unprivileged POSIX subprocess fixture")
    config = tmp_path / "unavailable.toml"
    config.write_text('[general]\ntransport = "ble"\nsync_time = false\n')
    events = tmp_path / "events"
    result = subprocess.run(
        [sys.executable, "-u", str(ROOT / "tests/helpers/offline_service.py"),
         str(config), str(events)],
        env={**os.environ, "MCTOMQTT_FIXTURE_UNAVAILABLE": "1"},
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text().splitlines()[-2:] == ["transport-selected=ble", "exited"]
    assert "forbidden external I/O" not in result.stderr
    # Runner intentionally returns normally on unavailable startup. on-failure
    # cannot recover this exit. Explicit supervisor stop is not a restart event.
    assert service()["Service"]["Restart"] == "always"
    assert service()["Service"]["KillSignal"] == "SIGTERM"
