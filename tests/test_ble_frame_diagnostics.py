"""Separate framing failure mechanisms without relaxing complete-record ACKs."""
import logging
import struct

import pytest

from test_ble_connection import FakeGatt, frame
from test_ble_diagnostics import connections, FailedCounterReadGatt, SENSITIVE


@pytest.mark.parametrize("notification, reason", [
    (b"short", "reject_short_header"),
    (struct.pack("<QIHH", 0, 1, 1, 0) + b"x", "reject_invalid_identity"),
    (frame(0, 1, 0, b"x"), "reject_invalid_identity"),
    (frame(1, 0, 0, b"x"), "reject_invalid_total"),
    (frame(1, 545, 0, b"x"), "reject_invalid_total"),
    (frame(1, 1, 0, b""), "reject_empty_payload"),
    (frame(1, 1, 0, b"xx"), "reject_fragment_overrun"),
    (frame(1, 4, 1, b"xxx"), "reject_missing_start"),
])
def test_rejected_notification_has_one_specific_reason_and_no_ack(
    notification, reason, connections,
):
    fake = FakeGatt()
    connection = connections(fake)
    fake.emit(notification)
    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics["malformed_frames"] == 1
    assert connection.metrics[reason] == 1
    assert sum(v for k, v in connection.metrics.items() if k.startswith("reject_")) == 1


@pytest.mark.parametrize("second, reason, aggregate", [
    (frame(2, 8, 4, b"tail"), "reject_record_switch", "malformed_frames"),
    (frame(1, 7, 0, b"new"), "reject_length_change", "reordered_frames"),
    (frame(1, 8, 5, b"end"), "reject_offset_mismatch", "reordered_frames"),
])
def test_rejected_continuation_distinguishes_switch_length_and_offset(
    second, reason, aggregate, connections,
):
    fake = FakeGatt()
    connection = connections(fake)
    fake.emit(frame(1, 8, 0, b"head"))
    fake.emit(second)
    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics[reason] == 1
    assert connection.metrics[aggregate] == 1
    assert connection.metrics["dropped_records"] == 1


def test_lost_start_then_full_retry_recovers_without_acking_partial_record(connections):
    fake = FakeGatt()
    connection = connections(fake)
    fake.emit(frame(1, 8, 4, b"end\n"))
    assert fake.writes == []
    fake.emit(frame(1, 8, 0, b"new!"))
    fake.emit(frame(1, 8, 4, b"end\n"))
    assert connection.read_line() == "new!end"
    assert len(fake.writes) == 1
    assert connection.metrics["reject_missing_start"] == 1
    assert connection.metrics["acked_records"] == 1


def test_sizes_are_counted_without_logging_each_notification_or_payload(connections, caplog):
    fake = FakeGatt()
    connection = connections(fake)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        fake.emit(b"bad")
        fake.emit(frame(1, 4, 0, b"abc\n"))       # 20 bytes
        fake.emit(frame(2, 228, 0, b"x" * 228))   # 244 bytes
        fake.emit(frame(3, 229, 0, b"x" * 229))   # 245 bytes
        fake.emit(frame(4, len(SENSITIVE), 0, SENSITIVE.encode()))
        assert caplog.records == []
        connection.execute_command("stats-core", timeout=0.01)

    assert connection.metrics["notifications"] == 5
    assert connection.metrics["notify_bytes_min"] == 3
    assert connection.metrics["notify_bytes_max"] == 245
    assert connection.metrics["notify_lt16"] == 1
    assert connection.metrics["notify_16_20"] == 1
    assert connection.metrics["notify_21_244"] == 2
    assert connection.metrics["notify_gt244"] == 1
    assert "BLE observer framing:" in caplog.text
    assert "reject_short_header=1" in caplog.text
    assert "notify_bytes_max=245" in caplog.text
    assert SENSITIVE not in caplog.text
    assert connection.address not in caplog.text


class HealthyStatsGatt(FailedCounterReadGatt):
    async def read_gatt_char(self, uuid):
        return b"evicted=0/0 truncated=0/0 failed=0"


def test_successful_stats_poll_emits_one_framing_summary_and_preserves_stats(connections, caplog):
    connection = connections(HealthyStatsGatt())
    caplog.clear()
    with caplog.at_level(logging.INFO):
        stats = connection.get_device_stats()
    assert stats["uptime_secs"] == 400000
    assert stats["observer_host"]["notifications"] == 3
    assert connection.is_open
    assert sum("BLE observer framing:" in r.getMessage() for r in caplog.records) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_retired_generation_cannot_pollute_current_frame_counters(connections):
    connection = connections(FakeGatt())
    connection._on_notification(None, bytearray(b"bad"), generation=0)
    assert connection.metrics["notifications"] == 0
    assert connection.metrics["reject_short_header"] == 0


@pytest.mark.parametrize("size, bucket", [
    (0, "notify_lt16"), (16, "notify_16_20"), (20, "notify_16_20"),
    (21, "notify_21_244"), (244, "notify_21_244"), (245, "notify_gt244"),
])
def test_notification_size_boundaries_are_reported_without_payloads(size, bucket, connections, caplog):
    connection = connections(FakeGatt())
    connection._on_notification(None, bytearray(b"x" * size))
    with caplog.at_level(logging.WARNING):
        connection.execute_command("stats-core", timeout=0.01)
    assert connection.metrics["notifications"] == 1
    assert connection.metrics[bucket] == 1
    assert connection.metrics["notify_bytes_min"] == size
    assert connection.metrics["notify_bytes_max"] == size
    assert f"notify_bytes_min={size}" in caplog.text


def test_unintended_att_resplitting_is_distinguishable_from_missing_start(connections):
    """One observer frame split below its header boundary is not valid framing."""
    fake = FakeGatt()
    connection = connections(fake)
    encoded = frame(1, 228, 0, b"x" * 228)  # 244-byte observer frame
    for start in range(0, len(encoded), 20):
        fake.emit(encoded[start:start + 20])

    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics["notifications"] == 13
    assert connection.metrics["notify_16_20"] == 12
    assert connection.metrics["notify_lt16"] == 1
    assert connection.metrics["reject_invalid_total"] == 11
    assert connection.metrics["reject_short_header"] == 1
    assert connection.metrics["reject_missing_start"] == 0
    assert connection.metrics["incomplete_records"] == 1
