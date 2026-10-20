"""The BLE private-key path must not disclose private material in logs/config."""
from __future__ import annotations

import io
import logging

from bridge.ble_connection import COMMAND_CHARACTERISTIC_UUID

from test_ble_connection import FakeGatt, frame, make_connection, close_ble_connections_after_test


def test_private_key_is_returned_to_memory_but_not_logged(caplog):
    key = "cd" * 64
    fake = FakeGatt()
    connection = make_connection(fake)
    connection.open()
    original_write = fake.write_gatt_char

    async def write_and_reply(uuid, value, response=True):
        await original_write(uuid, value, response)
        if uuid != COMMAND_CHARACTERISTIC_UUID:
            return
        response_bytes = f"-> >{key}\r\n".encode()
        fake.emit(frame(1, len(response_bytes), 0, response_bytes))

    fake.write_gatt_char = write_and_reply
    # Exercise a real formatted output stream like the unit's journal-bound
    # stdout/stderr, without starting journald or installing a service.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            assert connection.get_privkey() == key
            logging.getLogger("bridge").info("Key read completed (contents omitted)")
    finally:
        root.removeHandler(handler)
        handler.close()

    assert key not in caplog.text
    assert "Key read completed" in stream.getvalue()  # capture is not vacuous
    assert key not in stream.getvalue()
