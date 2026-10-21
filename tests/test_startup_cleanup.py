"""Outage/initialization cleanup: real runner and BLE lifecycle, fake I/O only."""
from __future__ import annotations

import pytest

from bridge import runner
from bridge.ble_connection import BleConnection
from tests.fakes import CallbackBrokerClient, FakeSerialConnection, make_test_state
from bridge.mqtt_manager import MqttManager
from test_ble_connection import FakeGatt


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
