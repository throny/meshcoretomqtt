"""Behavioral tests for the framed SenseCAP observer GATT transport.

These tests use a fake GATT client because BlueZ/pairing hardware is an
acceptance gate, not a unit-test prerequisite.
"""
from __future__ import annotations

import asyncio
import threading
import time
import struct
from types import SimpleNamespace

import pytest

from bridge.ble_connection import (
    COMMAND_CHARACTERISTIC_UUID,
    LOG_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    BleConnection,
    BleSecurityError,
)
from bridge.runner import connect_transport

_connections = []


@pytest.fixture(autouse=True)
def close_ble_connections_after_test():
    if hasattr(BleConnection, "_process_sessions"):
        BleConnection._process_sessions.clear()
    start = len(_connections)
    yield
    for connection in reversed(_connections[start:]):
        connection.close()
    del _connections[start:]


def frame(record_id: int, total: int, offset: int, payload: bytes) -> bytes:
    return struct.pack("<QIHH", 0x0123456789ABCDEF, record_id, total, offset) + payload


def test_uses_the_exact_uuid_sequence_from_the_observer_firmware():
    assert SERVICE_UUID == "10c25d4a-3c61-50a3-5e48-566b026f5f1b"
    assert LOG_CHARACTERISTIC_UUID == "10c25d4a-3c61-50a3-5e48-566b036f5f1b"
    assert COMMAND_CHARACTERISTIC_UUID == "10c25d4a-3c61-50a3-5e48-566b046f5f1b"


class FakeGatt:
    def __init__(self, *, paired: bool = True, trusted: bool = True) -> None:
        self._backend = SimpleNamespace(_device_info={"Paired": paired, "Trusted": trusted})
        self.is_connected = False
        self.writes: list[tuple[str, bytes]] = []
        self.callbacks = {}
        self.services = FakeServices()
        self.operation_threads = []
        self.operation_loops = []

    def _record_operation(self):
        self.operation_threads.append(threading.get_ident())
        self.operation_loops.append(asyncio.get_running_loop())

    async def connect(self) -> None:
        self._record_operation()
        self.is_connected = True

    async def disconnect(self) -> None:
        self._record_operation()
        self.is_connected = False

    async def start_notify(self, uuid: str, callback) -> None:
        self._record_operation()
        assert uuid == LOG_CHARACTERISTIC_UUID
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid: str) -> None:
        self._record_operation()
        self.callbacks.pop(uuid, None)

    async def read_gatt_char(self, uuid: str) -> bytes:
        self._record_operation()
        assert uuid == "10c25d4a-3c61-50a3-5e48-566b066f5f1b"
        return b"evicted=0/0 truncated=0/0 failed=0"

    async def write_gatt_char(self, uuid: str, value: bytes, response: bool = True) -> None:
        self._record_operation()
        assert uuid in {COMMAND_CHARACTERISTIC_UUID, "10c25d4a-3c61-50a3-5e48-566b056f5f1b"}
        assert response is True
        self.writes.append((uuid, value))

    def emit(self, payload: bytes) -> None:
        self.callbacks[LOG_CHARACTERISTIC_UUID](LOG_CHARACTERISTIC_UUID, bytearray(payload))


class FakeServices:
    def __init__(self, *, present=True):
        self.present = present

    def get_service(self, uuid):
        if not self.present or uuid != SERVICE_UUID:
            return None
        return type("Service", (), {"characteristics": [
            type("Characteristic", (), {"uuid": LOG_CHARACTERISTIC_UUID})(),
            type("Characteristic", (), {"uuid": COMMAND_CHARACTERISTIC_UUID})(),
            type("Characteristic", (), {"uuid": "10c25d4a-3c61-50a3-5e48-566b056f5f1b"})(),
            type("Characteristic", (), {"uuid": "10c25d4a-3c61-50a3-5e48-566b066f5f1b"})(),
        ]})()


