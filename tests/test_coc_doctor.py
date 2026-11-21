"""Offline tests for the read-only CoC doctor: every command output is faked."""
from __future__ import annotations

import argparse
import io
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import coc_doctor as doc
from installer.coc_doctor import CmdResult, Env, FAIL, PASS, WARN

ADDR = "AA:BB:CC:DD:EE:FF"
HELPER = "/usr/local/libexec/meshcoretomqtt/observer-coc-helper"

INFO_OK = "Device AA:BB:CC:DD:EE:FF (random)\n\tName: Repeater\n\tPaired: yes\n\tBonded: yes\n\tTrusted: yes\n\tConnected: no\n"
SHOW_ON = "Controller 11:22:33:44:55:66 (public)\n\tPowered: yes\n"
UNIT_OK = ("LoadState=loaded\nUser=mctomqtt\nWants=network-online.target bluetooth.service\nRequires=\nBindsTo=\n"
           "After=network-online.target bluetooth.service\nRuntimeDirectory=mctomqtt-coc\n")


def elf(machine: int, klass: int = 2) -> bytes:
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[4], header[5] = klass, 1
    struct.pack_into("<H", header, 18, machine)
    return bytes(header)


def st(mode=0o100755, uid=0, gid=0):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)


class Runner:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        value = self.table.get(tuple(cmd))
        if value is None:
            return CmdResult(127, "", "not found", found=False)
        return value if isinstance(value, CmdResult) else CmdResult(0, value)


def make_env(table=None, **kw):
    base = {
        ("bluetoothctl", "--version"): "bluetoothctl: 5.82\n",
        ("bluetoothctl", "show"): SHOW_ON,
        ("bluetoothctl", "info", ADDR): INFO_OK,
        ("systemctl", "is-enabled", "bluetooth.service"): "enabled\n",
        ("systemctl", "is-active", "bluetooth.service"): "active\n",
        ("timedatectl", "show", "-p", "NTPSynchronized"): "NTPSynchronized=yes\n",
        (HELPER, "--version"): CmdResult(2, "", "usage: observer-coc-helper --peer ...\n"),
    }
    base.update(table or {})
    runner = Runner(base)
    files = {HELPER: st(), "/run/mctomqtt-coc": st(0o040700, 990, 990)}
    env = Env(run=runner, system="Linux", machine="aarch64",
              exists=lambda p: p.startswith("/sys/class/bluetooth"),
              read_bytes=lambda p, n: elf(183)[:n] if p == HELPER else None,
              read_text=lambda p: None,
              stat=lambda p: files.get(p),
              lookup_user=lambda n: (990, {990}),
              can_open_l2cap=lambda: (True, ""),
              uid=990, gids={990})
    for key, value in kw.items():
        setattr(env, key, value)
    env.files = files
    return env


def status(results, name):
    matches = [r for r in results if r.name == name]
    assert matches, f"no result named {name}: {[r.name for r in results]}"
    return matches[0].status


# --- 1. BlueZ ---------------------------------------------------------------

def test_bluez_pass():
    r = doc.check_bluez(make_env())
    assert r.status == PASS and "5.82" in r.message


def test_bluez_old_version_warns():
    r = doc.check_bluez(make_env({("bluetoothctl", "--version"): "bluetoothctl: 5.66\n"}))
    assert r.status == WARN and r.hint


def test_bluez_missing_fails():
    r = doc.check_bluez(make_env({("bluetoothctl", "--version"): CmdResult(127, "", "", found=False)}))
    assert r.status == FAIL and "apt" in r.hint


def test_bluez_not_linux_fails():
    assert doc.check_bluez(make_env(system="Windows")).status == FAIL


# --- 2. adapter -------------------------------------------------------------

def test_adapter_powered_pass():
    assert doc.check_adapter(make_env(), "hci0").status == PASS


def test_adapter_unpowered_fails():
    env = make_env({("bluetoothctl", "show"): "Controller X\n\tPowered: no\n"})
    r = doc.check_adapter(env, "hci0")
    assert r.status == FAIL and "power on" in r.hint


def test_adapter_missing_fails():
    env = make_env(exists=lambda p: p == "/sys/class/bluetooth")
    assert doc.check_adapter(env, "hci0").status == FAIL
    assert doc.check_adapter(make_env(exists=lambda p: False), "hci0").status == FAIL


def test_adapter_cannot_read_state_is_warn():
    env = make_env({("bluetoothctl", "show"): CmdResult(1, "", "No default controller")})
    assert doc.check_adapter(env, "hci0").status == WARN


