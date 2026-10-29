"""Transport diagnostics distinguish failures without exporting device material."""
import asyncio
import json
import logging
import re

import pytest

from bridge.ble_connection import (
    BleConnection, COMMAND_CHARACTERISTIC_UUID, STATS_CHARACTERISTIC_UUID,
)
from test_ble_connection import FakeGatt, frame


SENSITIVE = "DO_NOT_LOG_device_reply_email_key_backend_payload"


@pytest.fixture
def connections():
    BleConnection._process_sessions.clear()
    opened = []

    def create(fake):
        connection = BleConnection("02:00:00:00:00:01", client_factory=lambda _: fake)
        opened.append(connection)
        connection.open()
        return connection

    yield create
    for connection in reversed(opened):
        connection.close()
    BleConnection._process_sessions.clear()


class FailedWriteGatt(FakeGatt):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    async def write_gatt_char(self, uuid, value, response=True):
        await super().write_gatt_char(uuid, value, response)
        if uuid == COMMAND_CHARACTERISTIC_UUID:
            if self.mode == "write-timeout":
                await asyncio.Event().wait()
            raise RuntimeError(SENSITIVE)


@pytest.mark.parametrize("stage", ["reply-timeout", "write-timeout", "write-error"])
def test_command_failures_report_stage_and_counters_without_changing_link_retirement(
    stage, connections, caplog,
):
    fake = FakeGatt() if stage == "reply-timeout" else FailedWriteGatt(stage)
    connection = connections(fake)
    connection.metrics["acked_records"] = 7
    connection.metrics["ack_failures"] = 2

    with caplog.at_level(logging.WARNING):
        success, _ = connection.execute_command("stats-core", timeout=0.02)

    assert not success
    assert not connection.is_open
    failures = [r.getMessage() for r in caplog.records if "BLE observer diagnostic:" in r.getMessage()]
    assert len(failures) == 1
    message = failures[0]
    assert "command=stats-core" in message
    assert f"stage={stage}" in message
    assert "acked=7" in message
    assert "ack_failures=2" in message
    assert "desynced=1" in message
    assert re.search(r"elapsed_ms=\d+", message)
    assert SENSITIVE not in caplog.text
    assert connection.address not in caplog.text


@pytest.mark.parametrize("command, category", [
    ("get prv.key", "device-query"),
    ("time 1790000000", "clock"),
])
def test_timeout_logs_only_safe_command_category(command, category, connections, caplog):
    connection = connections(FakeGatt())
    with caplog.at_level(logging.WARNING):
        success, _ = connection.execute_command(command, timeout=0.01)
    assert not success
    assert f"command={category}" in caplog.text
    assert "stage=reply-timeout" in caplog.text
    assert command not in caplog.text
    assert connection.address not in caplog.text


class FailedCounterReadGatt(FakeGatt):
    def __init__(self):
        super().__init__()
        self.sequence = 0

    async def write_gatt_char(self, uuid, value, response=True):
        await super().write_gatt_char(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        payloads = {
            b"stats-core": {"battery_mv": 3900, "uptime_secs": 400000, "errors": 0, "queue_len": 0},
            b"stats-radio": {"noise_floor": -116, "tx_air_secs": 3, "rx_air_secs": 4},
            b"stats-packets": {"recv_errors": 0},
        }
        self.sequence += 1
        payload = (json.dumps(payloads[value]) + "\r\n").encode()
        self.emit(frame(self.sequence, len(payload), 0, payload))

    async def read_gatt_char(self, uuid):
        assert uuid == STATS_CHARACTERISTIC_UUID
        raise RuntimeError(SENSITIVE)


def test_counter_read_error_reports_numeric_failure_without_dropping_usable_link(
    connections, caplog,
):
    connection = connections(FailedCounterReadGatt())
    with caplog.at_level(logging.WARNING):
        stats = connection.get_device_stats()
    assert stats["uptime_secs"] == 400000
    assert stats["observer_host"]["stats_read_failures"] == 1
    assert connection.is_open
    assert "command=observer-counters" in caplog.text
    assert "stage=stats-read-error" in caplog.text
    assert "stats_read_failures=1" in caplog.text
    assert SENSITIVE not in caplog.text
    assert connection.address not in caplog.text


def test_successful_open_reports_new_connection_without_peer_identifier(connections, caplog):
    with caplog.at_level(logging.INFO):
        connection = connections(FakeGatt())
    assert connection.is_open
    assert "BLE observer connected: generation=1" in caplog.text
    assert connection.address not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
