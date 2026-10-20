"""Offline wire/replay tests: only GATT and brokers are faked."""
import asyncio
import json
import logging
import os
import struct
import subprocess
from pathlib import Path

import pytest

from bridge import runner
from bridge.ble_connection import COMMAND_CHARACTERISTIC_UUID, MAX_RECORD_BYTES
from tests.fakes import FakeBrokerClient, make_config, make_test_state
from test_ble_connection import FakeGatt, make_connection, close_ble_connections_after_test

ACK = "10c25d4a-3c61-50a3-5e48-566b056f5f1b"
STATS = "10c25d4a-3c61-50a3-5e48-566b066f5f1b"
RAW = b"12:00:00 - 1/1/2026 U RAW: AABB\n"
RX = b"12:00:00 - 1/1/2026 U: RX, len=2 (type=1, route=D, payload_len=0) SNR=4 RSSI=-90 score=100 time=2 hash=ABCD [02 -> 01]\n"


def _firmware_source_root():
    configured = os.environ.get("OBSERVER_SOURCE")
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[3]
    headers = ("ObserverProtocol.h", "ObserverRecordQueue.h", "ObserverTransport.h")
    if not all((root / "src/observer" / name).is_file() for name in headers):
        if configured:
            raise AssertionError("OBSERVER_SOURCE must contain the exact patched firmware observer headers")
        pytest.skip("Cross-layer checks require exact patched firmware via OBSERVER_SOURCE")
    return root


def test_explicit_firmware_source_requires_observer_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("OBSERVER_SOURCE", str(tmp_path))
    with pytest.raises(AssertionError, match="OBSERVER_SOURCE"):
        _firmware_source_root()


def test_standalone_bridge_marks_missing_cross_layer_fixture_as_skip(monkeypatch, tmp_path):
    monkeypatch.delenv("OBSERVER_SOURCE", raising=False)
    monkeypatch.setitem(globals(), "__file__", str(tmp_path / "host/tests/test_wire.py"))
    with pytest.raises(pytest.skip.Exception):
        _firmware_source_root()


def wire(nonce, sequence, data, offset=0, total=None):
    return struct.pack("<QIHH", nonce, sequence, len(data) if total is None else total, offset) + data


def drain(connection):
    async def settle():
        for _ in range(10):
            await asyncio.sleep(0)
    connection._run(settle(), timeout=1)


def test_complete_record_ack_exact_bytes_and_lost_ack_replay_single_publish(monkeypatch):
    events = []
    original_parse = runner.message_parser.parse_and_publish
    def observed_parse(state, line):
        events.append(("parse",))
        return original_parse(state, line)
    monkeypatch.setattr(runner.message_parser, "parse_and_publish", observed_parse)
    class Gatt(FakeGatt):
        async def write_gatt_char(self, uuid, value, response=True):
            await super().write_gatt_char(uuid, value, response)
            events.append(("ack", value))
    class Broker(FakeBrokerClient):
        def publish(self, *args, **kwargs):
            events.append(("publish",))
            return super().publish(*args, **kwargs)
    fake = Gatt()
    connection = make_connection(fake)
    connection.open()
    broker = Broker()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    fake.emit(wire(0xFEDCBA9876543210, 4294967295, RX[:4], total=len(RX)))
    assert connection.read_line() is None
    assert events == []
    for offset in range(4, len(RX), 4):
        fake.emit(wire(0xFEDCBA9876543210, 4294967295, RX[offset:offset+4], offset, len(RX)))
    drain(connection)
    assert runner.process_device_line(state)
    # Simulate successful GATT ACK lost before firmware processes its ingress.
    fake.emit(wire(0xFEDCBA9876543210, 4294967295, RX))
    drain(connection)
    assert not runner.process_device_line(state)
    assert events == [("ack", b"ack fedcba9876543210 4294967295"), ("parse",), ("publish",), ("ack", b"ack fedcba9876543210 4294967295")]
    assert len(broker.published) == 1
    assert connection.metrics["duplicate_records"] == 1


@pytest.mark.parametrize("bad", [b"short", wire(1, 0, b"x"), wire(0, 1, b"x"), wire(1, 1, b""), wire(1, 1, b"xx", total=1), wire(1, 1, b"x", total=MAX_RECORD_BYTES + 1), wire(1, 1, b"x", offset=1, total=2)])
def test_malformed_records_never_ack(bad):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(bad)
    drain(connection)
    assert fake.writes == []
    assert connection.read_line() is None


