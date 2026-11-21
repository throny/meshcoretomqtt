"""Read-only preflight check for the Bluetooth (CoC) transport.

Run with ``python3 -m installer coc-doctor``. Nothing here changes system
state: every external command is a query and goes through one runner function
(``run_command``) so that tests can replace it. No network access and no root
privileges are required, but some files are only readable by the service user; a check that
cannot be performed as the current user is reported as WARN.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import socket
import stat as stat_module
import struct
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

MIN_BLUEZ = (5, 72)
DEFAULT_CONFIG_DIR = "/etc/mctomqtt"
DEFAULT_UNIT = "mctomqtt.service"
# Keep in sync with bridge/coc_connection.py validate_coc_config().
_ADDRESS_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")
_ADAPTER_RE = re.compile(r"hci[0-9]{1,3}\Z")
# machine -> (ELF class, ELF e_machine)
ELF_ARCHES = {
    "x86_64": (2, 62), "amd64": (2, 62),
    "aarch64": (2, 183), "arm64": (2, 183),
    "armv7l": (1, 40), "armv8l": (1, 40),
}


@dataclass
class CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    found: bool = True  # False when the program does not exist or cannot run


@dataclass
class Result:
    status: str
    name: str
    message: str
    hint: str = ""
    perm_skipped: bool = False  # not performed, or not authoritative, because of permissions


def run_command(cmd: list[str], timeout: float = 10.0) -> CmdResult:
    """The only place that starts a process. Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return CmdResult(127, "", f"{cmd[0]}: not found", found=False)
    except PermissionError:
        return CmdResult(126, "", f"{cmd[0]}: permission denied", found=False)
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"{cmd[0]}: timed out", found=False)
    except OSError as exc:
        return CmdResult(126, "", f"{cmd[0]}: {exc.strerror or exc}", found=False)
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _read_bytes(path: str, count: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(count)
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def _access_denied(path: str) -> bool:
    """True when `path` cannot be inspected because of permissions (not because it is absent)."""
    try:
        os.stat(path)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError, AttributeError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _lookup_user(name: str):
    """Return (uid, set of gids) for an account, or None."""
    try:
        import grp  # noqa: F401  (POSIX only)
        import pwd
        entry = pwd.getpwnam(name)
        return entry.pw_uid, set(os.getgrouplist(name, entry.pw_gid))
    except (ImportError, KeyError, OSError):
        return None


def _can_open_l2cap() -> tuple[bool, str]:
    family = getattr(socket, "AF_BLUETOOTH", None)
    proto = getattr(socket, "BTPROTO_L2CAP", None)
    if family is None or proto is None:
        return False, "this Python build has no Bluetooth socket support"
    try:
        sock = socket.socket(family, socket.SOCK_SEQPACKET, proto)
    except OSError as exc:
        return False, exc.strerror or str(exc)
    sock.close()
    return True, ""


@dataclass
class Env:
    """Everything the checks read from the machine, replaceable in tests."""
    run: Callable[[list[str]], CmdResult] = run_command
    system: str = field(default_factory=platform.system)
    machine: str = field(default_factory=lambda: platform.machine().lower())
    exists: Callable[[str], bool] = os.path.exists
    read_bytes: Callable[[str, int], bytes | None] = _read_bytes
    read_text: Callable[[str], str | None] = _read_text
    stat: Callable[[str], Any] = _stat
    access_denied: Callable[[str], bool] = _access_denied
    user: str = field(default_factory=_current_user)
    lookup_user: Callable[[str], Any] = _lookup_user
    can_open_l2cap: Callable[[], tuple[bool, str]] = _can_open_l2cap
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else -1)
    gids: set = field(default_factory=lambda: set(os.getgroups()) if hasattr(os, "getgroups") else set())


# --------------------------------------------------------------------------
# Configuration (mirrors config_loader.load_config; stdlib only)
# --------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigUnreadable(ValueError):
    """A config file or directory exists but this user may not read it."""

    def __init__(self, path: Any, message: str):
        super().__init__(message)
        self.path = str(path)


