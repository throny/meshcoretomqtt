"""Real-Paho characterization of the bridge's single reconnect owner.

Only a disposable loopback listener is contacted. No production configuration,
credentials, topics or MQTT publications are used.
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

from bridge.broker_client import PahoBrokerClient


def _connect_packet(sock: socket.socket) -> None:
    """Consume one bounded MQTT CONNECT packet at the external I/O boundary."""
    def read_exact(count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise EOFError("Incomplete synthetic CONNECT")
            data.extend(chunk)
        return bytes(data)

    assert read_exact(1) == b"\x10"
    remaining = 0
    for index in range(4):
        byte = read_exact(1)[0]
        remaining += (byte & 127) << (7 * index)
        if byte < 128:
            break
    else:
        raise AssertionError("Invalid synthetic CONNECT length")
    assert remaining <= 4096
    read_exact(remaining)


@contextmanager
def _dropping_broker():
    stop = threading.Event()
    drop_first = threading.Event()
    second_connect = threading.Event()
    accepted: list[socket.socket] = []
    workers: list[threading.Thread] = []
    errors: list[BaseException] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.1)

    def serve_connection(sock: socket.socket, index: int) -> None:
        try:
            sock.settimeout(2)
            _connect_packet(sock)
            sock.sendall(b"\x20\x02\x00\x00")  # MQTT 3.1.1 successful CONNACK
            if index > 0:
                second_connect.set()
            if index == 0:
                while not stop.is_set() and not drop_first.wait(0.05):
                    pass
            else:
                stop.wait(10)
        except (OSError, EOFError) as exc:
            if not stop.is_set():
                errors.append(exc)
        except BaseException as exc:
            errors.append(exc)
        finally:
            sock.close()

    def accept_connections() -> None:
        try:
            while not stop.is_set():
                try:
                    sock, _ = listener.accept()
                except socket.timeout:
                    continue
                accepted.append(sock)
                worker = threading.Thread(target=serve_connection, args=(sock, len(accepted) - 1))
                workers.append(worker)
                worker.start()
        except OSError as exc:
            if not stop.is_set():
                errors.append(exc)

    thread = threading.Thread(target=accept_connections)
    thread.start()
    try:
        yield listener.getsockname()[1], drop_first, second_connect
    finally:
        stop.set()
        thread.join(3)
        listener.close()
        for sock in accepted:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for worker in workers:
            worker.join(3)
        assert not thread.is_alive()
        assert all(not worker.is_alive() for worker in workers)
        assert not errors, errors


def test_only_explicit_replacement_connects_after_remote_drop():
    """Enabling Paho reconnect makes an unwanted second CONNECT fail this test."""
    connected = threading.Event()
    disconnected = threading.Event()
    replacement_connected = threading.Event()
    first = PahoBrokerClient(
        "loopback-owner-first",
        on_connect=lambda *_: connected.set(),
        on_disconnect=lambda *_: disconnected.set(),
    )
    replacement = PahoBrokerClient(
        "loopback-owner-replacement",
        on_connect=lambda *_: replacement_connected.set(),
    )
    with _dropping_broker() as (port, drop_first, second_connect):
        try:
            first.connect("127.0.0.1", port)
            first.loop_start()
            assert connected.wait(3), "Initial synthetic broker handshake failed"
            drop_first.set()
            assert disconnected.wait(3), "Remote closure was not observed"
            assert not second_connect.wait(2.5), "Retired Paho client reconnected without its manager"

            replacement.connect("127.0.0.1", port)
            replacement.loop_start()
            assert replacement_connected.wait(3), "Explicit fresh replacement did not connect"
            assert second_connect.is_set()
        finally:
            for client in (first, replacement):
                client.disconnect()
                client.loop_stop()