def test_new_boot_restarts_sequence_but_stale_fragments_cannot_restore_old_boot():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    for nonce, seq, data in [(1, 9, b"old"), (2, 1, b"new"), (1, 10, b"stale"), (2, 2, b"next")]:
        fake.emit(wire(nonce, seq, data))
    drain(connection)
    assert [connection.read_line() for _ in range(4)] == ["old", "new", "next", None]
    assert [value for _, value in fake.writes] == [b"ack 0000000000000001 9", b"ack 0000000000000002 1", b"ack 0000000000000002 2"]


def test_sequence_gap_clears_pending_raw_in_real_runner_and_parser():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    fake.emit(wire(1, 1, RAW))
    drain(connection)
    runner.process_device_line(state)
    assert state.last_raw == "AABB"
    # Eviction dropped both the summary paired with RAW and the next RAW.
    fake.emit(wire(1, 4, RX))
    drain(connection)
    runner.process_device_line(state)
    assert json.loads(broker.published[0][1])["raw"] is None


@pytest.mark.parametrize("failed_index", [0, 1])
def test_each_broker_failure_is_counted_dropped_after_ack_without_replay(failed_index):
    class FailingBroker(FakeBrokerClient):
        available = False
        def publish(self, *args, **kwargs):
            super().publish(*args, **kwargs)
            return self.available
    brokers = [FakeBrokerClient(), FakeBrokerClient()]
    brokers[failed_index] = FailingBroker()
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    config = make_config(broker=[
        {"name": "MeshMapper", "topics": {"packets": "mapper/SEA/packets"}},
        {"name": "LetsMesh", "topics": {"packets": "letsmesh/EU/packets"}},
    ])
    state = make_test_state(config=config, device=connection, broker_clients=[{"client": b, "broker_idx": i, "connected": True} for i, b in enumerate(brokers)])
    fake.emit(wire(1, 1, RX))
    drain(connection)
    assert fake.writes == [(ACK, b"ack 0000000000000001 1")]
    runner.process_device_line(state)
    assert state.stats["publish_failures"] == 1
    fake.emit(wire(1, 1, RX))
    drain(connection)
    assert not runner.process_device_line(state)
    assert [len(b.published) for b in brokers] == [1, 1]
    assert [b.published[0][0] for b in brokers] == ["mapper/SEA/packets", "letsmesh/EU/packets"]
    brokers[failed_index].available = True
    assert not runner.process_device_line(state)  # Recovery has no retry queue.
    fake.emit(wire(1, 2, RX))
    runner.process_device_line(state)
    assert [len(b.published) for b in brokers] == [2, 2]
    assert state.stats["publish_failures"] == 1
    assert connection.metrics["dropped_records"] == 0


def test_command_reply_requires_ack_without_command_transaction_deadlock():
    class ReplyGatt(FakeGatt):
        async def write_gatt_char(self, uuid, value, response=True):
            await super().write_gatt_char(uuid, value, response)
            if uuid == COMMAND_CHARACTERISTIC_UUID:
                self.emit(wire(1, 1, b"-> 1.17.1 (Build: test)\r\n"))
                await asyncio.sleep(0)
    fake = ReplyGatt()
    connection = make_connection(fake)
    connection.open()
    assert connection.execute_command("ver", timeout=0.5) == (True, "1.17.1 (Build: test)")
    assert fake.writes == [(COMMAND_CHARACTERISTIC_UUID, b"ver"), (ACK, b"ack 0000000000000001 1")]


def test_firmware_stats_are_read_separately_from_host_and_mqtt_loss():
    class StatsGatt(FakeGatt):
        async def read_gatt_char(self, uuid):
            self._record_operation()
            assert uuid == STATS
            return b"evicted=3/400 truncated=2/70 failed=9 unavailable=5/80"
        async def write_gatt_char(self, uuid, value, response=True):
            await super().write_gatt_char(uuid, value, response)
            if uuid == COMMAND_CHARACTERISTIC_UUID:
                replies = {b"stats-core": b'-> {"battery_mv":4200}', b"stats-radio": b'-> {"noise_floor":-100}', b"stats-packets": b'-> {"recv_errors":4}'}
                reply = replies[value]
                self.emit(wire(1, len(self.writes), reply))
    fake = StatsGatt()
    connection = make_connection(fake)
    connection.open()
    stats = connection.get_device_stats()
    assert stats["observer_firmware"] == {"evicted_records": 3, "evicted_bytes": 400, "truncated_records": 2, "truncated_bytes": 70, "failed_notifications": 9, "unavailable_records": 5, "unavailable_bytes": 80}
    assert stats["observer_host"]["malformed_frames"] == 0
    assert stats["recv_errors"] == 4
    assert "publish_failures" not in stats["observer_host"]