def load_config(config_dir: Path, files: list[str] | None = None) -> tuple[dict, list[Path]]:
    """Merge config.toml and config.d/*.toml. Raises ValueError with the file name."""
    if files:
        paths = [Path(item) for item in files]
    else:
        paths = []
        base = config_dir / "config.toml"
        drop_in = config_dir / "config.d"
        try:
            if base.is_file():
                paths.append(base)
            if drop_in.is_dir():
                paths.extend(sorted(drop_in / name for name in os.listdir(drop_in) if name.endswith(".toml")))
        except PermissionError as exc:
            raise ConfigUnreadable(exc.filename or config_dir, f"{exc.filename or config_dir}: permission denied") from None
    config: dict = {}
    for path in paths:
        try:
            with open(path, "rb") as handle:
                config = _deep_merge(config, tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path}: {exc}") from None
        except PermissionError:
            raise ConfigUnreadable(path, f"{path}: permission denied") from None
        except OSError as exc:
            raise ValueError(f"{path}: {exc.strerror or exc}") from None
    return config, paths


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def check_bluez(env: Env) -> Result:
    name = "BlueZ"
    if env.system != "Linux":
        return Result(FAIL, name, f"{env.system} is not supported; the CoC transport needs Linux with BlueZ",
                      "Run the bridge on a Linux host with BlueZ and systemd.")
    res = env.run(["bluetoothctl", "--version"])
    if not res.found:
        return Result(FAIL, name, "bluetoothctl not found; BlueZ is not installed",
                      "Install it, for example: sudo apt install bluez")
    version = _version_tuple(res.stdout + res.stderr)
    if res.returncode != 0 or version is None:
        return Result(WARN, name, "bluetoothctl did not report a version",
                      "Check that 'bluetoothctl --version' works for this user.")
    text = ".".join(str(part) for part in version)
    if version < MIN_BLUEZ:
        want = ".".join(str(part) for part in MIN_BLUEZ)
        return Result(WARN, name, f"BlueZ {text} is older than {want}; older releases crashed in field use",
                      f"Upgrade BlueZ to {want} or newer (newer Raspberry Pi OS or Debian release).")
    return Result(PASS, name, f"BlueZ {text}")


def _controller_block(env: Env, adapter: str) -> tuple[str | None, str]:
    """Return (`bluetoothctl show` text, note) for the named adapter, if resolvable."""
    address = (env.read_text(f"/sys/class/bluetooth/{adapter}/address") or "").strip()
    cmd = ["bluetoothctl", "show"] + ([address] if _ADDRESS_RE.fullmatch(address) else [])
    res = env.run(cmd)
    if not res.found or res.returncode != 0 or "Powered:" not in res.stdout:
        return None, res.stderr.strip() or "bluetoothctl show gave no controller data"
    return res.stdout, ""


def check_adapter(env: Env, adapter: str) -> Result:
    name = f"Adapter {adapter}"
    hint_up = "Check the adapter is plugged in and not blocked: rfkill list; sudo rfkill unblock bluetooth."
    if env.system != "Linux":
        return Result(WARN, name, "cannot check outside Linux", "Run the doctor on the Linux host.")
    present = env.exists(f"/sys/class/bluetooth/{adapter}")
    if not present and not env.exists("/sys/class/bluetooth"):
        return Result(FAIL, name, "no Bluetooth adapter is visible to the kernel", hint_up)
    if not present:
        return Result(FAIL, name, f"{adapter} does not exist; the kernel lists a different adapter name",
                      f"Set coc.adapter to an existing adapter (ls /sys/class/bluetooth), or fix {adapter}.")
    block, note = _controller_block(env, adapter)
    if block is None:
        return Result(WARN, name, f"{adapter} exists but its powered state cannot be read ({note})",
                      "Start bluetooth.service, then run 'bluetoothctl show'.")
    match = re.search(r"Powered:\s*(\w+)", block)
    if not match or match.group(1).lower() != "yes":
        return Result(FAIL, name, f"{adapter} is present but not powered",
                      "Run: bluetoothctl power on (and sudo rfkill unblock bluetooth if it stays off).")
    return Result(PASS, name, f"{adapter} present and powered")


