"""Contributor-rule guard for new BLE tests, not inherited upstream tests."""
import ast
import asyncio
from pathlib import Path
import pytest


ROOT = Path(__file__).parent
NEW_TESTS = (
    "test_transport_parity.py", "test_transport_config.py",
    "test_startup_cleanup.py", "test_observer_ack_protocol.py",
    "test_ble_connection.py", "helpers/offline_service.py",
    "test_installer_system.py",
)


def test_new_ble_tests_use_injected_fakes_not_module_patches():
    violations = []
    for name in NEW_TESTS:
        for node in ast.walk(ast.parse((ROOT / name).read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("unittest.mock"):
                violations.append((name, node.lineno, "mock import"))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "monkeypatch"
                    and node.func.attr in {"setattr", "setitem", "delattr"}):
                violations.append((name, node.lineno, "module/object patch"))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Attribute):
                        continue
                    while isinstance(target, ast.Attribute):
                        target = target.value
                    if isinstance(target, ast.Name) and target.id in {"runner", "bridge"}:
                        violations.append((name, node.lineno, "core module reassignment"))
    assert not violations, violations


@pytest.mark.parametrize("boundary", ["dispatch", "gatt", "broker"])
def test_explicit_falsey_factory_does_not_fall_back_to_real_io(boundary):
    from bridge import runner
    from bridge.ble_connection import BleConnection
    from bridge.mqtt_manager import MqttManager
    from tests.fakes import FakeSerialConnection, FakeBrokerClient, make_test_state
    from test_ble_connection import FakeGatt

    class Factory:
        def __init__(self, result): self.result = result
        def __bool__(self): return False
        def __call__(self, *args, **kwargs): return self.result

    if boundary == "dispatch":
        device = FakeSerialConnection()
        # Even the defective fallback has no ports to open in this regression.
        result = runner.connect_transport({"serial": {"ports": []}}, serial_factory=Factory(device))
        assert result is device
    elif boundary == "gatt":
        backend = FakeGatt()
        connection = BleConnection("02:00:00:00:00:01", client_factory=Factory(backend))
        # Construction only: never call the real backend's connect method.
        assert asyncio.run(connection._create_client()) is backend
    else:
        client = FakeBrokerClient()
        state = make_test_state()
        state.repeater_pub_key = "AA" * 32
        manager = MqttManager(state, client_factory=Factory(client))
        assert manager._create_broker_client(0) is client
