"""BLE carriage must not alter the inherited parser/publisher behavior."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from bridge.ble_connection import BleConnection
from bridge.message_parser import parse_and_publish
from bridge.mqtt_publish import publish_status
from tests.fakes import FakeBrokerClient, make_test_state
from bridge.mqtt_manager import MqttManager
from bridge.state import BridgeState
from auth_token import decode_token_payload
from bridge.auth_provider import MeshCoreAuthProvider
from tests.test_auth_token import TestAuthToken

from test_ble_connection import FakeGatt, frame
from test_ble_connection import _connections, close_ble_connections_after_test


def canonical_publications(messages, before, after):
    """Compare invariant payloads while validating real, unfrozen UTC stamps."""
    result = []
    for topic, payload, qos, retain in messages:
        message = json.loads(payload)
        stamp = datetime.fromisoformat(message.pop("timestamp"))
        assert stamp.utcoffset() == timedelta(0)
        assert before <= stamp <= after
        result.append((topic, message, qos, retain))
    return result


def test_ble_carried_classic_raw_rx_tx_fixtures_publish_the_same_payloads_and_origin():
    before = datetime.now(timezone.utc)
    broker = FakeBrokerClient()
    state = make_test_state(broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}])
    state.repeater_name = "SenseCAP"
    state.repeater_pub_key = "AA" * 32
    fake = FakeGatt()
    connection = BleConnection("02:00:00:00:00:01", client_factory=lambda _: fake)
    _connections.append(connection)
    connection.open()
    classic = [
        b"12:00:00 - 1/1/2026 U RAW: AABB\n",
        b"12:00:00 - 1/1/2026 U: RX, len=2 (type=1, route=D, payload_len=0) SNR=4 RSSI=-90 score=100 time=2 hash=ABCD [02 -> 01]\n",
        b"12:00:01 - 1/1/2026 U: TX, len=2 (type=1, route=F, payload_len=0) [02 -> 01]\n",
    ]
    for record_id, line in enumerate(classic, 1):
        fake.emit(frame(record_id, len(line), 0, line))
        received = connection.read_line()
        assert received == line.decode().rstrip("\r\n"), connection.metrics
        parse_and_publish(state, received)

    published = [json.loads(item[1]) for item in broker.published]
    assert [payload["direction"] for payload in published] == ["rx", "tx"]
    assert published[0]["raw"] == "AABB"
    assert published[1]["raw"] == "AABB"  # classic TX has no raw frame
    assert published[0]["origin"] == "SenseCAP"
    assert published[0]["origin_id"] == "AA" * 32
    connection.close()

    # The same byte-identical serial fixtures through the inherited parser yield
    # identical topics, JSON objects, QoS and retain settings.
    serial_broker = FakeBrokerClient()
    serial_state = make_test_state(broker_clients=[{"client": serial_broker, "broker_idx": 0, "connected": True}])
    serial_state.repeater_name = "SenseCAP"
    serial_state.repeater_pub_key = "AA" * 32
    for line in classic:
        parse_and_publish(serial_state, line.decode().strip())
    after = datetime.now(timezone.utc)
    assert canonical_publications(broker.published, before, after) == canonical_publications(serial_broker.published, before, after)


def test_status_topic_data_keeps_iata_and_repeater_identity_for_ble_transport():
    def make_status_state(broker, transport):
        state = make_test_state(
            config={
                "general": {"iata": "SEA", "transport": transport},
                "topics": {"status": "meshcore/{IATA}/{PUBLIC_KEY}/status"},
            },
            broker_clients=[{"client": broker, "broker_idx": 0, "connected": True}],
        )
        state.repeater_name = "SenseCAP"
        state.repeater_pub_key = "BB" * 32
        return state

    ble_broker, serial_broker = FakeBrokerClient(), FakeBrokerClient()
    ble_state, serial_state = make_status_state(ble_broker, "ble"), make_status_state(serial_broker, "serial")
    before = datetime.now(timezone.utc)
    publish_status(ble_state, "online")
    publish_status(serial_state, "online")
    after = datetime.now(timezone.utc)
    assert canonical_publications(ble_broker.published, before, after) == canonical_publications(serial_broker.published, before, after)
    assert ble_broker.published[0][0] == "meshcore/SEA/" + "BB" * 32 + "/status"
    assert json.loads(ble_broker.published[0][1])["origin"] == "SenseCAP"
    assert json.loads(ble_broker.published[0][1])["origin_id"] == "BB" * 32


def test_token_auth_claims_and_audience_flow_matches_existing_host_generator():
    config = {
        "general": {"iata": "SEA"},
        "broker": [{"name": "letsmesh-us", "enabled": True,
                    "server": "mqtt-us-v1.letsmesh.net", "port": 443,
                    "tls": {"enabled": True, "verify": True},
                    "auth": {"method": "token", "audience": "mqtt-us-v1.letsmesh.net",
                             "owner": "owner-id", "email": "USER@example.com"}}],
    }
    state = BridgeState(config)
    state.repeater_name = "SenseCAP"
    state.repeater_pub_key = TestAuthToken.public_key
    state.repeater_priv_key = TestAuthToken.private_key
    state.client_version = "meshcoretomqtt/test"
    state.auth = MeshCoreAuthProvider()
    manager = MqttManager(state)
    username, token = manager._generate_auth_credentials(0)
    claims = decode_token_payload(token)
    assert username == f"v1_{state.repeater_pub_key.upper()}"
    assert claims["aud"] == "mqtt-us-v1.letsmesh.net"
    assert claims["owner"] == "owner-id"
    assert claims["email"] == "user@example.com"
    assert claims["client"] == "meshcoretomqtt/test"
    assert claims["publicKey"] == state.repeater_pub_key.upper()
    assert "private" not in json.dumps(claims).lower()
    assert state.repeater_priv_key not in token