def check_bluetooth_service(env: Env) -> Result:
    name = "bluetooth.service"
    hint = "Run: sudo systemctl enable --now bluetooth"
    enabled = env.run(["systemctl", "is-enabled", "bluetooth.service"])
    active = env.run(["systemctl", "is-active", "bluetooth.service"])
    if not enabled.found and not active.found:
        return Result(WARN, name, "systemctl is not available; cannot check the service",
                      "Make sure bluetoothd starts at boot on this system.")
    state = enabled.stdout.strip() or "unknown"
    running = active.stdout.strip() or "unknown"
    problems = []
    if state in ("disabled", "masked", "masked-runtime", "not-found"):
        problems.append(f"not enabled at boot ({state})")
    elif state not in ("enabled", "enabled-runtime", "alias", "static", "indirect", "generated"):
        problems.append(f"enablement unknown ({state})")
    if running != "active":
        problems.append(f"not running ({running})")
    if problems:
        status = FAIL if any(p.startswith("not ") for p in problems) else WARN
        return Result(status, name, "; ".join(problems), hint)
    return Result(PASS, name, f"enabled ({state}) and active")


def _unit_properties(env: Env, unit: str) -> dict[str, str] | None:
    res = env.run(["systemctl", "show", unit, "-p", "LoadState", "-p", "User", "-p", "Wants",
                   "-p", "Requires", "-p", "BindsTo", "-p", "After", "-p", "RuntimeDirectory", "-p", "NeedDaemonReload"])
    if not res.found or res.returncode != 0:
        return None
    props = {}
    for line in res.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def check_unit_ordering(unit: str, props: dict[str, str] | None) -> Result:
    name = f"{unit} ordering"
    if props is None:
        return Result(WARN, name, "cannot read the unit (no systemctl); skipped",
                      "Check by hand: systemctl show " + unit + " -p Wants -p After")
    if props.get("LoadState", "not-found") != "loaded":
        return Result(WARN, name, "no systemd unit by this name is installed; if the bridge is not run as a service, "
                                  "it will not start after a reboot", "Re-run with --unit <name> if the service has another name.")
    if props.get("NeedDaemonReload") == "yes":
        return Result(WARN, name, "the unit files changed on disk and systemd has not reloaded them yet",
                      "Run: sudo systemctl daemon-reload, then run the doctor again.")
    wants = {dep for key in ("Wants", "Requires", "BindsTo") for dep in props.get(key, "").split()}
    after = set(props.get("After", "").split())
    if "bluetooth.service" not in wants:
        detail = "has After=bluetooth.service but no Wants=" if "bluetooth.service" in after else "has no dependency on bluetooth.service"
        return Result(FAIL, name, f"{detail}; after a reboot the bridge can start without Bluetooth and restart-loop",
                      f"Add to the unit (or a drop-in): [Unit] Wants=bluetooth.service and After=bluetooth.service. "
                      f"The 'configure-coc' command does this.")
    if "bluetooth.service" not in after:
        return Result(WARN, name, "Wants=bluetooth.service is set but After=bluetooth.service is missing",
                      "Add After=bluetooth.service to the unit or a drop-in.")
    return Result(PASS, name, "Wants= and After= bluetooth.service present")


