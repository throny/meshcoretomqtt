"""Characterize duplicate delivery without relaxing ordered-record validation.

These reproduce aggregate signatures, not the origin of live duplication.
Callbacks run on the actual transport owner loop, as Bleak delivers them.
"""
import asyncio

import pytest

from bridge.ble_connection import LOG_CHARACTERISTIC_UUID
from test_ble_connection import (
    FakeGatt, close_ble_connections_after_test, frame, make_connection,
)


async def deliver(connection, callbacks, fragments):
    for fragment in fragments:
        for callback in callbacks:
            callback(LOG_CHARACTERISTIC_UUID, bytearray(fragment))
    # Let scheduled real _accept_record tasks acquire the completion lock before
    # this barrier; their ACK and line delivery must finish before assertions.
    await asyncio.sleep(0)
    async with connection._completion_lock:
        pass


@pytest.mark.parametrize(
    'length,copies,cycles,notifications,missing,mismatches,minimum',
    [
        (487, 3, 299, 2691, 1196, 299, 47),
        (462, 4, 299, 3588, 1794, 299, 22),
        (491, 8, 181, 4344, 2534, 181, 51),
    ],
)
def test_repeated_continuations_match_failure_counts_without_acknowledging(
    length, copies, cycles, notifications, missing, mismatches, minimum,
):
    # Removing ordered-offset rejection would falsely ACK these invalid
    # delivery streams; broken diagnostic accounting would obscure the failure.
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    value = b'x' * (length - 1) + b'\n'
    fragments = [frame(1, length, offset, value[offset:offset + 228])
                 for offset in (0, 228, 456)]
    callback = fake.callbacks[LOG_CHARACTERISTIC_UUID]

    connection._run(deliver(connection, [callback] * copies, fragments * cycles))

    assert connection.metrics['notifications'] == notifications
    assert connection.metrics['reject_missing_start'] == missing
    assert connection.metrics['reject_offset_mismatch'] == mismatches
    assert connection.metrics['notify_bytes_min'] == minimum
    assert connection.metrics['notify_bytes_max'] == 244
    assert connection.metrics['acked_records'] == 0
    assert connection.metrics['ack_failures'] == 0
    assert connection.read_line() is None
    assert fake.writes == []


def test_single_delivery_on_owner_loop_completes_and_acknowledges_record():
    # A broken test barrier or owner-loop completion path must not make every
    # duplicate-stream test pass merely because ACK tasks never execute.
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    value = b'x' * 461 + b'\n'
    fragments = [frame(1, len(value), offset, value[offset:offset + 228])
                 for offset in (0, 228, 456)]

    connection._run(deliver(
        connection, [fake.callbacks[LOG_CHARACTERISTIC_UUID]], fragments,
    ))

    assert connection.read_line() == 'x' * 461
    assert connection.read_line() is None
    assert connection.metrics['notifications'] == 3
    assert connection.metrics['acked_records'] == 1
    assert len(fake.writes) == 1


@pytest.mark.parametrize('length,acked,duplicates,missing,mismatches', [
    (100, 3, 2, 0, 0),
    (300, 1, 0, 2, 0),
    (487, 0, 0, 4, 1),
])
def test_triplication_allows_short_records_but_blocks_three_fragment_records(
    length, acked, duplicates, missing, mismatches,
):
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    value = b'x' * (length - 1) + b'\n'
    fragments = [frame(1, length, offset, value[offset:offset + 228])
                 for offset in range(0, length, 228)]
    callback = fake.callbacks[LOG_CHARACTERISTIC_UUID]

    connection._run(deliver(connection, [callback] * 3, fragments))

    assert connection.read_line() == ('x' * (length - 1) if acked else None)
    assert connection.read_line() is None
    assert connection.metrics['acked_records'] == acked
    assert connection.metrics['duplicate_records'] == duplicates
    assert connection.metrics['reject_missing_start'] == missing
    assert connection.metrics['reject_offset_mismatch'] == mismatches


def test_repeated_open_and_reconnects_fence_all_retired_callbacks():
    # Removing generation fencing would make retired subscriptions multiply
    # active callbacks and corrupt the valid continuation stream.
    fake = FakeGatt()
    connection = make_connection(fake)
    retired = []
    for sequence in range(1, 10):
        connection.open()
        active = fake.callbacks[LOG_CHARACTERISTIC_UUID]
        connection.open()  # An already-open link must not resubscribe.
        value = b'x' * 461 + b'\n'
        fragments = [frame(sequence, len(value), offset, value[offset:offset + 228])
                     for offset in (0, 228, 456)]
        connection._run(deliver(connection, retired + [active], fragments))

        assert connection.read_line() == 'x' * 461
        assert connection.read_line() is None
        assert connection.metrics['notifications'] == sequence * 3
        assert connection.metrics['acked_records'] == sequence
        assert connection.metrics['reject_offset_mismatch'] == 0
        retired.append(active)
        connection.close()


class PassiveBus:
    """Only the D-Bus transport boundary used by real Bleak disconnect()."""
    def __init__(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    async def wait_for_disconnect(self):
        assert not self.connected


class RemoteDisconnectedGatt(FakeGatt):
    def __init__(self, backend):
        super().__init__()
        self._backend = backend
        backend._device_info = {'Paired': True, 'Trusted': True}
        self.bus = PassiveBus()
        backend._bus = self.bus
        self.stop_attempts = 0

    async def stop_notify(self, uuid):
        self.stop_attempts += 1
        await super().stop_notify(uuid)

    async def disconnect(self):
        # Keep the installed-version Bleak cleanup implementation real. No
        # D-Bus or Bluetooth connection is created by this test.
        self._record_operation()
        await self._backend.disconnect()
        self.is_connected = False


def test_close_releases_real_bleak_bus_after_remote_disconnection():
    bleak = pytest.importorskip('bleak.backends.bluezdbus.client')
    backend = bleak.BleakClientBlueZDBus('02:00:00:00:00:01', bluez={}, timeout=10)
    fake = RemoteDisconnectedGatt(backend)
    connection = make_connection(fake)
    connection.open()
    owner_loop = connection._loop
    owner_thread = connection._loop_thread.ident
    # Bleak 3.0.2's remote-disconnect callback sets its physical state false and
    # calls _cleanup_all(), which removes watchers but does NOT close _bus.
    # Public disconnect() still must run to release the D-Bus owner/session.
    fake.is_connected = False
    backend._is_connected = False
    backend._cleanup_all()

    connection.close()

    assert not fake.bus.connected
    assert backend._bus is None
    assert not connection.is_open
    assert fake.stop_attempts == 0
    assert connection.metrics['disconnects'] == 0
    assert fake.operation_threads[-1] == owner_thread
    assert fake.operation_loops[-1] is owner_loop
