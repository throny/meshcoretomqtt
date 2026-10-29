"""Test-only external I/O boundary for subprocess entrypoint lifecycle checks.

No production hook, installer, account, Bluetooth stack, broker or service manager.
"""
from pathlib import Path
import os
import sys
import time

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
from bridge import MeshCoreBridge
from mctomqtt import main
from tests.fakes import FakeSerialConnection, CallbackBrokerClient

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


class OfflineBroker(CallbackBrokerClient):
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


def construct_bridge(config, debug, version):
    def transport_factory(config):
        return runner.connect_transport(config, serial_factory=forbid_serial, ble_factory=connect_ble)

    app = MeshCoreBridge(config, debug=debug, version=version,
                         transport_factory=transport_factory, broker_client_factory=OfflineBroker)
    app.state.token_cache[0] = ("SENTINEL-token-not-for-logs", time.time())
    return app


main(["--debug", "--config", config_path], bridge_factory=construct_bridge)
record("exited")
