"""Outage/initialization cleanup: real runner and BLE lifecycle, fake I/O only."""
from __future__ import annotations

import itertools
import json
import logging
import struct
import time

import pytest

from bridge import runner
from bridge.ble_connection import (
    BleConnection,
    COMMAND_CHARACTERISTIC_UUID,
    LOG_CHARACTERISTIC_UUID,
)
from tests.fakes import CallbackBrokerClient, FakeSerialConnection, make_test_state
from bridge.mqtt_manager import MqttManager
from test_ble_connection import FakeGatt, frame


class TrackedSerial(FakeSerialConnection):
    def __init__(self, **kwargs):
        self.fail_getter = kwargs.pop("fail_getter", False)
        super().__init__(**kwargs)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        super().close()

    def get_name(self):
        if self.fail_getter:
            raise RuntimeError("synthetic device read failure")
        return super().get_name()


class StartupBle(BleConnection):
    """Keep real GATT ownership/teardown; supply startup getter I/O locally."""

    def __init__(self, backend, **kwargs):
        super().__init__("02:00:00:00:00:01", client_factory=lambda _: backend)
        self.answers = TrackedSerial(**kwargs)
        self.close_calls = 0

    def get_name(self): return self.answers.get_name()
    def get_pubkey(self): return self.answers.get_pubkey()
    def get_privkey(self): return self.answers.get_privkey()
    def get_radio_info(self): return self.answers.get_radio_info()
    def get_firmware_version(self): return self.answers.get_firmware_version()
    def get_board_type(self): return self.answers.get_board_type()
    def get_device_stats(self): return self.answers.get_device_stats()

    def close(self):
        self.close_calls += 1
        super().close()


class StartupBrokerIO:
    def __init__(self, state, *, available=False, stop_on_failure=False):
        self.state = state
        self.available = available
        self.stop_on_failure = stop_on_failure
        self.attempts = 0

    def __call__(self, **kwargs):
        owner = self

        class Client(CallbackBrokerClient):
            def connect(self, *args, **options):
                owner.attempts += 1
                if owner.stop_on_failure:
                    owner.state.should_exit = True
                return super().connect(*args, **options)

        return Client(unavailable=not self.available,
                      after_connect=lambda: setattr(self.state, "should_exit", True), **kwargs)


class MainLoopBrokerFactory:
    """Deterministic broker availability across one real runner session."""

    def __init__(self, *, unavailable_attempts=()):
        self.unavailable_attempts = set(unavailable_attempts)
        self.attempts = 0
        self.clients = []

    def __call__(self, **kwargs):
        attempt = self.attempts
        self.attempts += 1
        client = CallbackBrokerClient(
            unavailable=attempt in self.unavailable_attempts,
            **kwargs,
        )
        self.clients.append(client)
        return client


class ScriptedSerial(TrackedSerial):
    """Real runner-facing fake with deterministic per-iteration events."""

    def __init__(self, on_read, **kwargs):
        self.on_read = on_read
        self.read_count = 0
        super().__init__(**kwargs)

    def read_line(self):
        self.read_count += 1
        self.on_read(self.read_count)
        return None


class DisconnectingStartupBle(StartupBle):
    """Real BleConnection lifecycle with a fake backend loss after startup."""

    def __init__(self, backend, on_read, **kwargs):
        self.backend = backend
        self.on_read = on_read
        self.read_count = 0
        super().__init__(backend, **kwargs)

    def read_line(self):
        self.read_count += 1
        self.on_read(self.read_count)
        return super().read_line()