def check_config(env: Env, config_dir: Path, files: list[str] | None) -> tuple[list[Result], dict | None]:
    """Return the config results and the validated [coc] table (None when unusable)."""
    results: list[Result] = []
    try:
        config, paths = load_config(config_dir, files)
    except ConfigUnreadable as exc:
        return [Result(WARN, "Config", f"cannot read {exc.path} as user {env.user}; re-run with sudo or as the service user",
                       "Run: sudo -u <service user> python3 -m installer coc-doctor (or use sudo).",
                       perm_skipped=True)], None
    except ValueError as exc:
        return [Result(FAIL, "Config", f"does not parse: {exc}", "Fix the TOML syntax in that file.")], None
    if not paths:
        return [Result(FAIL, "Config", f"no configuration found under {config_dir}",
                       "Install the bridge first (install.sh) and create config.d/99-user.toml.")], None
    results.append(Result(PASS, "Config", f"parsed {len(paths)} file(s)"))
    general = config.get("general", {})
    transport = general.get("transport", "serial") if isinstance(general, dict) else None
    ok = True
    if transport != "coc":
        ok = False
        results.append(Result(FAIL, "Transport", f"general.transport is {transport!r}, not 'coc'",
                              'Set transport = "coc" in [general] of a config.d file.'))
    else:
        results.append(Result(PASS, "Transport", "general.transport = coc"))
    coc = config.get("coc")
    if not isinstance(coc, dict):
        results.append(Result(FAIL, "Peer address", "no [coc] section", "Add a [coc] section with address and helper."))
        return results, None
    address = coc.get("address")
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address):
        ok = False
        results.append(Result(FAIL, "Peer address", "coc.address is missing or not AA:BB:CC:DD:EE:FF",
                              "Set coc.address to the repeater's Bluetooth address."))
    else:
        results.append(Result(PASS, "Peer address", "coc.address is well-formed"))
    adapter = coc.get("adapter", "hci0")
    if not isinstance(adapter, str) or not _ADAPTER_RE.fullmatch(adapter):
        ok = False
        results.append(Result(FAIL, "Adapter name", "coc.adapter must look like hci0", "Set coc.adapter = \"hci0\"."))
    helper = coc.get("helper")
    sockets = (coc.get("data_socket"), coc.get("control_socket"))
    if not isinstance(helper, str) or not os.path.isabs(helper) or re.search(r"[\x00-\x20]", helper):
        ok = False
        results.append(Result(FAIL, "Helper path", "coc.helper is not set to an absolute path without spaces",
                              "Set coc.helper to the installed observer-coc-helper path."))
    elif any(not isinstance(s, str) or not os.path.isabs(s) or len(s.encode()) > 100 for s in sockets) \
            or sockets[0] == sockets[1]:
        ok = False
        results.append(Result(FAIL, "Helper sockets", "coc.data_socket and coc.control_socket must be distinct absolute paths",
                              "Set both to files under /run/mctomqtt-coc/."))
    else:
        results.append(Result(PASS, "Helper path", "helper and socket paths set"))
    sync = general.get("sync_time", True) if isinstance(general, dict) else True
    table = {"address": address, "adapter": adapter, "helper": helper,
             "data_socket": sockets[0], "control_socket": sockets[1], "sync_time": sync is not False}
    return results, (table if ok else {**table, "invalid": True})


