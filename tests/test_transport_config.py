"""Real TOML loading/selection; only device I/O uses offline fakes."""
from pathlib import Path
import logging

import pytest

from bridge import ble_connection, runner
from config_loader import load_config, log_config_sources
from tests.fakes import CallbackBrokerClient, FakeSerialConnection, make_test_state, make_config
from bridge.mqtt_manager import MqttManager
from test_ble_connection import FakeGatt, close_ble_connections_after_test

ROOT = Path(__file__).parents[1]


@pytest.fixture
def documented_ble_config(tmp_path):
    text = (ROOT / "config.toml.example").read_text()
    text = text.replace('transport = "serial"', 'transport = "ble"', 1)
    text = text.replace(
        '# address = "REPLACE_WITH_CONFIRMED_BONDED_BLE_ADDRESS"',
        'address = "02:00:00:00:00:01"',
        1,
    )
    text = text.replace('# watchdog_timeout = 900', 'watchdog_timeout = 7', 1)
    text = text.replace('sync_time = true', 'sync_time = false', 1)
    path = tmp_path / "documented-ble.toml"
    path.write_text(text)
    return load_config([str(path)])


def test_documented_toml_opens_real_ble_connection_with_fake_gatt(documented_ble_config):
    fake = FakeGatt()
    addresses = []

    def client(address):
        addresses.append(address)
        return fake

    connection = runner.connect_transport(
        documented_ble_config,
        serial_factory=lambda _: pytest.fail("serial I/O forbidden"),
        ble_factory=lambda config: ble_connection.connect(config, client_factory=client),
    )
    try:
        assert isinstance(connection, ble_connection.BleConnection)
        assert connection.is_open and fake.is_connected
        assert addresses == ["02:00:00:00:00:01"]
    finally:
        if connection is not None:
            connection.close()


@pytest.mark.parametrize("toml, expected", [
    ("", "serial"),
    ('transport = "ble"\n', "ble"),
    ('[general]\ntransport = "ble"\n', "ble"),
    ('transport = "serial"\n[general]\ntransport = "ble"\n', "ble"),
    ('transport = "ble"\n[general]\ntransport = "serial"\n', "serial"),
])
def test_toml_selection_and_summary_agree_with_canonical_precedence(tmp_path, caplog, toml, expected):
    path = tmp_path / "config.toml"
    path.write_text(toml + '\n[ble]\naddress = "02:00:00:00:00:01"\n')
    config = load_config([str(path)])
    fake = FakeGatt()
    serial = FakeSerialConnection()
    connection = runner.connect_transport(
        config, serial_factory=lambda _: serial,
        ble_factory=lambda cfg: ble_connection.connect(cfg, client_factory=lambda _: fake),
    )
    try:
        if expected == "ble":
            assert isinstance(connection, ble_connection.BleConnection)
            assert fake.is_connected
        else:
            assert connection is serial
            assert not fake.is_connected
        with caplog.at_level(logging.INFO):
            log_config_sources(config)
        assert f"Transport: {expected}" in caplog.text
    finally:
        if connection is not None:
            connection.close()


@pytest.mark.parametrize("toml", [
    '[general]\ntransport = "auto"',
    '[general]\ntransport = "BLE"',
    '[general]\ntransport = ""',
    '[general]\ntransport = false',
    '[general]\ntransport = 42',
    '[general]\ntransport = ["ble"]',
    'general = "ble"',
    'transport = "auto"',
    'transport = false',
    # Even a shadowed legacy value must be valid, rather than hiding a typo.
    'transport = "auto"\n[general]\ntransport = "ble"',
    'transport = "ble"\n[general]\ntransport = false',
])
def test_invalid_transport_never_calls_either_device_factory(tmp_path, caplog, toml):
    path = tmp_path / "invalid.toml"
    path.write_text(toml)
    config = load_config([str(path)])

    def forbidden(_config):
        pytest.fail("invalid config reached device I/O")

    with caplog.at_level(logging.ERROR):
        assert runner.connect_transport(config, serial_factory=forbidden, ble_factory=forbidden) is None
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_runner_watchdog_uses_canonical_ble_timeout_and_reconnect(tmp_path, documented_ble_config, caplog):
    # Load both actual TOML layers. Even a conflicting legacy key must not make
    # the watchdog read serial's 900-second timeout instead of BLE's 7 seconds.
    legacy = tmp_path / "legacy.toml"
    legacy.write_text('transport = "serial"\n')
    config = load_config([str(tmp_path / "documented-ble.toml"), str(legacy)])
    config["broker"] = make_config()["broker"]
    state = make_test_state(config=config)
    devices = []

    class IdleTransport(FakeSerialConnection):
        reads = 0

        def read_line(self):
            self.reads += 1
            if self.reads >= 2:
                runner.handle_signal(state, 15, None)
            return None

        def seconds_since_activity(self):
            return 10

    def connect_ble(_config):
        device = IdleTransport()
        devices.append(device)
        if len(devices) == 2:
            state.should_exit = True
        return device

    state.mqtt_manager = MqttManager(state, client_factory=CallbackBrokerClient)

    def factory(config):
        return runner.connect_transport(
            config, ble_factory=connect_ble,
            serial_factory=lambda _: pytest.fail("serial I/O forbidden"),
        )

    with caplog.at_level(logging.INFO):
        runner.run(state, transport_factory=factory)
    assert len(devices) == 2
    assert all(not device.is_open for device in devices)
    assert "BLE watchdog: no data received for 10s (threshold: 7s), forcing reconnect" in caplog.text
    assert "BLE watchdog: reconnected successfully" in caplog.text