def test_truncated_frame_invalidates_partial_record_without_ack():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(wire(1, 1, b"ab", total=4))
    fake.emit(b"short")
    fake.emit(wire(1, 1, b"cd", offset=2, total=4))
    drain(connection)
    assert fake.writes == []
    assert connection.read_line() is None


def test_retired_boot_fragment_does_not_destroy_current_partial_record():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(wire(1, 1, b"old"))
    fake.emit(wire(2, 1, b"new"))
    fake.emit(wire(2, 2, b"ab", total=4))
    fake.emit(wire(1, 2, b"stale"))
    fake.emit(wire(2, 2, b"cd", offset=2, total=4))
    drain(connection)
    assert [connection.read_line() for _ in range(3)] == ["old", "new", "abcd"]


def test_dedup_survives_runner_replacing_connection_and_session_history_is_bounded():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(wire(1, 1, b"line"))
    assert connection.read_line() == "line"
    connection.close()
    replacement_fake = FakeGatt()
    replacement = make_connection(replacement_fake)
    replacement.open()
    replacement_fake.emit(wire(1, 1, b"line"))
    assert replacement.read_line() is None
    assert replacement_fake.writes == [(ACK, b"ack 0000000000000001 1")]
    for nonce in range(2, 66):
        replacement_fake.emit(wire(nonce, 1, b"boot"))
    # Capacity requests a generation-fenced reconnect, not a permanent outage.
    replacement_fake.emit(wire(1, 2, b"stale"))
    assert replacement.metrics["session_capacity_errors"] == 1
    assert len(replacement_fake.writes) == 64
    assert not replacement.is_open
    replacement.close()
    recovered_fake = FakeGatt()
    recovered = make_connection(recovered_fake)
    recovered.open()
    recovered_fake.emit(wire(65, 1, b"recovered"))
    assert recovered.read_line() == "recovered"
    assert recovered_fake.writes == [(ACK, b"ack 0000000000000041 1")]


def test_over_65_actual_boot_reconnects_never_poison_process_history():
    connection = None
    stale_callbacks = []
    for nonce in range(1, 131):
        if connection:
            connection.close()
        fake = FakeGatt()
        connection = make_connection(fake)
        connection.open()
        stale_callbacks.append(fake.callbacks["10c25d4a-3c61-50a3-5e48-566b036f5f1b"])
        fake.emit(wire(nonce, 1, b"boot"))
        assert connection.read_line() == "boot", nonce
        # Even callbacks whose nonce has aged out cannot change the new session.
        if nonce > 1:
            stale_callbacks[0](None, bytearray(wire(1, 2, b"stale")))
        assert connection.read_line() is None
        fake.emit(wire(nonce, 1, b"boot"))
        assert connection.read_line() is None


def test_ack_timeout_keeps_record_unexposed_and_retry_can_deliver_once(monkeypatch, caplog):
    monkeypatch.setattr("bridge.ble_connection.GATT_TIMEOUT", 0.02)
    class SlowAck(FakeGatt):
        stalled = True
        async def write_gatt_char(self, uuid, value, response=True):
            if self.stalled:
                await asyncio.Event().wait()
            await super().write_gatt_char(uuid, value, response)
    fake = SlowAck()
    connection = make_connection(fake)
    connection.open()
    fake.emit(wire(1, 1, b"secret-private-key"))
    assert connection.read_line() is None
    assert connection.metrics["ack_failures"] == 1
    fake.stalled = False
    fake.emit(wire(1, 1, b"secret-private-key"))
    assert connection.read_line() == "secret-private-key"
    fake.emit(wire(1, 1, b"secret-private-key"))
    assert connection.read_line() is None
    assert "secret-private-key" not in caplog.text


@pytest.mark.parametrize("command", [" ver ", "time ١", "time +1", "time 4294967296", "ack 0000000000000001 1", "get prv.key\nver"])
def test_commands_reject_noncanonical_ascii_without_writing(command):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    assert not connection.execute_command(command, timeout=0.01)[0]
    assert fake.writes == []