def test_adapter_uses_sysfs_address_for_named_controller():
    env = make_env({("bluetoothctl", "show", "11:22:33:44:55:66"): "Powered: yes\n"},
                   read_text=lambda p: "11:22:33:44:55:66\n")
    assert doc.check_adapter(env, "hci1").status == PASS
    assert ["bluetoothctl", "show", "11:22:33:44:55:66"] in env.run.calls


# --- 3. bluetooth.service ---------------------------------------------------

def test_service_enabled_active_pass():
    assert doc.check_bluetooth_service(make_env()).status == PASS


def test_service_disabled_fails():
    env = make_env({("systemctl", "is-enabled", "bluetooth.service"): CmdResult(1, "disabled\n")})
    r = doc.check_bluetooth_service(env)
    assert r.status == FAIL and "enable --now" in r.hint


def test_service_inactive_fails():
    env = make_env({("systemctl", "is-active", "bluetooth.service"): CmdResult(3, "inactive\n")})
    assert doc.check_bluetooth_service(env).status == FAIL


def test_service_without_systemctl_warns():
    env = make_env({("systemctl", "is-enabled", "bluetooth.service"): CmdResult(127, found=False),
                    ("systemctl", "is-active", "bluetooth.service"): CmdResult(127, found=False)})
    assert doc.check_bluetooth_service(env).status == WARN


# --- 4. unit ordering -------------------------------------------------------

def props(text):
    return dict(line.split("=", 1) for line in text.splitlines())


def test_unit_ordering_pass():
    assert doc.check_unit_ordering("u.service", props(UNIT_OK)).status == PASS


def test_unit_after_only_fails():
    text = UNIT_OK.replace("Wants=network-online.target bluetooth.service", "Wants=network-online.target")
    r = doc.check_unit_ordering("u.service", props(text))
    assert r.status == FAIL and "After=bluetooth.service but no Wants" in r.message


def test_unit_requires_counts_and_missing_after_warns():
    text = UNIT_OK.replace("Requires=", "Requires=bluetooth.service").replace(
        "After=network-online.target bluetooth.service", "After=network-online.target")
    assert doc.check_unit_ordering("u.service", props(text)).status == WARN


def test_unit_absent_or_unreadable():
    assert doc.check_unit_ordering("u.service", {"LoadState": "not-found"}).status == PASS
    assert doc.check_unit_ordering("u.service", None).status == WARN


# --- 5. config --------------------------------------------------------------

GOOD = f'''[general]
transport = "coc"
[coc]
address = "{ADDR}"
adapter = "hci0"
helper = "{HELPER}"
data_socket = "/run/mctomqtt-coc/data.sock"
control_socket = "/run/mctomqtt-coc/control.sock"
'''


def write_cfg(tmp_path, text, name="99-user.toml"):
    (tmp_path / "config.d").mkdir(exist_ok=True)
    (tmp_path / "config.d" / name).write_text(text)


def cfg(tmp_path):
    return doc.check_config(make_env(), tmp_path, None)


def test_config_pass(tmp_path):
    write_cfg(tmp_path, GOOD)
    results, coc = cfg(tmp_path)
    assert all(r.status == PASS for r in results)
    assert coc["address"] == ADDR and coc["sync_time"] is True


def test_config_parse_error_fails(tmp_path):
    write_cfg(tmp_path, "[general\n")
    results, coc = cfg(tmp_path)
    assert results[0].status == FAIL and "99-user.toml" in results[0].message and coc is None


def test_config_missing_fails(tmp_path):
    results, coc = cfg(tmp_path)
    assert results[0].status == FAIL and coc is None


def test_config_wrong_transport_fails(tmp_path):
    write_cfg(tmp_path, GOOD.replace('"coc"', '"serial"', 1))
    results, _ = cfg(tmp_path)
    assert status(results, "Transport") == FAIL


@pytest.mark.parametrize("bad", ["02:00:00:00:00", "not-an-address", ""])
def test_config_bad_address_fails(tmp_path, bad):
    write_cfg(tmp_path, GOOD.replace(ADDR, bad))
    results, coc = cfg(tmp_path)
    assert status(results, "Peer address") == FAIL and coc["invalid"]


def test_config_missing_helper_or_relative_fails(tmp_path):
    write_cfg(tmp_path, GOOD.replace(f'helper = "{HELPER}"\n', ""))
    assert status(cfg(tmp_path)[0], "Helper path") == FAIL
    write_cfg(tmp_path, GOOD.replace(HELPER, "helper"))
    assert status(cfg(tmp_path)[0], "Helper path") == FAIL


