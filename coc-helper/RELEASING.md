# Building helper releases

## Build locally

Most users should use a prebuilt helper. For development or an unsupported host,
install Rust 1.90 or newer and native C build tools, then run from the repository
root as an unprivileged user:

```sh
cargo fetch --locked --manifest-path coc-helper/Cargo.toml
cargo test --offline --locked --manifest-path coc-helper/Cargo.toml
cargo build --offline --locked --release --manifest-path coc-helper/Cargo.toml
```

The executable is `coc-helper/target/release/observer-coc-helper`. The first command
downloads dependencies; the remaining commands use the locked, cached inputs.
Build on a compatible target system; a binary from a newer glibc installation
may not run on an older host.

## Release layout

Helper releases are separate from repeater firmware. The installer expects a
fixed tag such as `coc-helper-v0.1.0`, with these raw executables and one combined
`SHA256SUMS` file:

```text
observer-coc-helper-x86_64
observer-coc-helper-aarch64
observer-coc-helper-armv7
SHA256SUMS
```

`armv7` is 32-bit ARM hard-float. The candidate workflow builds all three on
Debian Bookworm with the locked Cargo dependencies and Rust 1.90.0. These builds
target glibc-based Linux, not Alpine/musl. Docker's native/emulated architecture
selection supplies the matching C toolchain for the vendored D-Bus dependency.

## Candidate workflow

Run **Build CoC helper candidates** from a reviewed commit. It tests the helper,
builds release binaries as an unprivileged container user, validates their ELF
architecture and uploads one artifact per platform. It has read-only repository
permissions and does **not** publish a release.

The Docker image has a fixed version tag; it is not an immutable/reproducible
toolchain pin. Do not describe these builds as byte-reproducible. GitHub Actions
dependencies are pinned to reviewed commit SHAs.

## Assemble and verify

Download and unpack the three build artifacts into separate directories. Using
the matching source checkout, run the packager for each binary into one fresh
output directory:

```sh
python3 coc-helper/package_release.py --machine x86_64 \
  --binary /path/to/x86_64/observer-coc-helper-x86_64 --output helper-release
python3 coc-helper/package_release.py --machine aarch64 \
  --binary /path/to/aarch64/observer-coc-helper-aarch64 --output helper-release
python3 coc-helper/package_release.py --machine armv7l \
  --binary /path/to/armv7l/observer-coc-helper-armv7 --output helper-release
(cd helper-release && sha256sum --check SHA256SUMS)
```

The packager checks architecture and refuses to overwrite an existing binary.
It regenerates the checksum file over all assembled helper assets. Review the
source version, outgoing Git identity, executable content and privacy scan
before publishing. Test installation and reception on each supported host
architecture; an emulated unit test is not Bluetooth hardware acceptance.

Publish only validated architectures and list any missing ones in the release
notes. Do not replace existing release assets silently. Checksum verification
establishes download integrity, not independent provenance or publisher signing.

No helper release is currently published by this work. The workflow and local
x86_64 candidate still need multi-platform execution/acceptance before promotion.
