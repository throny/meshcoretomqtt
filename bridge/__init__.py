"""MeshCore to MQTT bridge package."""
from __future__ import annotations

from typing import Any

from .state import BridgeState
from .mqtt_manager import MqttManager
from . import runner


class MeshCoreBridge:
    """Facade: creates BridgeState, wires up MqttManager, exposes run() and handle_signal()."""

    def __init__(self, config: dict[str, Any], debug: bool = False, version: str = "0.0.0",
                 *, transport_factory=None, broker_client_factory=None) -> None:
        self.state = BridgeState(config, debug)
        self.state.client_version = runner.load_client_version(version)
        self._transport_factory = transport_factory
        if broker_client_factory is None:
            self.state.mqtt_manager = MqttManager(self.state)
        else:
            self.state.mqtt_manager = MqttManager(self.state, client_factory=broker_client_factory)

    def run(self) -> None:
        if self._transport_factory is None:
            runner.run(self.state)
        else:
            runner.run(self.state, transport_factory=self._transport_factory)

    def handle_signal(self, signum: int, frame: Any) -> None:
        runner.handle_signal(self.state, signum, frame)