def test_config_same_sockets_fail(tmp_path):
    write_cfg(tmp_path, GOOD.replace("control.sock", "data.sock"))
    assert status(cfg(tmp_path)[0], "Helper sockets") == FAIL


def test_config_dropins_override_base(tmp_path):
    (tmp_path / "config.toml").write_text('[general]\nsync_time = true\ntransport = "serial"\n')
    write_cfg(tmp_path, GOOD)
    write_cfg(tmp_path, "[general]\nsync_time = false\n", name="99-zz.toml")
    results, coc = cfg(tmp_path)
    assert status(results, "Transport") == PASS and coc["sync_time"] is False


# --- 6. helper --------------------------------------------------------------

def test_helper_pass_without_version_flag():
    r = doc.check_helper(make_env(), HELPER)
    assert r.status == PASS and "aarch64" in r.message


def test_helper_prints_version():
    env = make_env({(HELPER, "--version"): "observer-coc-helper 0.1.0\n"})
    assert "0.1.0" in doc.check_helper(env, HELPER).message


def test_helper_missing_fails():
    env = make_env()
    env.stat = lambda p: None
    assert doc.check_helper(env, HELPER).status == FAIL


def test_helper_not_executable_fails():
    env = make_env()
    env.stat = lambda p: st(0o100644)
    assert doc.check_helper(env, HELPER).status == FAIL


def test_helper_wrong_architecture_fails():
    env = make_env(machine="x86_64")
    r = doc.check_helper(env, HELPER)
    assert r.status == FAIL and "different CPU" in r.message


def test_helper_32bit_arm_on_armv7l():
    env = make_env(machine="armv7l", read_bytes=lambda p, n: elf(40, klass=1))
    assert doc.check_helper(env, HELPER).status == PASS


def test_helper_not_elf_fails():
    env = make_env(read_bytes=lambda p, n: b"#!/bin/sh\n" + bytes(20))
    assert doc.check_helper(env, HELPER).status == FAIL


def test_helper_cannot_execute_fails():
    env = make_env({(HELPER, "--version"): CmdResult(126, "", "permission denied", found=False)})
    assert doc.check_helper(env, HELPER).status == FAIL


# --- 7. peer ----------------------------------------------------------------

def test_peer_bonded_trusted_pass():
    assert [r.status for r in doc.check_peer(make_env(), ADDR)] == [PASS, PASS]


def test_peer_not_bonded_warns_clearly():
    info = INFO_OK.replace("Bonded: yes", "Bonded: no")
    r = doc.check_peer(make_env({("bluetoothctl", "info", ADDR): info}), ADDR)[0]
    assert r.status == WARN and "security level" in r.message and "remove" in r.hint


def test_peer_not_trusted_warns():
    info = INFO_OK.replace("Trusted: yes", "Trusted: no")
    results = doc.check_peer(make_env({("bluetoothctl", "info", ADDR): info}), ADDR)
    assert status(results, "Peer trust") == WARN


def test_peer_unknown_fails():
    env = make_env({("bluetoothctl", "info", ADDR): CmdResult(1, f"Device {ADDR} not available\n")})
    assert doc.check_peer(env, ADDR)[0].status == FAIL


def test_peer_without_bluetoothctl_warns():
    env = make_env({("bluetoothctl", "info", ADDR): CmdResult(127, found=False)})
    assert doc.check_peer(env, ADDR)[0].status == WARN


# --- 8. clock ---------------------------------------------------------------

def test_clock_synced_pass():
    assert doc.check_clock(make_env(), True).status == PASS


def test_clock_unsynced_warns():
    env = make_env({("timedatectl", "show", "-p", "NTPSynchronized"): "NTPSynchronized=no\n"})
    assert doc.check_clock(env, True).status == WARN


def test_clock_ignored_when_sync_time_off():
    env = make_env({("timedatectl", "show", "-p", "NTPSynchronized"): "NTPSynchronized=no\n"})
    assert doc.check_clock(env, False).status == PASS
    assert not any(c[0] == "timedatectl" for c in env.run.calls)


def test_clock_without_timedatectl_warns():
    env = make_env({("timedatectl", "show", "-p", "NTPSynchronized"): CmdResult(127, found=False)})
    assert doc.check_clock(env, True).status == WARN


# --- 9. permissions ---------------------------------------------------------

