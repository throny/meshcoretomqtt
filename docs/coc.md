# Receive repeater logs over Bluetooth

The optional CoC transport connects a Linux computer to a repeater without a USB
cable. It uses a Rust helper for Bluetooth and the usual meshcoretomqtt parser
and MQTT configuration. USB serial remains the default.

## Before you start

- A SenseCAP Solar / P1-Pro with the [CoC firmware](https://github.com/throny/meshcore-ble-observer/releases/tag/v1.18.0-coc).
- Linux with BlueZ, a Bluetooth adapter, and Rust/Cargo to build the helper.
- A repeater already enrolled with this host. The helper requires a bonded,
  trusted peer and an authenticated, encrypted connection; it does not pair devices.
  See [firmware enrollment](https://github.com/throny/meshcore-ble-observer/blob/main/docs/enrollment.md).
- A working meshcoretomqtt configuration, including your broker settings.

This is experimental and has been used on one P1-Pro/Linux setup. Other boards
and long-term reliability are not established.

## 1. Build and install the helper

Run from this checkout as an unprivileged user, on the computer that will receive
the logs. The first command downloads the Rust dependencies; the build is offline.

```sh
cargo fetch --locked --manifest-path coc-helper/Cargo.toml
cargo build --offline --locked --release --manifest-path coc-helper/Cargo.toml
```

The executable is `coc-helper/target/release/observer-coc-helper`. Install it at
the path configured below, owned by an administrator rather than the bridge's
service account. Give that account a private runtime directory (mode `0700`)
for the two sockets. If using `/run`, arrange for the directory to be recreated
after each boot. The normal installer does not install the helper or create this
directory.

## 2. Configure the connection

Merge these settings into your configuration. Edit an existing `[general]` table
rather than adding a duplicate one, and keep your broker settings. Replace the
example address with your repeater's enrolled Bluetooth address.

```toml
[general]
transport = "coc"
sync_time = true

[coc]
address = "02:00:00:00:00:01"
adapter = "hci0"
helper = "/opt/mctomqtt/bin/observer-coc-helper"
data_socket = "/run/mctomqtt-coc/data.sock"
control_socket = "/run/mctomqtt-coc/control.sock"
```

## 3. Run the bridge

Stop any other receiver using this repeater, then run your configured bridge:

```sh
python3 mctomqtt.py --config /path/to/your/config.toml
```

Check that the connection succeeds and that packets reach your configured broker.
The bridge launches the helper; do not start a second helper manually. A failed
Bluetooth connection does not fall back to USB or another repeater.

## Clock and delivery behavior

With `sync_time = true`, startup and reconnect restore UTC only after the host's
NTP state is verified and the radio acknowledges the command. Failures are logged
and retried on the next connection. Explicit `false` disables clock writes.

Received records are buffered in RAM before being acknowledged. A process crash
or broker outage can lose records; acknowledgements are not proof of MQTT delivery.

## If it will not connect

Check the helper path and executable permissions, the adapter name, the enrolled
peer address, and ownership/mode of the runtime directory. Existing socket paths
are not silently deleted: first confirm no receiver is running before removing
stale sockets. BlueZ/platform compatibility beyond the tested setup is unverified.