def check_helper(env: Env, helper: str) -> Result:
    name = "Helper binary"
    st = env.stat(helper)
    if st is None and env.access_denied(helper):
        return Result(WARN, name, f"cannot inspect {helper} as user {env.user} (a directory on its path is not accessible); "
                                  "re-run with sudo or as the service user",
                      "Run: sudo -u <service user> python3 -m installer coc-doctor (or use sudo).", perm_skipped=True)
    if st is None:
        return Result(FAIL, name, f"{helper} does not exist",
                      "Install the helper: python3 -m installer configure-coc ... (see docs/coc.md).")
    if not stat_module.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        return Result(FAIL, name, f"{helper} is not an executable file", f"Run: sudo chmod 755 {helper}")
    header = env.read_bytes(helper, 20)
    if header is None:
        return Result(WARN, name, f"cannot read {helper} as user {env.user}; architecture not checked; "
                                  "re-run with sudo or as the service user",
                      "Run: sudo -u <service user> python3 -m installer coc-doctor (or use sudo).", perm_skipped=True)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return Result(FAIL, name, f"{helper} is not an ELF executable", "Reinstall the helper from the release.")
    wanted = ELF_ARCHES.get(env.machine)
    if wanted is None:
        return Result(WARN, name, f"unknown machine type {env.machine!r}; architecture not compared",
                      "Use a helper built for this CPU.")
    endian = "<" if header[5] == 1 else ">"
    found = (header[4], struct.unpack_from(endian + "H", header, 18)[0])
    if found != wanted:
        return Result(FAIL, name, f"helper is built for a different CPU (ELF machine {found[1]}, host is {env.machine})",
                      "Install the helper asset that matches 'uname -m' (aarch64, armv7 or x86_64).")
    # The helper rejects unknown options with its usage text and exit status 2 before it touches
    # Bluetooth or any socket, so this is a safe way to prove that the binary starts.
    res = env.run([helper, "--help"])
    text = (res.stdout + res.stderr).strip()
    if not res.found:
        return Result(FAIL, name, f"helper cannot be executed ({res.stderr.strip()})",
                      "Check the glibc version (2.36 or newer) and the filesystem is not mounted noexec.")
    if res.returncode < 0:
        return Result(FAIL, name, f"helper crashed (killed by signal {-res.returncode}) when started",
                      "Reinstall the helper from the release; check it matches this system.")
    if res.returncode not in (0, 2) or "usage" not in text.lower():
        first = text.splitlines()[0] if text else "no output"
        return Result(FAIL, name, f"helper did not start normally (exit status {res.returncode}: {first})",
                      "Reinstall the helper from the release; check the glibc version (2.36 or newer).")
    return Result(PASS, name, f"helper starts and prints its usage text (architecture {env.machine})")


def _parse_info(text: str) -> dict[str, str]:
    props = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in ("Paired", "Bonded", "Trusted", "Connected", "Name"):
            props[key] = value.strip()
    return props


def check_peer(env: Env, address: str) -> list[Result]:
    name = "Peer pairing"
    res = env.run(["bluetoothctl", "info", address])
    if not res.found:
        return [Result(WARN, name, "bluetoothctl missing; cannot check pairing", "Install BlueZ first.")]
    if "not available" in (res.stdout + res.stderr) or (res.returncode != 0 and "Paired" not in res.stdout):
        reason = (res.stdout + res.stderr).strip().splitlines()
        reason_text = reason[0] if reason else "no answer"
        return [Result(FAIL, name, f"BlueZ does not know {address} ({reason_text})",
                       "Pair with the repeater first (see the enrollment guide), then 'bluetoothctl trust " + address + "'.")]
    props = _parse_info(res.stdout)
    out = []
    if props.get("Paired") != "yes" or props.get("Bonded") != "yes":
        out.append(Result(WARN, name,
                          f"not bonded (Paired={props.get('Paired', '?')}, Bonded={props.get('Bonded', '?')}); "
                          "the repeater refuses links below its required security level",
                          f"Remove and pair again with passkey entry: bluetoothctl remove {address}; then pair {address}."))
    else:
        out.append(Result(PASS, name, "paired and bonded"))
    if props.get("Trusted") != "yes":
        out.append(Result(WARN, "Peer trust", "peer is not trusted", f"Run: bluetoothctl trust {address}"))
    else:
        out.append(Result(PASS, "Peer trust", "peer is trusted"))
    return out


def check_clock(env: Env, sync_time: bool | None) -> Result:
    """`sync_time` None means the configuration could not be read, so only the host state is reported."""
    name = "System clock"
    if sync_time is False:
        return Result(PASS, name, "general.sync_time is off; the host clock is not sent to the repeater")
    res = env.run(["timedatectl", "show", "-p", "NTPSynchronized"])
    if not res.found or res.returncode != 0:
        return Result(WARN, name, "timedatectl unavailable; cannot tell whether the clock is synchronised",
                      "Make sure the host syncs time (for example systemd-timesyncd or chrony).")
    value = res.stdout.strip().partition("=")[2]
    if value != "yes" and sync_time is None:
        return Result(WARN, name, "the clock is not synchronised; this matters only if general.sync_time is on "
                                  "(not known: the configuration could not be read)",
                      "Run: sudo timedatectl set-ntp true (needs network access to a time server).")
    if value != "yes":
        return Result(WARN, name, "the clock is not synchronised, so the bridge will not set the repeater clock",
                      "Run: sudo timedatectl set-ntp true (needs network access to a time server).")
    return Result(PASS, name, "clock is synchronised")


