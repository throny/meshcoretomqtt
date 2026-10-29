# CoC setup (experimental, Linux)

Use the matching two-channel repeater firmware and an already enrolled, pinned
Bluetooth peer. The helper checks bonded/trusted state and authenticated,
encrypted links. It does not scan, pair, change trust or flash firmware.

Build the helper for the host's architecture, as an unprivileged user:

```sh
cargo fetch --locked --manifest-path coc-helper/Cargo.toml
cargo build --offline --locked --release --manifest-path coc-helper/Cargo.toml
```

Install the executable somewhere the service account cannot modify. Give that
account a private runtime directory (mode 0700) for its two IPC sockets. The
normal installer does not perform these steps.

Add to the existing configuration; keep its broker settings:

```toml
[general]
transport = "coc"
sync_time = true

[coc]
address = "02:00:00:00:00:01" # Replace this synthetic address with the pinned peer.
adapter = "hci0"
helper = "/opt/mctomqtt/bin/observer-coc-helper"
data_socket = "/run/mctomqtt-coc/data.sock"
control_socket = "/run/mctomqtt-coc/control.sock"
```

Stop any other receiver for that radio before an authorized cutover. CoC never
falls back to USB or a different peer. Existing IPC paths are not blindly deleted.

With `sync_time = true`, startup and reconnect restore UTC only after the host's
NTP state is verified and the radio acknowledges the command. Failures are logged
and retried on the next connection. Explicit `false` disables clock writes.

Records enter a bounded process-memory queue before receipt. A crash can lose
acknowledged records before MQTT publication; there is no persistent outbox or
guaranteed delivery. Expired queued commands are discarded, but a command already
in flight cannot be recalled. The current helper includes a bounded read-only
service-discovery workaround; wider BlueZ/platform support still needs testing.
