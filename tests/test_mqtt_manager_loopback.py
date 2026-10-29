"""Manager recovery with real Paho and only synthetic loopback broker I/O."""
import time

from bridge.mqtt_manager import MqttManager
from bridge.state import BridgeState
from test_mqtt_reconnect_owner import _dropping_broker


def test_manager_replaces_dropped_client_and_restores_connected_state():
    with _dropping_broker() as (port, drop_first, second_connect):
        state = BridgeState({
            "general": {"iata": "TST"},
            "remote_serial": {"enabled": False},
            "broker": [{
                "name": "loopback",
                "enabled": True,
                "server": "127.0.0.1",
                "port": port,
                "transport": "tcp",
                "auth": {"method": "none"},
            }],
        })
        state.repeater_name = "synthetic-loopback"
        state.repeater_pub_key = "00" * 32
        manager = MqttManager(state)
        try:
            assert manager.connect_all_brokers()
            first = state.mqtt_clients[0]["client"]
            drop_first.set()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                manager.reconnect_disconnected_brokers()
                if second_connect.is_set() and state.mqtt_clients[0]["connected"]:
                    break
                time.sleep(0.01)
            assert second_connect.is_set(), "Manager did not establish replacement connection"
            assert len(state.mqtt_clients) == 1
            assert state.mqtt_clients[0]["client"] is not first
            assert state.mqtt_clients[0]["connected"] is True
            assert state.mqtt_connected is True
            assert len(state.stats["reconnects"][0]) == 1
        finally:
            state.should_exit = True
            for info in state.mqtt_clients:
                info["client"].disconnect()
                info["client"].loop_stop()