def _writable(st: Any, uid: int, gids: set) -> bool:
    mode = st.st_mode
    if uid == 0:
        return True
    if st.st_uid == uid:
        return bool(mode & 0o200)
    if st.st_gid in gids:
        return bool(mode & 0o020)
    return bool(mode & 0o002)


def _rerun_hint(user: str) -> str:
    return f"Run: sudo -u {user or '<service user>'} python3 -m installer coc-doctor (or use sudo)."


def check_permissions(env: Env, coc: dict, props: dict[str, str] | None, unit_present: bool) -> list[Result]:
    out: list[Result] = []
    user = (props or {}).get("User") or ""
    account = env.lookup_user(user) if user else None
    if account is not None:
        uid, gids = account
        who = f"service user {user}"
    else:
        uid, gids = env.uid, env.gids
        who = "the current user" + (f" (cannot look up service user {user})" if user else "")
    # The L2CAP test opens a socket as whoever runs the doctor, so it speaks for the service only
    # when that is the same account.
    same_user = not user or (account is not None and env.uid == account[0])
    ok, why = env.can_open_l2cap()
    if same_user:
        if ok:
            out.append(Result(PASS, "L2CAP socket", "a Bluetooth L2CAP socket can be opened"))
        else:
            out.append(Result(FAIL, "L2CAP socket", f"cannot open a Bluetooth L2CAP socket: {why}",
                              "Load the Bluetooth kernel modules (sudo modprobe bluetooth) and run on a kernel with L2CAP support."))
    elif env.uid == 0 and not ok:
        out.append(Result(FAIL, "L2CAP socket", f"cannot open a Bluetooth L2CAP socket even as root: {why}",
                          "Load the Bluetooth kernel modules (sudo modprobe bluetooth) and run on a kernel with L2CAP support."))
    elif env.uid == 0:
        out.append(Result(PASS, "L2CAP socket", f"a Bluetooth L2CAP socket can be opened (tested as root, not as {user})"))
    else:
        outcome = "can open a socket" if ok else f"cannot open a socket ({why})"
        out.append(Result(WARN, "L2CAP socket", f"checked as {env.user}, service runs as {user}; re-run with sudo -u {user} "
                                                f"(as {env.user} this {outcome})", _rerun_hint(user), perm_skipped=True))
    helper = coc.get("helper")
    st = env.stat(helper) if isinstance(helper, str) else None
    if st is None and isinstance(helper, str):
        if env.access_denied(helper):
            out.append(Result(WARN, "Helper access", f"skipped: {helper} cannot be inspected as user {env.user} "
                              "(a directory on its path is not accessible)", _rerun_hint(user), perm_skipped=True))
        else:
            out.append(Result(WARN, "Helper access", f"skipped: {helper} does not exist (see Helper binary)",
                              "Install the helper first."))
    if st is not None:
        if st.st_uid == uid and st.st_mode & 0o100 or st.st_mode & 0o001 or (st.st_gid in gids and st.st_mode & 0o010):
            out.append(Result(PASS, "Helper access", f"{who} can execute the helper"))
        else:
            out.append(Result(WARN, "Helper access", f"{who} may not be allowed to execute the helper",
                              f"Run: sudo chmod 755 {helper}"))
    sock_dirs = sorted({os.path.dirname(p) for p in (coc.get("data_socket"), coc.get("control_socket")) if isinstance(p, str)})
    runtime = (props or {}).get("RuntimeDirectory", "").split()
    for directory in sock_dirs:
        dst = env.stat(directory)
        if dst is None and env.access_denied(directory):
            out.append(Result(WARN, "Socket directory", f"skipped: {directory} cannot be inspected as user {env.user}",
                              _rerun_hint(user), perm_skipped=True))
            continue
        if dst is not None:
            if stat_module.S_ISDIR(dst.st_mode) and _writable(dst, uid, gids):
                out.append(Result(PASS, "Socket directory", f"{directory} is writable by {who}"))
            else:
                out.append(Result(FAIL, "Socket directory", f"{directory} is not a directory writable by {who}",
                                  f"Fix ownership: sudo chown {user or '<service user>'} {directory}; or use RuntimeDirectory= in the unit."))
            continue
        parent_ok = os.path.dirname(directory) == "/run" and os.path.basename(directory) in runtime
        if parent_ok:
            out.append(Result(PASS, "Socket directory", f"{directory} is created by RuntimeDirectory= when the service starts"))
        elif unit_present and (props or {}).get("NeedDaemonReload") == "yes":
            out.append(Result(WARN, "Socket directory", f"{directory} cannot be checked until systemd reloads the unit",
                              "Run: sudo systemctl daemon-reload, then run the doctor again."))
        elif unit_present:
            out.append(Result(FAIL, "Socket directory",
                              f"{directory} does not exist and the unit has no matching RuntimeDirectory=",
                              f"Add to the unit: [Service] RuntimeDirectory={os.path.basename(directory)} and RuntimeDirectoryMode=0700 "
                              "(the 'configure-coc' command does this)."))
        else:
            out.append(Result(WARN, "Socket directory", f"{directory} does not exist (no unit to create it)",
                              "Create it for the service user, or run the bridge as a systemd service with RuntimeDirectory=."))
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _unit_name_of(header_path: str) -> str:
    path = Path(header_path)
    return path.parent.name[:-2] if path.parent.name.endswith(".d") else path.name