def test_gatt_writes_are_serialized_even_when_reply_arrives_inside_command_write():
    class ConcurrentGatt(FakeGatt):
        in_flight = 0
        maximum = 0
        async def write_gatt_char(self, uuid, value, response=True):
            self.in_flight += 1
            self.maximum = max(self.maximum, self.in_flight)
            try:
                await super().write_gatt_char(uuid, value, response)
                if uuid == COMMAND_CHARACTERISTIC_UUID:
                    self.emit(wire(1, 1, b"-> 1.17.1 (Build: test)"))
                await asyncio.sleep(0.01)
            finally:
                self.in_flight -= 1
    fake = ConcurrentGatt()
    connection = make_connection(fake)
    connection.open()
    assert connection.execute_command("ver", timeout=0.5)[0]
    assert fake.maximum == 1
    assert len(fake.writes) == 2


def test_raw_gap_is_retained_across_intervening_command_reply_and_queue_eviction():
    fake = FakeGatt()
    connection = make_connection(fake, max_queued_lines=1)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    fake.emit(wire(1, 1, RAW))
    runner.process_device_line(state)
    original_write = fake.write_gatt_char
    async def reply(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid == COMMAND_CHARACTERISTIC_UUID:
            fake.emit(wire(1, 3, b"-> 1.17.1 (Build: test)"))
    fake.write_gatt_char = reply
    assert connection.execute_command("ver", timeout=0.5)[0]
    fake.emit(wire(1, 4, b"ignored log"))
    fake.emit(wire(1, 5, RX))  # Evict the queued line carrying the gap marker.
    runner.process_device_line(state)
    assert json.loads(broker.published[0][1])["raw"] is None
    assert connection.metrics["sequence_gaps"] == 1
    assert connection.metrics["dropped_records"] == 1


def test_contiguous_reconnect_preserves_classic_pending_raw():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    fake.emit(wire(1, 1, RAW))
    runner.process_device_line(state)
    connection.close()
    replacement_fake = FakeGatt()
    state.device = make_connection(replacement_fake)
    state.device.open()
    replacement_fake.emit(wire(1, 2, RX))
    runner.process_device_line(state)
    assert json.loads(broker.published[0][1])["raw"] == "AABB"


def test_replacing_connection_with_unread_records_clears_pending_raw():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    fake.emit(wire(1, 1, RAW))
    runner.process_device_line(state)
    fake.emit(wire(1, 2, RX))  # Accepted, but lost with the old adapter's queue.
    connection.close()
    replacement_fake = FakeGatt()
    state.device = make_connection(replacement_fake)
    state.device.open()
    replacement_fake.emit(wire(1, 3, RX))
    runner.process_device_line(state)
    assert json.loads(broker.published[0][1])["raw"] is None


def test_backend_exception_text_cannot_log_private_material(caplog):
    class ErrorGatt(FakeGatt):
        async def write_gatt_char(self, uuid, value, response=True):
            raise RuntimeError("private-material-must-not-leak")
    fake = ErrorGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(wire(1, 1, b"private-material-must-not-leak"))
    assert connection.read_line() is None
    assert connection.metrics["ack_failures"] == 1
    assert not connection.execute_command("get prv.key", timeout=0.1)[0]
    assert "private-material-must-not-leak" not in caplog.text


def test_literal_firmware_little_endian_header_is_not_native_or_big_endian():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(bytes.fromhex("10 32 54 76 98 ba dc fe 04 03 02 01 04 00 00 00") + b"line")
    assert connection.read_line() == "line"
    assert fake.writes == [(ACK, b"ack fedcba9876543210 16909060")]


def test_private_key_reply_replayed_after_reconnect_cannot_reach_parser_logs(caplog):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    state = make_test_state(device=connection)
    # A queued reply to the previous link's request may precede new setup.
    key = "cd" * 64
    with caplog.at_level(logging.DEBUG):
        fake.emit(wire(1, 1, f"-> >{key}\r\n".encode()))
        runner.process_device_line(state)
    assert key not in caplog.text
    assert fake.writes == [(ACK, b"ack 0000000000000001 1")]


def test_real_firmware_queue_rollover_to_host_has_fresh_identity_and_single_publishes(tmp_path):
    source = tmp_path / "rollover.cpp"
    source.write_text('''
#include <cstdio>
#include <cstring>
#include "observer/ObserverRecordQueue.h"
#include "observer/ObserverProtocol.h"
int main() {
  observer::RecordQueue<512, 4> queue(true, 1, UINT32_MAX);
  const char* line = ''' + json.dumps(RX.decode()) + ''';
  if (!queue.enqueue(reinterpret_cast<const uint8_t*>(line), strlen(line))) return 1;
  queue.setBootNonce(2); // supplied by loop-side fresh S140 entropy
  if (!queue.enqueue(reinterpret_cast<const uint8_t*>(line), strlen(line))) return 2;
  observer::RecordFragment fragment{};
  while (queue.peek(fragment, 4)) {
    uint8_t encoded[20];
    size_t length = observer::encodeFrame(fragment.boot_nonce, fragment.sequence,
      fragment.total_length, fragment.offset, fragment.bytes, fragment.length, encoded, sizeof(encoded));
    for (size_t i = 0; i < length; ++i) printf("%02x", encoded[i]);
    printf("\\n");
    queue.consume(fragment.boot_nonce, fragment.sequence, fragment.length);
    if (fragment.offset + fragment.length == fragment.total_length)
      queue.acknowledge(fragment.boot_nonce, fragment.sequence);
  }
}
''')
    executable = tmp_path / "rollover"
    root = _firmware_source_root()
    subprocess.run(["c++", "-std=c++11", "-I", str(root / "src"), str(source), "-o", str(executable)], check=True, capture_output=True)
    frames = [bytes.fromhex(line) for line in subprocess.check_output([str(executable)], text=True).splitlines()]
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    for value in frames:
        fake.emit(value)
        runner.process_device_line(state)
    assert len(broker.published) == 2
    assert fake.writes == [(ACK, b"ack 0000000000000001 4294967295"), (ACK, b"ack 0000000000000002 1")]
    # Replay both old and new identities: neither may publish a second time.
    for value in frames:
        fake.emit(value)
        runner.process_device_line(state)
    assert len(broker.published) == 2
    # Do not fix firmware wrap by accepting a lower sequence under the new nonce.
    fake.emit(wire(2, 4294967295, RX))
    runner.process_device_line(state)
    fake.emit(wire(2, 1, RX))
    assert not runner.process_device_line(state)
    assert len(broker.published) == 3


@pytest.mark.parametrize("lost_length,ending", [(256, "\n"), (256, "\r\n"), (300, "\n")])
def test_real_linebuffer_truncated_raw_cannot_attach_previous_packet_bytes(tmp_path, lost_length, ending):
    """Production C++ line capture/queue/framing -> actual host runner/parser."""
    source = tmp_path / "truncation.cpp"
    source.write_text('''
#include <cstdio>
#include <cstring>
#include <string>
#include "observer/ObserverRecordQueue.h"
#include "observer/ObserverStreamQueue.h"
#include "observer/ObserverLineBuffer.h"
#include "observer/ObserverProtocol.h"
int main() {
  observer::RecordQueue<2048, 16> queue(true, 7);
  observer::LineBuffer<256, 2048, decltype(queue)> buffer(queue);
  const std::string input = ''' + json.dumps((RAW + RX).decode() + "12:00:00 - 1/1/2026 U RAW: ".ljust(lost_length, "B") + ending + RX.decode()) + ''';
  buffer.write(reinterpret_cast<const uint8_t*>(input.data()), input.size());
  observer::RecordFragment fragment{};
  while (queue.peek(fragment, 4)) {
    uint8_t encoded[20];
    size_t length = observer::encodeFrame(fragment.boot_nonce, fragment.sequence,
      fragment.total_length, fragment.offset, fragment.bytes, fragment.length, encoded, sizeof(encoded));
    for (size_t i = 0; i < length; ++i) printf("%02x", encoded[i]);
    printf("\\n");
    queue.consume(fragment.boot_nonce, fragment.sequence, fragment.length);
    if (fragment.offset + fragment.length == fragment.total_length)
      queue.acknowledge(fragment.boot_nonce, fragment.sequence);
  }
}
''')
    executable = tmp_path / "truncation"
    root = _firmware_source_root()
    subprocess.run(["c++", "-std=c++11", "-I", str(root / "src"), str(source), "-o", str(executable)], check=True, capture_output=True)
    frames = [bytes.fromhex(line) for line in subprocess.check_output([str(executable)], text=True).splitlines()]
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    broker = FakeBrokerClient()
    state = make_test_state(device=connection, broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    for frame in frames:
        fake.emit(frame)
        runner.process_device_line(state)
    assert len(broker.published) == 2
    assert json.loads(broker.published[0][1])["raw"] == "AABB"
    assert json.loads(broker.published[1][1])["raw"] is None
    assert connection.metrics["sequence_gaps"] == 1
    assert fake.writes == [(ACK, b"ack 0000000000000007 1"), (ACK, b"ack 0000000000000007 2"), (ACK, b"ack 0000000000000007 4")]