def make_connection(fake: FakeGatt, **kwargs) -> BleConnection:
    connection = BleConnection("02:00:00:00:00:01", client_factory=lambda _: fake, **kwargs)
    _connections.append(connection)
    return connection


def test_requires_the_observer_service_and_a_paired_trusted_bluez_device():
    fake = FakeGatt(paired=False)
    connection = make_connection(fake)

    with pytest.raises(BleSecurityError):
        connection.open()

    assert not connection.is_open


def test_service_discovery_and_security_must_fail_closed():
    fake = FakeGatt()
    fake.services = FakeServices(present=False)
    connection = make_connection(fake)
    with pytest.raises(Exception, match="service"):
        connection.open()

    fake = FakeGatt()
    fake._backend._device_info = None
    connection = make_connection(fake)
    with pytest.raises(BleSecurityError):
        connection.open()


def test_blue_z_device_info_properties_shape_is_accepted():
    fake = FakeGatt()
    # Bleak BlueZ copies Device1 properties from BLEDevice.details["props"]
    # into the backend, not the public BleakClient wrapper (observed on 3.0.2).
    fake._backend._device_info = {"Paired": True, "Trusted": True, "Connected": True}
    connection = make_connection(fake)
    connection.open()
    assert connection.is_open


@pytest.mark.parametrize("properties", [
    None, {}, {"Paired": False, "Trusted": True},
    {"Paired": True, "Trusted": False},
    {"Paired": "true", "Trusted": True},
    {"Paired": True, "Trusted": 1},
])
def test_wrapped_backend_missing_or_unverified_security_fails_closed(properties):
    fake = FakeGatt()
    fake._backend._device_info = properties
    fake._device_info = {"Paired": True, "Trusted": True}
    connection = make_connection(fake)
    with pytest.raises(BleSecurityError):
        connection.open()
    assert not connection.is_open


@pytest.mark.parametrize("backend", [None, SimpleNamespace()])
def test_invalid_backend_never_falls_back_to_verified_wrapper(backend):
    fake = FakeGatt()
    fake._backend = backend
    fake._device_info = {"Paired": True, "Trusted": True}
    connection = make_connection(fake)
    with pytest.raises(BleSecurityError):
        connection.open()
    assert not connection.is_open


def test_direct_backend_factory_security_properties_remain_supported():
    fake = FakeGatt()
    del fake._backend
    fake._device_info = {"Paired": True, "Trusted": True}
    connection = make_connection(fake)
    connection.open()
    assert connection.is_open


def test_bleak_operations_share_persistent_loop_thread_and_notify_survives_open():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    opened_threads = set(fake.operation_threads)
    opened_loops = set(fake.operation_loops)
    assert fake.callbacks[LOG_CHARACTERISTIC_UUID]
    assert len(opened_threads) == len(opened_loops) == 1

    # An operation after open must use the exact same live loop and owner thread.
    fake.emit(frame(30, 5, 0, b"line\n"))
    assert connection.read_line() == "line"
    async def check_live_loop():
        fake._record_operation()
    connection._run(check_live_loop())
    assert set(fake.operation_threads) == opened_threads
    assert set(fake.operation_loops) == opened_loops
    connection.close()


def test_client_is_constructed_on_the_persistent_loop_thread():
    fake = FakeGatt()
    factory_context = []

    def factory(address):
        factory_context.append((threading.get_ident(), asyncio.get_running_loop()))
        return fake

    connection = BleConnection("02:00:00:00:00:01", client_factory=factory)
    _connections.append(connection)
    connection.open()
    assert len(factory_context) == 1
    assert factory_context[0][0] == fake.operation_threads[0]
    assert factory_context[0][1] is fake.operation_loops[0]