def find_unit_candidates(env: Env, requested: str, helper: str | None) -> list[str]:
    """Names of installed services whose ExecStart mentions the bridge or the helper."""
    listing = env.run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
    if not listing.found:
        return []
    names = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service") and not parts[0].endswith("@.service") and parts[0] != requested:
            names.append(parts[0])
    if not names:
        return []
    needles = ["mctomqtt", "observer-coc-helper"] + ([helper] if helper else [])
    res = env.run(["systemctl", "cat", "--no-pager"] + names)
    found: list[str] = []
    current = None
    for line in res.stdout.splitlines():
        if line.startswith("# /"):
            current = _unit_name_of(line[2:].strip())
        elif current and line.lstrip().startswith("ExecStart") and any(n in line for n in needles):
            if current not in found:
                found.append(current)
    return sorted(found)


def run_checks(env: Env, config_dir: Path = Path(DEFAULT_CONFIG_DIR), unit: str = DEFAULT_UNIT,
               files: list[str] | None = None, info: dict | None = None) -> list[Result]:
    info = info if info is not None else {}
    results = [check_bluez(env)]
    config_results, coc = check_config(env, config_dir, files)
    adapter = (coc or {}).get("adapter") if isinstance((coc or {}).get("adapter"), str) else "hci0"
    if not _ADAPTER_RE.fullmatch(adapter or ""):
        adapter = "hci0"
    results.append(check_adapter(env, adapter))
    results.append(check_bluetooth_service(env))
    props = _unit_properties(env, unit)
    unit_used = unit
    if props is not None and props.get("LoadState", "not-found") != "loaded":
        helper = (coc or {}).get("helper") if isinstance((coc or {}).get("helper"), str) else None
        candidates = find_unit_candidates(env, unit, helper)
        if len(candidates) == 1:
            unit_used = candidates[0]
            props = _unit_properties(env, unit_used)
            ordering = check_unit_ordering(unit_used, props)
            ordering.message = f"unit {unit_used} picked because no unit is named {unit}; {ordering.message}"
            results.append(ordering)
        elif candidates:
            results.append(Result(WARN, f"{unit} ordering",
                                  f"no unit is named {unit}; these units run the bridge: {', '.join(candidates)}",
                                  f"Re-run with --unit <name>, for example --unit {candidates[0]}"))
            props = None
        else:
            results.append(Result(WARN, f"{unit} ordering",
                                  f"no unit named {unit} and no installed service runs mctomqtt or the helper; "
                                  "if the bridge is not run as a service it will not start after a reboot",
                                  "Re-run with --unit <name> if the service has an unusual name."))
            props = None
    else:
        results.append(check_unit_ordering(unit, props))
    info["unit"] = unit_used
    info["service_user"] = (props or {}).get("User") or ""
    results.extend(config_results)
    unit_present = bool(props) and props.get("LoadState") == "loaded"
    if coc is not None and not coc.get("invalid"):
        results.append(check_helper(env, coc["helper"]))
        results.extend(check_peer(env, coc["address"]))
        results.append(check_clock(env, coc["sync_time"]))
        results.extend(check_permissions(env, coc, props if unit_present else None, unit_present))
    elif any(r.perm_skipped for r in config_results):
        results.append(check_clock(env, None))
        results.append(Result(WARN, "Helper, peer, permissions", "skipped because the configuration cannot be read",
                              _rerun_hint(info["service_user"]), perm_skipped=True))
    else:
        results.append(Result(WARN, "Helper, peer, clock, permissions", "skipped because the configuration is unusable",
                              "Fix the configuration problems above and run again."))
    return results


