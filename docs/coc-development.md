# Experimental LE L2CAP CoC observer

CoC means **Credit-Based Connection-Oriented Channels** in Bluetooth LE's
L2CAP layer. This uses standard Nordic S140 and Linux/BlueZ channel transport;
the observer discovery, application framing and receipts remain experimental
MeshCore-specific interfaces, not an official Bluetooth or MeshCore profile.

## Scope and compatibility

- Select `general.transport = "coc"` explicitly; USB serial remains the default.
- Linux/BlueZ, a matching two-channel repeater image, and the Rust helper are
  required. Stock repeater/Companion firmware does not implement this interface.
- The existing `ble` backend is legacy GATT compatibility, not a fallback.
  Never run both receivers against the same radio concurrently.
- The helper requires an already enrolled/pinned peer, paired/trusted/bonded and
  unblocked BlueZ state, authenticated discovery and High-security encrypted
  CoC links. It does not scan, pair, clear caches or change trust/bonds.
- Kernel-reported key size zero is unknown, not proof of a 16-byte key. Admission
  uses the approved authenticated/High-security policy; it does not infer a key size.
- The helper waits for BlueZ service resolution. A bounded read-only `gatttool`
  discovery is currently used when the bonded cache omits the exact characteristic.
  That compatibility workaround is a portability/upstream-review limitation.

## Manual preparation

Build as an unprivileged user using Rust and a C toolchain. Dependency fetching
is an explicit setup action; subsequent builds/tests can run offline:

```sh
cargo fetch --locked --manifest-path coc-helper/Cargo.toml
cargo build --offline --locked --release --manifest-path coc-helper/Cargo.toml
cargo test --offline --locked --manifest-path coc-helper/Cargo.toml
```

Install the resulting helper for the **host's architecture**, not an x86 binary
on an ARM host. Keep executable/code files non-writable by the service account.
The normal installer does not provision this helper or Bluetooth enrollment.

Example configuration (synthetic peer; replace with the pinned peer):

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
startup_timeout = 20
queue_limit = 128
```

Have the supervisor create a private service-owned runtime directory, with
mode 0700, before startup. Helper sockets are mode 0600. Existing paths are
never blindly removed/reused. Both paths must be distinct and absolute.
Stop the old receiver before an explicitly authorized cutover; do not restart
Bluetooth, re-pair or flash as an attempted workaround for a connection failure.

With `sync_time = true`, startup and every successful CoC reconnect synchronize
the radio using UTC epoch seconds only after the host NTP property is verified.
The bridge checks the device's positive clock reply. Failures are logged and
retried at the next connection, without claiming that synchronization succeeded.
`false` is an explicit opt-out. Serial/GATT retain their existing startup policy.

## Wire and receipt contract

The authenticated discovery descriptor selects separate data and control PSMs;
both channels must become READY before log records flow. Current SDU MTU is 558
bytes: a 14-byte little-endian `<BBQI>` header (version, kind, session, ID) plus
at most 544 record bytes. This is not the legacy 16-byte GATT fragment format.
Transport credits are not application receipts. Python admits a complete record
to a bounded in-process queue, then sends its receipt. Reconnect deduplication is
bounded and process-local; host crashes can lose acknowledged records before MQTT.
No persistent outbox, guaranteed broker receipt or lossless backfill is claimed.

Data/control use independent local IPC channels so stalled data does not own the
control-channel queue. Requests are allowlisted and correlated with reply IDs.
No arbitrary radio, identity, firmware or general administrative commands are exposed.

Concurrent command callers allocate/admit request IDs atomically. A queued
command that expires before the writer claims it is discarded, including clock
writes. Once claimed for transmission it is in flight and cannot be recalled;
a later timeout is not evidence that the device did not execute it.

## Validation limits

Offline tests cover real Unix IPC/processes, frame limits, reconnect/session
retirement, command normalization, serial/MQTT parser parity, clock policy and
Rust security/configuration boundaries. They do not certify actual BlueZ or radio
behavior. A single P1-Pro/Linux deployment is field evidence, not general support.
RF coexistence, power, recovery, sustained reconnects and downstream delivery
remain independent acceptance gates. CI network/API failures are not radio tests.
