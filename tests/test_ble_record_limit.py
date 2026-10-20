"""Coordinated lines544 ceiling through real reassembly/ACK/dedup paths.

The lines544 firmware series extends the earlier MTU series' 256-byte ceiling.
These synthetic records exercise the reported 541-byte RAW size without raw
hardware captures; no device or broker access is involved.
"""
from __future__ import annotations

from bridge.ble_connection import ACK_CHARACTERISTIC_UUID, MAX_RECORD_BYTES
from test_ble_connection import (
    FakeGatt,
    close_ble_connections_after_test,  # noqa: F401 (autouse cleanup fixture)
    frame,
    make_connection,
)

CEILING = 544
OBSERVED_MAX_RAW = 541
MTU_PAYLOAD_BUDGET = 228  # 244-byte notification minus 16-byte header


def test_coordinated_record_ceiling_is_544_bytes():
    assert MAX_RECORD_BYTES == CEILING
    assert OBSERVED_MAX_RAW < CEILING


def test_observed_max_raw_record_reassembles_from_four_fragments_acks_once_and_deduplicates_replay():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    record = b"RAW: " + b"A" * 535 + b"\n"
    assert len(record) == OBSERVED_MAX_RAW
    sizes = [MTU_PAYLOAD_BUDGET, 136, 136, 41]
    offset = 0
    for size in sizes[:-1]:
        fake.emit(frame(5, len(record), offset, record[offset:offset + size]))
        offset += size
        assert connection.read_line() is None
        assert fake.writes == []
    fake.emit(frame(5, len(record), offset, record[offset:]))
    assert connection.read_line() == record[:-1].decode("ascii")
    assert connection.read_line() is None
    assert fake.writes == [(ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 5")]
    assert connection.metrics["acked_records"] == 1
    assert connection.metrics["malformed_frames"] == 0
    assert connection.metrics["reordered_frames"] == 0
    offset = 0
    for size in sizes:
        fake.emit(frame(5, len(record), offset, record[offset:offset + size]))
        offset += size
    assert connection.read_line() is None
    assert connection.metrics["acked_records"] == 2
    assert connection.metrics["duplicate_records"] == 1
    assert connection.metrics["malformed_frames"] == 0


def test_observed_max_raw_record_reassembles_from_4_byte_fragments_like_the_live_retry_trace():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    record = b"RAW: " + b"A" * 535 + b"\n"
    assert len(record) == OBSERVED_MAX_RAW
    offset = 0
    while offset < len(record) - 4:
        fake.emit(frame(11, len(record), offset, record[offset:offset + 4]))
        offset += 4
    assert connection.read_line() is None
    assert fake.writes == []
    fake.emit(frame(11, len(record), offset, record[offset:]))
    assert connection.read_line() == record[:-1].decode("ascii")
    assert connection.read_line() is None
    assert fake.writes == [(ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 11")]
    assert connection.metrics["acked_records"] == 1
    assert connection.metrics["malformed_frames"] == 0
    assert connection.metrics["reordered_frames"] == 0


def test_ceiling_record_reassembles_from_mtu_sized_fragments_and_acks_only_completion():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    record = b"RAW: " + b"B" * 538 + b"\n"
    assert len(record) == CEILING
    sizes = [MTU_PAYLOAD_BUDGET, MTU_PAYLOAD_BUDGET, 88]
    offset = 0
    for size in sizes[:-1]:
        fake.emit(frame(6, len(record), offset, record[offset:offset + size]))
        offset += size
        assert connection.read_line() is None
        assert fake.writes == []
    fake.emit(frame(6, len(record), offset, record[offset:]))
    assert connection.read_line() == record[:-1].decode("ascii")
    assert connection.read_line() is None
    assert fake.writes == [(ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 6")]
    assert connection.metrics["acked_records"] == 1
    assert connection.metrics["malformed_frames"] == 0
    offset = 0
    for size in sizes:
        fake.emit(frame(6, len(record), offset, record[offset:offset + size]))
        offset += size
    assert connection.read_line() is None
    assert fake.writes == [
        (ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 6"),
        (ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 6"),
    ]
    assert connection.metrics["acked_records"] == 2
    assert connection.metrics["duplicate_records"] == 1
    assert connection.metrics["malformed_frames"] == 0


def test_one_byte_over_ceiling_record_is_malformed_and_never_acked():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    record = b"RAW: " + b"C" * 539 + b"\n"
    assert len(record) == CEILING + 1 == MAX_RECORD_BYTES + 1
    fake.emit(frame(7, len(record), 0, record[:MTU_PAYLOAD_BUDGET]))
    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics["malformed_frames"] == 1
    assert connection.metrics["acked_records"] == 0


def test_legacy_256_byte_boundary_records_still_reassemble_and_ack():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    legacy = b"RAW: " + b"D" * 250 + b"\n"
    assert len(legacy) == 256
    fake.emit(frame(8, len(legacy), 0, legacy[:128]))
    assert connection.read_line() is None
    assert fake.writes == []
    fake.emit(frame(8, len(legacy), 128, legacy[128:]))
    assert connection.read_line() == legacy[:-1].decode("ascii")
    assert fake.writes == [(ACK_CHARACTERISTIC_UUID, b"ack 0123456789abcdef 8")]
    assert connection.metrics["malformed_frames"] == 0
    fake.emit(frame(9, 3, 0, b"OK\n"))
    assert connection.read_line() == "OK"
    assert connection.metrics["malformed_frames"] == 0