def format_header(env: Env, config_dir: Path, unit: str, files: list[str] | None, info: dict | None = None) -> str:
    shown = ", ".join(files) if files else f"{config_dir}"
    used = (info or {}).get("unit", unit)
    unit_text = used if used == unit else f"{used} (requested {unit})"
    return f"coc-doctor ran as user {env.user}; config: {shown}; unit: {unit_text}"


def format_results(results: list[Result], header: str = "") -> str:
    lines = [header] if header else []
    for item in results:
        lines.append(f"[{item.status}] {item.name}: {item.message}")
        if item.status != PASS and item.hint:
            lines.append(f"       fix: {item.hint}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}
    lines.append(f"{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)")
    if any(r.perm_skipped for r in results):
        lines.append("Some checks were skipped or are not authoritative because of file permissions; "
                     "re-run with sudo, or as the service user: sudo -u <service user> python3 -m installer coc-doctor")
    return "\n".join(lines)


def run_after_setup(config_dir: str, env: Env | None = None, out=None) -> int:
    """Print the doctor's report at the end of guided setup; return its failure count."""
    out = out or sys.stdout
    env = env or Env()
    info: dict = {}
    results = run_checks(env, Path(config_dir), info=info)
    print("\nChecking the host with coc-doctor:", file=out)
    print(format_results(results, format_header(env, Path(config_dir), DEFAULT_UNIT, None, info)), file=out)
    return sum(1 for r in results if r.status == FAIL)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                        help="Directory holding config.toml and config.d (default: %(default)s)")
    parser.add_argument("--config", action="append", dest="config_files", default=None,
                        help="Check these config files only, like mctomqtt.py --config (repeatable)")
    parser.add_argument("--unit", default=DEFAULT_UNIT, help="systemd unit of the bridge (default: %(default)s)")


def run(args: argparse.Namespace, env: Env | None = None, out=None) -> int:
    env = env or Env()
    info: dict = {}
    results = run_checks(env, Path(args.config_dir), args.unit, args.config_files, info)
    header = format_header(env, Path(args.config_dir), args.unit, args.config_files, info)
    print(format_results(results, header), file=out or sys.stdout)
    return 1 if any(r.status == FAIL for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coc-doctor", description="Read-only check of the Bluetooth (CoC) setup")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