COC = {"helper": HELPER, "data_socket": "/run/mctomqtt-coc/data.sock",
       "control_socket": "/run/mctomqtt-coc/control.sock"}


def test_permissions_pass():
    env = make_env()
    results = doc.check_permissions(env, COC, props(UNIT_OK), True)
    assert all(r.status == PASS for r in results)


def test_permissions_l2cap_denied_fails():
    env = make_env(can_open_l2cap=lambda: (False, "Operation not permitted"))
    results = doc.check_permissions(env, COC, props(UNIT_OK), True)
    assert status(results, "L2CAP socket") == FAIL


def test_permissions_socket_dir_not_writable_fails():
    env = make_env()
    env.files["/run/mctomqtt-coc"] = st(0o040755, 0, 0)
    results = doc.check_permissions(env, COC, props(UNIT_OK), True)
    assert status(results, "Socket directory") == FAIL


def test_permissions_missing_dir_created_by_runtime_directory():
    env = make_env()
    del env.files["/run/mctomqtt-coc"]
    assert status(doc.check_permissions(env, COC, props(UNIT_OK), True), "Socket directory") == PASS


def test_permissions_missing_dir_without_runtime_directory_fails():
    env = make_env()
    del env.files["/run/mctomqtt-coc"]
    p = props(UNIT_OK.replace("RuntimeDirectory=mctomqtt-coc", "RuntimeDirectory="))
    r = doc.check_permissions(env, COC, p, True)
    assert status(r, "Socket directory") == FAIL
    assert status(doc.check_permissions(env, COC, None, False), "Socket directory") == WARN


def test_permissions_helper_not_executable_by_service_user_warns():
    env = make_env()
    env.files[HELPER] = st(0o100750, 0, 0)
    assert status(doc.check_permissions(env, COC, props(UNIT_OK), True), "Helper access") == WARN


# --- end to end -------------------------------------------------------------

UNIT_CMD = ("systemctl", "show", "mctomqtt.service", "-p", "LoadState", "-p", "User", "-p", "Wants",
            "-p", "Requires", "-p", "BindsTo", "-p", "After", "-p", "RuntimeDirectory")


def test_full_run_all_pass_exit_zero(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env({UNIT_CMD: UNIT_OK})
    out = io.StringIO()
    args = argparse.Namespace(config_dir=str(tmp_path), unit="mctomqtt.service", config_files=None)
    assert doc.run(args, env, out) == 0
    text = out.getvalue()
    assert "FAIL" not in text and "WARN" not in text and "0 failure(s)" in text


def test_full_run_failure_exit_nonzero_with_hint(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env({UNIT_CMD: UNIT_OK, ("systemctl", "is-enabled", "bluetooth.service"): CmdResult(1, "disabled\n")})
    out = io.StringIO()
    args = argparse.Namespace(config_dir=str(tmp_path), unit="mctomqtt.service", config_files=None)
    assert doc.run(args, env, out) == 1
    assert "[FAIL] bluetooth.service" in out.getvalue() and "fix: Run: sudo systemctl enable --now bluetooth" in out.getvalue()


def test_unusable_config_skips_dependent_checks_and_still_runs(tmp_path):
    env = make_env()
    results = doc.run_checks(env, tmp_path)
    assert any(r.status == FAIL for r in results)
    assert not any(c[:2] == ["bluetoothctl", "info"] for c in env.run.calls)


def test_checks_only_issue_read_only_commands(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env({UNIT_CMD: UNIT_OK})
    doc.run_checks(env, tmp_path)
    allowed = {("bluetoothctl", "--version"), ("bluetoothctl", "show"), ("bluetoothctl", "info"),
               ("systemctl", "is-enabled"), ("systemctl", "is-active"), ("systemctl", "show"),
               ("timedatectl", "show"), (HELPER, "--version")}
    assert {tuple(c[:2]) for c in env.run.calls} <= allowed


def test_run_command_never_raises_on_missing_program():
    res = doc.run_command(["definitely-not-a-real-program-xyz"])
    assert res.found is False and res.returncode != 0


def test_cli_does_not_require_root(monkeypatch):
    import sys
    from installer import __main__ as entry
    monkeypatch.setattr(entry, "require_root", lambda: pytest.fail("doctor must not need root"))
    monkeypatch.setattr(doc, "run", lambda args, *a, **k: 7)
    monkeypatch.setattr(sys, "argv", ["installer", "coc-doctor", "--config-dir", "/nonexistent"])
    with pytest.raises(SystemExit) as raised:
        entry.main()
    assert raised.value.code == 7