def test_command_replies_must_match_active_request_and_ignore_stale_log_lines():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()

    # Startup prompts/old command text already in the stream must not reply to ver.
    for record_id, stale in enumerate((b"meshcore>\r\n", b"-> >OldName\r\n", b"12:00:00 - 1/1/2026 U: RX, len=1 (type=1, route=D, payload_len=0)\r\n"), 1):
        fake.emit(frame(record_id, len(stale), 0, stale))
    original_write = fake.write_gatt_char

    async def write_and_reply(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        reply = b"-> 1.17.1 (Build: test)\r\n"
        fake.emit(frame(4, len(reply), 0, reply))

    fake.write_gatt_char = write_and_reply
    assert connection.execute_command("ver", timeout=0.1) == (True, "1.17.1 (Build: test)")


def test_out_of_order_reply_for_another_method_does_not_satisfy_active_command():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    original_write = fake.write_gatt_char

    async def reply_with_name(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        reply = b"-> > stale-name\r\n" if value == b"ver" else b"-> 1.17.1 (Build: test)\r\n"
        fake.emit(frame(1 if value == b"ver" else 2, len(reply), 0, reply))

    fake.write_gatt_char = reply_with_name
    assert connection.execute_command("ver", timeout=0.02) == (
        False, "BLE command timed out; reconnect required to resynchronize replies"
    )


def test_concurrent_command_transactions_are_serialized_and_timeouts_do_not_reuse_reply():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    writes_in_flight = 0
    maximum_in_flight = 0
    lock = threading.Lock()
    original_write = fake.write_gatt_char

    async def delayed_write(uuid, value, response=True):
        nonlocal writes_in_flight, maximum_in_flight
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            await original_write(uuid, value, response)
            return
        with lock:
            writes_in_flight += 1
            maximum_in_flight = max(maximum_in_flight, writes_in_flight)
        await original_write(uuid, value, response)
        await asyncio.sleep(0.04)
        reply = b"-> 1.17.1 (Build: test)\r\n" if value == b"ver" else b"-> SEA-Board\r\n"
        fake.emit(frame(len(fake.writes), len(reply), 0, reply))
        with lock:
            writes_in_flight -= 1

    fake.write_gatt_char = delayed_write
    results = {}
    calls = [threading.Thread(target=lambda cmd=cmd: results.setdefault(cmd, connection.execute_command(cmd, timeout=0.5))) for cmd in ("ver", "board")]
    for call in calls:
        call.start()
    for call in calls:
        call.join()

    assert maximum_in_flight == 1
    assert results == {"ver": (True, "1.17.1 (Build: test)"), "board": (True, "SEA-Board")}


def test_zero_length_fragment_for_nonempty_record_is_malformed():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(frame(1, 5, 0, b""))
    assert connection.read_line() is None
    assert connection.metrics["malformed_frames"] == 1


def test_timeout_requires_reconnect_instead_of_allowing_late_reply_cross_match():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    assert connection.execute_command("ver", timeout=0.001) == (
        False, "BLE command timed out; reconnect required to resynchronize replies"
    )
    # Late reply after timeout is never eligible for the next command.
    late = b"1.17.1\r\n"
    if LOG_CHARACTERISTIC_UUID in fake.callbacks:
        fake.emit(frame(1, len(late), 0, late))
    assert connection.execute_command("board", timeout=0.1) == (
        False, "BLE connection is not open"
    )


def test_stalled_gatt_write_is_bounded_and_tears_down_connection():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    write_started = threading.Event()
    release_write = asyncio.Event()
    original_write = fake.write_gatt_char

    async def stalled_write(uuid, value, response=True):
        await original_write(uuid, value, response)
        write_started.set()
        await release_write.wait()

    fake.write_gatt_char = stalled_write
    result = []
    started = threading.Event()

    def issue_command():
        started.set()
        result.append(connection.execute_command("ver", timeout=0.05))

    caller = threading.Thread(target=issue_command, daemon=True)
    began = time.monotonic()
    caller.start()
    assert started.wait(0.2)
    assert write_started.wait(0.2)
    caller.join(timeout=0.15)
    returned_within_budget = not caller.is_alive()

    # Always unblock the fake on the loop so a RED run can cleanly finish.
    if caller.is_alive() and connection._loop:
        connection._loop.call_soon_threadsafe(release_write.set)
    caller.join(timeout=1)
    elapsed = time.monotonic() - began

    assert returned_within_budget, f"stalled GATT write exceeded budget ({elapsed:.3f}s)"
    assert result == [(False, "BLE command timed out; reconnect required to resynchronize replies")]
    assert not connection.is_open
    assert connection._loop is None
    assert fake.is_connected is False


def test_reopen_after_timeout_constructs_fresh_client_on_usable_loop():
    clients = [FakeGatt(), FakeGatt()]
    factory_context = []

    def factory(address):
        client = clients[len(factory_context)]
        factory_context.append((threading.get_ident(), asyncio.get_running_loop(), client))
        return client

    connection = BleConnection("02:00:00:00:00:01", client_factory=factory)
    _connections.append(connection)
    connection.open()
    assert connection.execute_command("ver", timeout=0.01) == (
        False, "BLE command timed out; reconnect required to resynchronize replies"
    )
    assert not clients[0].is_connected

    connection.open()
    assert connection.is_open
    assert len(factory_context) == 2
    assert factory_context[0][2] is clients[0]
    assert factory_context[1][2] is clients[1]
    assert factory_context[1][0] == clients[1].operation_threads[0]
    assert factory_context[1][1] is clients[1].operation_loops[0]
    async def check_reconnected_loop():
        clients[1]._record_operation()
    connection._run(check_reconnected_loop())
    assert clients[1].operation_loops[-1] is factory_context[1][1]


def test_hung_stop_notify_still_disconnects_before_loop_shutdown_and_reconnect():
    clients = [FakeGatt(), FakeGatt()]
    factory_context = []
    disconnect_context = []

    class HungStopNotifyGatt(FakeGatt):
        async def stop_notify(self, uuid: str) -> None:
            self._record_operation()
            self.callbacks.pop(uuid, None)
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self._record_operation()
            disconnect_context.append((self, connection._loop.is_running()))
            self.is_connected = False

    old_client = HungStopNotifyGatt()
    clients[0] = old_client

    def factory(address):
        client = clients[len(factory_context)]
        factory_context.append((client, asyncio.get_running_loop()))
        return client

    connection = BleConnection("02:00:00:00:00:01", client_factory=factory)
    _connections.append(connection)
    connection.open()
    old_callback = old_client.callbacks[LOG_CHARACTERISTIC_UUID]

    started = time.monotonic()
    connection._close_link(timeout=0.05)
    cleanup_elapsed = time.monotonic() - started

    assert disconnect_context == [(old_client, True)]
    assert not old_client.is_connected
    assert connection._loop is None
    assert cleanup_elapsed < 0.5

    connection.open()
    assert connection.is_open
    old_line = b"stale old client line\n"
    old_callback(LOG_CHARACTERISTIC_UUID, bytearray(frame(1, len(old_line), 0, old_line)))
    assert connection.read_line() is None
    assert factory_context[1][0] is clients[1]
    assert factory_context[1][1] is not factory_context[0][1]


@pytest.mark.parametrize("disconnect_failure", ["error", "hang"])
def test_timeout_with_unconfirmed_disconnect_is_not_open_or_safe_to_reopen(disconnect_failure):
    class UnconfirmedDisconnectGatt(FakeGatt):
        async def disconnect(self):
            self._record_operation()
            if disconnect_failure == "error":
                raise RuntimeError("disconnect failed")
            await asyncio.Event().wait()

    fake = UnconfirmedDisconnectGatt()
    connection = make_connection(fake)
    connection.open()
    old_callback = fake.callbacks[LOG_CHARACTERISTIC_UUID]
    assert connection.execute_command("ver", timeout=0.01) == (
        False, "BLE command timed out; reconnect required to resynchronize replies"
    )
    assert connection._loop is None
    assert fake.is_connected  # Uncertain physical state must not be overwritten.
    assert not connection.is_open
    assert connection.execute_command("board", timeout=0.01) == (
        False, "BLE connection is not open"
    )
    old_callback(LOG_CHARACTERISTIC_UUID, bytearray(frame(1, 5, 0, b"late\n")))
    assert connection.read_line() is None
    with pytest.raises(RuntimeError, match="refusing a second link"):
        connection.open()
    assert connection._loop is None
    assert connection.metrics["disconnects"] == 0

    fake.is_connected = False
    connection.open()
    assert connection.is_open
    fake.emit(frame(2, 5, 0, b"live\n"))
    assert connection.read_line() == "live"


def test_reassembles_classic_line_only_after_all_ordered_fragments_arrive():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()

    line = b"12:00:00 - 1/1/2026 U RAW: AABB\r\n"
    fake.emit(frame(7, len(line), 0, line[:11]))
    assert connection.read_line() is None
    fake.emit(frame(7, len(line), 11, line[11:]))

    assert connection.read_line() == "12:00:00 - 1/1/2026 U RAW: AABB"
    assert connection.metrics["malformed_frames"] == 0


def test_discards_incomplete_record_before_later_record_instead_of_splicing_lines():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()

    fake.emit(frame(1, 20, 0, b"partial RAW "))
    next_line = b"12:00:01 - 1/1/2026 U: RX, len=1 (type=1, route=D, payload_len=0)\n"
    fake.emit(frame(2, len(next_line), 0, next_line))

    assert connection.read_line() == next_line.decode().strip()
    assert connection.read_line() is None
    assert connection.metrics["incomplete_records"] == 1
    assert connection.metrics["dropped_records"] == 1


@pytest.mark.parametrize("bad", [
    b"\x01",  # header truncation
    frame(1, 2, 0, b"abc"),  # payload longer than total
    frame(1, 4, 1, b"abc"),  # first offset must be zero
])
def test_rejects_malformed_frames_without_emitting_a_line(bad: bytes):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(bad)

    assert connection.read_line() is None
    assert connection.metrics["malformed_frames"] == 1


@pytest.mark.parametrize("offset,payload", [(2, b"ef"), (6, b"gh")])
def test_rejects_nonzero_duplicate_or_reordered_fragment(offset, payload):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(frame(4, 8, 0, b"ab"))
    fake.emit(frame(4, 8, 2, b"cd"))
    fake.emit(frame(4, 8, offset, payload))
    fake.emit(frame(4, 8, 4, b"efgh"))

    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics["reordered_frames"] == 1
    assert connection.metrics["dropped_records"] == 1


def test_same_record_restart_replaces_partial_bytes_and_acks_only_completion():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(frame(19, 8, 0, b"old!"))
    fake.emit(frame(19, 8, 0, b"new!"))
    assert connection.read_line() is None
    assert fake.writes == []
    fake.emit(frame(19, 8, 4, b"end\n"))
    assert connection.read_line() == "new!end"
    assert connection.read_line() is None
    assert fake.writes == [("10c25d4a-3c61-50a3-5e48-566b056f5f1b", b"ack 0123456789abcdef 19")]
    assert connection.metrics["reordered_frames"] == 0
    assert connection.metrics["malformed_frames"] == 0
    assert connection.metrics["dropped_records"] == 0


def test_repeated_twenty_byte_frame_restarts_recover_and_complete_replay_is_deduplicated():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    line = b"x" * 129 + b"\n"
    for partial_end in (4, 20, 8, 36):
        for offset in range(0, partial_end, 4):
            fake.emit(frame(19, 130, offset, line[offset:offset + 4]))
        assert connection.read_line() is None
        assert fake.writes == []
    for _ in range(2):
        for offset in range(0, 130, 4):
            fake.emit(frame(19, 130, offset, line[offset:offset + 4]))
    assert connection.read_line() == "x" * 129
    assert connection.read_line() is None
    assert len(fake.writes) == 2
    assert connection.metrics["acked_records"] == 2
    assert connection.metrics["duplicate_records"] == 1
    assert connection.metrics["malformed_frames"] == 0
    assert connection.metrics["reordered_frames"] == 0


def test_offset_zero_length_change_is_not_a_valid_same_record_restart():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    fake.emit(frame(19, 8, 0, b"old!"))
    fake.emit(frame(19, 7, 0, b"new!"))
    fake.emit(frame(19, 7, 4, b"no\n"))
    assert connection.read_line() is None
    assert fake.writes == []
    assert connection.metrics["reordered_frames"] == 1


def test_execute_command_only_writes_exact_firmware_allowlist():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()

    ok, response = connection.execute_command("set radio 915", timeout=0.01)
    assert (ok, response) == (False, "observer operation not allowed")
    assert fake.writes == []

    # Replies are emitted after the matching write, not queued before it.
    original_write = fake.write_gatt_char
    async def write_and_reply(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        reply = b"-> 1.17.1 (Build: test)\r\n"
        fake.emit(frame(1, len(reply), 0, reply))
    fake.write_gatt_char = write_and_reply
    ok, response = connection.execute_command("ver", timeout=0.1)
    assert ok is True
    assert response == "1.17.1 (Build: test)"
    assert fake.writes == [(COMMAND_CHARACTERISTIC_UUID, b"ver"), ("10c25d4a-3c61-50a3-5e48-566b056f5f1b", b"ack 0123456789abcdef 1")]


def test_time_is_canonical_uint32_and_disconnect_can_reconnect():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()

    assert connection.execute_command("time 01", timeout=0.01)[0] is False
    connection.close()
    assert not connection.is_open
    connection.open()
    assert connection.is_open


def test_bounds_host_line_queue_and_counts_ble_drops():
    fake = FakeGatt()
    connection = make_connection(fake, max_queued_lines=1)
    connection.open()
    fake.emit(frame(1, 2, 0, b"a\n"))
    fake.emit(frame(2, 2, 0, b"b\n"))

    assert connection.read_line() == "b"
    assert connection.metrics["dropped_records"] == 1


def test_runner_keeps_serial_default_and_requires_explicit_ble():
    from tests.fakes import FakeSerialConnection
    serial, ble = FakeSerialConnection(), FakeSerialConnection()
    factories = {"serial_factory": lambda _: serial, "ble_factory": lambda _: ble}
    assert connect_transport({}, **factories) is serial
    assert connect_transport({"transport": "ble", "ble": {"address": "02:00:00:00:00:01"}}, **factories) is ble
    assert connect_transport({"transport": "auto"}, **factories) is None


def test_public_private_key_and_stats_methods_match_classic_serial_parsing():
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    responses = iter([
        f"-> > {'ab' * 32}\r\n".encode(),
        f"-> > {'cd' * 64}\r\n".encode(),
        b'-> {"battery_mv":4200,"uptime_secs":9,"errors":0,"queue_len":1}\r\n',
        b'-> {"noise_floor":-100,"tx_air_secs":2,"rx_air_secs":3}\r\n',
        b'-> {"recv_errors":4}\r\n',
    ])
    original_write = fake.write_gatt_char

    async def write_and_reply(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        reply = next(responses)
        fake.emit(frame(len(fake.writes), len(reply), 0, reply))

    fake.write_gatt_char = write_and_reply
    assert connection.get_pubkey() == "AB" * 32
    assert connection.get_privkey() == "cd" * 64
    stats = connection.get_device_stats()
    assert stats.pop("observer_firmware")["evicted_records"] == 0
    assert stats.pop("observer_host")["ack_failures"] == 0
    assert stats == {
        "battery_mv": 4200, "uptime_secs": 9, "debug_flags": 0, "queue_len": 1,
        "noise_floor": -100, "tx_air_secs": 2, "rx_air_secs": 3, "recv_errors": 4,
    }
