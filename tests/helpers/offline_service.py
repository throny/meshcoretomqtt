"""Test-only external I/O boundary for subprocess entrypoint lifecycle checks.

No production hook, installer, account, Bluetooth stack, broker or service manager.
"""
from pathlib import Path
import os
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def deny_external_io(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
        raise RuntimeError("forbidden external I/O")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
        if path.startswith(("/dev/tty", "/dev/rfcomm", "/run/dbus", "/var/run/dbus")):
            raise RuntimeError("forbidden external I/O")


sys.addaudithook(deny_external_io)

from bridge import runner
import bridge
from tests.fakes import FakeSerialConnection, FakeBrokerClient

config_path, events_path = sys.argv[1:]
events = Path(events_path)


def record(event):
    with events.open("a") as output:
        output.write(event + "\n")


class OfflineTransport(FakeSerialConnection):
    def __init__(self):
        super().__init__(privkey="cd" * 64)
        self.ready = False

    def read_line(self):
        if not self.ready:
            self.ready = True
            record("ready")
        return None

    def close(self):
        super().close()
        record("transport-closed")


class OfflineBroker(FakeBrokerClient):
    def publish(self, topic, payload, qos=0, retain=False):
        import json
        if json.loads(payload).get("status") == "offline":
            record("offline-status")
        return super().publish(topic, payload, qos, retain)

    def loop_stop(self):
        record("broker-loop-stopped")

    def disconnect(self):
        super().disconnect()
        record("broker-disconnected")


class OfflineManager:
    def __init__(self, state):
        self.state = state

    def connect_all_brokers(self):
        self.state.mqtt_connected = True
        self.state.token_cache[0] = {"token": "SENTINEL-token-not-for-logs"}
        self.state.mqtt_clients = [{"client": OfflineBroker(), "broker_idx": 0, "connected": True}]
        return True

    def reconnect_disconnected_brokers(self):
        pass

    def stop_websocket_ping_thread(self, broker_idx):
        pass


if os.getuid() == 0:
    raise RuntimeError("fixture requires an unprivileged caller")
record(f"identity={os.getuid()}:{os.getgid()}")


def connect_ble(config):
    record("transport-selected=ble")
    if os.environ.get("MCTOMQTT_FIXTURE_UNAVAILABLE") == "1":
        return None
    return OfflineTransport()


def forbid_serial(config):
    raise RuntimeError("serial transport forbidden in BLE lifecycle fixture")


runner.ble_connection.connect = connect_ble
runner.serial_connection.connect = forbid_serial
bridge.MqttManager = OfflineManager
sys.argv = [str(ROOT / "mctomqtt.py"), "--debug", "--config", config_path]
runpy.run_path(str(ROOT / "mctomqtt.py"), run_name="__main__")
record("exited")