class StatsResponseGatt(FakeGatt):
    """Return deterministic stats command records over the real fake-GATT path."""

    _boot_nonces = itertools.count(0x5354415453000001)

    def __init__(self):
        super().__init__()
        # Each instance is a new simulated boot, not a replay of another test's
        # peer/session with sequence numbers starting at one again.
        self.boot_nonce = next(self._boot_nonces)
        self.sequence = 0
        self.responses = {
            "stats-core": b'-> {"battery_mv":4200,"uptime_secs":10,"errors":0,"queue_len":0}\r\n',
            "stats-radio": b'-> {"noise_floor":-100,"tx_air_secs":0,"rx_air_secs":0}\r\n',
            "stats-packets": b'-> {"recv_errors":0}\r\n',
        }

    async def write_gatt_char(self, uuid, value, response=True):
        await super().write_gatt_char(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        reply = self.responses[value.decode("ascii")]
        self.sequence += 1
        record = bytearray(frame(self.sequence, len(reply), 0, reply))
        struct.pack_into("<Q", record, 0, self.boot_nonce)
        self.emit(bytes(record))


class StatsPollStartupBle(StartupBle):
    """Use real BleConnection stats commands and trigger one test poll in-loop."""

    def __init__(self, backend, on_read, **kwargs):
        self.on_read = on_read
        self.read_count = 0
        self.stats_calls = 0
        super().__init__(backend, **kwargs)

    def get_device_stats(self):
        self.stats_calls += 1
        return BleConnection.get_device_stats(self)

    def read_line(self):
        self.read_count += 1
        self.on_read(self.read_count)
        return super().read_line()


def make_main_loop_state(broker_factory):
    state = make_test_state()
    state.mqtt_manager = MqttManager(state, client_factory=broker_factory)
    return state


def disconnect_first_broker(state):
    info = state.mqtt_clients[0]
    state.mqtt_manager.on_mqtt_disconnect(
        info["client"], info["client"].userdata, None, 0, None
    )
    # The runner and manager remain real; fast-forward only the fake outage's
    # reconnect deadline so the test does not wait through production backoff.
    info["reconnect_at"] = 0


@pytest.fixture(params=["serial", "ble"])
def startup(request):
    devices = []
    sleeps = []

    def prepare(**kwargs):
        backend = FakeGatt() if request.param == "ble" else None
        device = StartupBle(backend, **kwargs) if backend else TrackedSerial(**kwargs)
        devices.append(device)

        def open_device(config):
            if backend:
                device.open()
            return device

        state = make_test_state()
        state.startup_broker_io = StartupBrokerIO(state)
        state.mqtt_manager = MqttManager(state, client_factory=state.startup_broker_io)
        state.startup_factory = open_device
        return state, device, backend

    yield prepare, sleeps
    for device in devices:
        if device.is_open:
            device.close()


def assert_released(device, backend):
    assert device.close_calls == 1
    assert not device.is_open
    if backend:
        assert not backend.is_connected
        assert not backend.callbacks


def run_startup(state, sleeps):
    runner.run(state, transport_factory=state.startup_factory, startup_wait=sleeps.append)


def test_initial_mqtt_outage_releases_device_before_process_return(startup):
    prepare, sleeps = startup
    state, device, backend = prepare()
    run_startup(state, sleeps)
    assert state.startup_broker_io.attempts == 10
    assert len(sleeps) == 10
    assert_released(device, backend)


@pytest.mark.parametrize("unavailable", ["name", "pubkey", "radio_info"])
def test_early_device_query_failure_releases_device(startup, unavailable):
    prepare, sleeps = startup
    state, device, backend = prepare(**{unavailable: None})
    run_startup(state, sleeps)
    assert state.startup_broker_io.attempts == 0
    assert_released(device, backend)


def test_initialization_exception_releases_device(startup):
    prepare, sleeps = startup
    state, device, backend = prepare(fail_getter=True)
    with pytest.raises(RuntimeError, match="synthetic device read failure"):
        run_startup(state, sleeps)
    assert_released(device, backend)


def test_stop_requested_during_initial_mqtt_failure_is_not_retried(startup):
    prepare, sleeps = startup
    state, device, backend = prepare()
    state.startup_broker_io.stop_on_failure = True
    run_startup(state, sleeps)
    assert state.startup_broker_io.attempts == 1
    assert not sleeps
    assert_released(device, backend)


def test_successful_startup_shutdown_closes_device_only_once(startup):
    prepare, sleeps = startup
    state, device, backend = prepare()
    state.startup_broker_io.available = True
    run_startup(state, sleeps)
    assert_released(device, backend)


def test_startup_then_midrun_broker_reconnect_and_signal_cleanup():
    """A later broker reconnect must not bypass graceful runner cleanup."""
    broker_factory = MainLoopBrokerFactory(unavailable_attempts={1})
    state = make_main_loop_state(broker_factory)
    stopped_after_reconnect = []

    def on_read(read_count):
        if read_count == 1:
            disconnect_first_broker(state)
        elif read_count == 2:
            # The first reconnect attempt failed; make the next attempt due.
            state.mqtt_clients[0]["reconnect_at"] = 0
        elif read_count == 3:
            stopped_after_reconnect.append(state.mqtt_clients[0]["connected"])
            runner.handle_signal(state, 15, None)

    device = ScriptedSerial(on_read)
    runner.run(state, transport_factory=lambda _config: device)

    assert broker_factory.attempts == 3  # startup, one failed reconnect, recovery
    assert broker_factory.clients[1].unavailable
    recovered_client = broker_factory.clients[2]
    assert stopped_after_reconnect == [True]
    assert device.close_calls == 1
    assert not device.is_open
    assert broker_factory.clients[0].disconnect_calls >= 1
    assert recovered_client.disconnect_calls == 1
    assert [json.loads(payload)["status"] for _, payload, _, _ in recovered_client.published] == [
        "online", "offline",
    ]


def test_startup_then_midrun_reconnect_exhaustion_closes_transport(caplog):
    """Reconnect exhaustion after startup must exit the real runner and clean up."""
    broker_factory = MainLoopBrokerFactory(unavailable_attempts={1, 2, 3, 4, 5})
    state = make_main_loop_state(broker_factory)
    state.max_reconnect_attempts = 2
    exhaustion_seen = []
    bounded_guard_seen = []

    def on_read(read_count):
        if read_count == 1:
            disconnect_first_broker(state)
        elif read_count >= 2:
            info = state.mqtt_clients[0]
            info["reconnect_at"] = 0
            if state.should_exit:
                exhaustion_seen.append(True)
            elif read_count >= 6:
                # Keep this real-runner regression bounded if the exhaustion
                # branch is removed; the assertion below exposes that regression.
                bounded_guard_seen.append(True)
                runner.handle_signal(state, 15, None)

    device = ScriptedSerial(on_read)
    with caplog.at_level(logging.CRITICAL):
        runner.run(state, transport_factory=lambda _config: device)

    assert broker_factory.attempts == 2  # initial connect and one failed retry
    assert exhaustion_seen == [True]
    assert bounded_guard_seen == []
    assert "2 consecutive failures - exiting for service restart" in caplog.text
    assert device.close_calls == 1
    assert not device.is_open


def test_startup_then_midrun_stop_suppresses_next_broker_retry_and_cleans_up():
    """SIGTERM during a post-start outage must stop before another retry."""
    broker_factory = MainLoopBrokerFactory(unavailable_attempts={1, 2})
    state = make_main_loop_state(broker_factory)

    def on_read(read_count):
        if read_count == 1:
            disconnect_first_broker(state)
        elif read_count == 2:
            runner.handle_signal(state, 15, None)

    device = ScriptedSerial(on_read)
    runner.run(state, transport_factory=lambda _config: device)

    assert broker_factory.attempts == 2  # no attempt after the stop request
    assert device.close_calls == 1
    assert not device.is_open
    assert broker_factory.clients[0].disconnect_calls >= 1


def test_startup_then_midrun_fake_gatt_backend_loss_closes_before_unavailable_retry():
    """A real BleConnection backend loss must close before runner retry/exit."""
    backend = FakeGatt()
    broker_factory = MainLoopBrokerFactory()
    state = make_main_loop_state(broker_factory)
    guard_seen = []
    old_callbacks = []

    def on_read(read_count):
        if read_count == 1:
            # Model BlueZ reporting that the existing backend connection vanished.
            backend.is_connected = False
        elif read_count >= 5:
            guard_seen.append(True)
            state.should_exit = True

    device = DisconnectingStartupBle(backend, on_read)
    factory_calls = 0

    def transport_factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            device.open()
            old_callbacks.append(backend.callbacks[LOG_CHARACTERISTIC_UUID])
            return device
        state.should_exit = True
        return None

    runner.run(state, transport_factory=transport_factory)

    assert factory_calls == 2  # startup, then one reconnect after backend loss
    assert guard_seen == []
    assert device.close_calls == 1
    assert not device.is_open
    assert not backend.is_connected
    assert len(old_callbacks) == 1
    stale_line = b"stale callback after backend loss\r\n"
    old_callbacks[0](
        LOG_CHARACTERISTIC_UUID,
        bytearray(frame(1, len(stale_line), 0, stale_line)),
    )
    assert device.read_line() is None  # the retired generation is fenced
    assert broker_factory.clients[0].disconnect_calls == 1


def test_startup_then_stats_poll_refreshes_watchdog_without_packet_counters():
    """A stats-command cycle refreshes BLE activity but is not a packet line."""
    config = make_test_state().config
    config["general"]["transport"] = "ble"
    config["ble"] = {"address": "02:00:00:00:00:01", "watchdog_timeout": 0.5}
    state = make_test_state(config=config)
    broker_factory = MainLoopBrokerFactory()
    state.mqtt_manager = MqttManager(state, client_factory=broker_factory)
    backend = StatsResponseGatt()
    observations = {}
    factory_calls = 0
    device = None

    def on_read(read_count):
        if read_count == 1:
            # Create an aged but real activity timestamp, then execute the same
            # GATT stats method used by background.stats_logging_loop.
            device._last_activity = time.time() - 2.0
            observations["age_before"] = device.seconds_since_activity()
            observations["stats"] = device.get_device_stats()
            observations["age_after"] = device.seconds_since_activity()
            observations["packet_counts"] = (
                state.stats["packets_rx"], state.stats["packets_tx"],
                state.stats["bytes_processed"],
            )
        elif read_count == 2:
            runner.handle_signal(state, 15, None)

    device = StatsPollStartupBle(backend, on_read)

    def transport_factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            device.open()
            return device
        state.should_exit = True
        return None

    runner.run(state, transport_factory=transport_factory)

    session_state = BleConnection._process_sessions.get(device.address.upper(), {})
    assert factory_calls == 1, (
        "stats poll did not keep the runner transport open: "
        f"stats_calls={device.stats_calls}; metrics={device.metrics}; "
        f"retirement_required={device._retirement_reconnect_required}; "
        f"cached_session_count={len(session_state.get('sessions', {}))}; "
        f"backend_connected={backend.is_connected}"
    )  # stats replies kept the 0.5s watchdog from firing
    assert device.stats_calls == 2  # startup snapshot plus one in-loop poll
    assert observations["age_before"] >= 1.5
    assert observations["age_after"] < 0.5
    assert observations["stats"]["observer_host"]["acked_records"] >= 3
    assert observations["packet_counts"] == (0, 0, 0)
    assert state.stats["packets_rx"] == state.stats["packets_tx"] == 0
    assert state.stats["bytes_processed"] == 0
    assert device.close_calls == 1
    assert not device.is_open
    assert not backend.is_connected
    assert not backend.callbacks
