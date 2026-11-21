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
        (HELPER, "--help"): CmdResult(2, "", "usage: observer-coc-helper --peer ...\n"),
    }
    base.update(table or {})
    runner = Runner(base)
    files = {HELPER: st(), "/run/mctomqtt-coc": st(0o040700, 990, 990)}
    env = Env(run=runner, system="Linux", machine="aarch64",
              exists=lambda p: p.startswith("/sys/class/bluetooth"),
              read_bytes=lambda p, n: elf(183)[:n] if p == HELPER else None,
              read_text=lambda p: None,
              stat=lambda p: files.get(p),
              access_denied=lambda p: False,
              user="mctomqtt",
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
    assert doc.check_unit_ordering("u.service", {"LoadState": "not-found"}).status == WARN
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

def test_helper_pass_when_it_prints_usage():
    r = doc.check_helper(make_env(), HELPER)
    assert r.status == PASS and "aarch64" in r.message


def test_helper_is_started_with_an_option_that_cannot_open_bluetooth():
    env = make_env()
    doc.check_helper(env, HELPER)
    assert [HELPER, "--help"] in env.run.calls and [HELPER, "--version"] not in env.run.calls


@pytest.mark.parametrize("result", [
    CmdResult(-11, "", ""),                                            # killed by SIGSEGV
    CmdResult(127, "", "error while loading shared libraries: libc.so.6"),
    CmdResult(1, "", "CoC helper stopped (security, discovery, channel, or IPC failure)"),
    CmdResult(0, "", ""),                                              # started, said nothing
])
def test_helper_crash_or_wrong_exit_fails(result):
    env = make_env({(HELPER, "--help"): result})
    assert doc.check_helper(env, HELPER).status == FAIL


def test_helper_crash_by_signal_is_named():
    env = make_env({(HELPER, "--help"): CmdResult(-4, "", "")})
    assert "signal 4" in doc.check_helper(env, HELPER).message


def test_helper_parent_directory_not_traversable_warns_not_fails():
    env = make_env(user="observer")
    env.stat = lambda p: None
    env.access_denied = lambda p: p == HELPER
    r = doc.check_helper(env, HELPER)
    assert r.status == WARN and "cannot inspect" in r.message and "observer" in r.message
    assert "sudo" in r.message and r.perm_skipped and "Install" not in r.hint


def test_helper_unreadable_header_warns():
    env = make_env(read_bytes=lambda p, n: None)
    assert doc.check_helper(env, HELPER).status == WARN


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
    env = make_env({(HELPER, "--help"): CmdResult(126, "", "permission denied", found=False)})
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
            "-p", "Requires", "-p", "BindsTo", "-p", "After", "-p", "RuntimeDirectory", "-p", "NeedDaemonReload")


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
               ("timedatectl", "show"), (HELPER, "--help")}
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


def test_unit_needing_daemon_reload_warns_instead_of_failing():
    stale = dict(props(UNIT_OK.replace("Wants=network-online.target bluetooth.service", "Wants=")), NeedDaemonReload="yes")
    r = doc.check_unit_ordering("u.service", stale)
    assert r.status == WARN and "daemon-reload" in r.hint


def test_missing_runtime_directory_warns_until_reload():
    env = make_env()
    del env.files["/run/mctomqtt-coc"]
    p = props(UNIT_OK.replace("RuntimeDirectory=mctomqtt-coc", "RuntimeDirectory=") + "NeedDaemonReload=yes")
    assert status(doc.check_permissions(env, COC, p, True), "Socket directory") == WARN


def test_run_after_setup_prints_report_and_counts_failures(tmp_path):
    write_cfg(tmp_path, GOOD)
    out = io.StringIO()
    env = make_env({UNIT_CMD: UNIT_OK})
    assert doc.run_after_setup(str(tmp_path), env, out) == 0
    assert "coc-doctor" in out.getvalue() and "0 failure(s)" in out.getvalue()
    env = make_env({UNIT_CMD: UNIT_OK, ("bluetoothctl", "info", ADDR): CmdResult(1, f"Device {ADDR} not available\n")})
    assert doc.run_after_setup(str(tmp_path), env, io.StringIO()) == 1


# --- unreadable config (permission denied is not a syntax error) ------------------

def test_config_unreadable_is_warn_not_syntax_error(tmp_path):
    write_cfg(tmp_path, GOOD)
    target = tmp_path / "config.d" / "99-user.toml"
    real_open = open

    def deny(path, *a, **k):
        if str(path) == str(target):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **k)

    import builtins
    mp = pytest.MonkeyPatch()
    mp.setattr(builtins, "open", deny)
    try:
        results, coc = doc.check_config(make_env(user="observer"), tmp_path, None)
    finally:
        mp.undo()
    assert coc is None and results[0].status == WARN and results[0].perm_skipped
    assert f"cannot read {target} as user observer" in results[0].message
    assert "re-run with sudo or as the service user" in results[0].message
    assert "TOML" not in results[0].message + results[0].hint


def test_config_dir_not_listable_is_warn(tmp_path, monkeypatch):
    write_cfg(tmp_path, GOOD)
    real = os.listdir

    def deny(path="."):
        if str(path).endswith("config.d"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "listdir", deny)
    results, coc = doc.check_config(make_env(), tmp_path, None)
    assert coc is None and results[0].status == WARN and results[0].perm_skipped


def _unreadable(monkeypatch):
    def fail(*a, **k):
        raise doc.ConfigUnreadable("/etc/mctomqtt/config.d/99-user.toml", "x")

    monkeypatch.setattr(doc, "load_config", fail)


def test_unreadable_config_still_runs_checks_that_need_no_config(tmp_path, monkeypatch):
    _unreadable(monkeypatch)
    env = make_env({UNIT_CMD: UNIT_OK})
    results = doc.run_checks(env, tmp_path)
    names = [r.name for r in results]
    for needed in ("BlueZ", "Adapter hci0", "bluetooth.service", "mctomqtt.service ordering", "System clock"):
        assert needed in names
    assert status(results, "Config") == WARN and status(results, "mctomqtt.service ordering") == PASS
    assert not any(r.status == FAIL for r in results)
    assert not any(c[:2] == ["bluetoothctl", "info"] for c in env.run.calls)
    assert any(r.perm_skipped and "cannot be read" in r.message for r in results)


def test_unreadable_config_clock_does_not_assume_sync_time(tmp_path, monkeypatch):
    _unreadable(monkeypatch)
    env = make_env({("timedatectl", "show", "-p", "NTPSynchronized"): "NTPSynchronized=no\n"})
    clock = [r for r in doc.run_checks(env, tmp_path) if r.name == "System clock"][0]
    assert clock.status == WARN and "sync_time" in clock.message


# --- unit discovery -----------------------------------------------------------------

LIST_CMD = ("systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager")
OTHER_UNIT = "meshcore-observer.service"
SHOW_FIELDS = ("-p", "LoadState", "-p", "User", "-p", "Wants", "-p", "Requires", "-p", "BindsTo", "-p", "After",
               "-p", "RuntimeDirectory", "-p", "NeedDaemonReload")
OTHER_CMD = ("systemctl", "show", OTHER_UNIT) + SHOW_FIELDS
NOT_FOUND = "LoadState=not-found\nUser=\nWants=\nRequires=\nBindsTo=\nAfter=\nRuntimeDirectory=\nNeedDaemonReload=no\n"
LISTING = ("bluetooth.service enabled enabled\ncron.service enabled enabled\n"
           "meshcore-observer.service enabled enabled\ngetty@.service enabled enabled\n")


def cat_text(*units):
    return "\n".join(f"# /etc/systemd/system/{name}\n[Service]\nExecStart={line}\n" for name, line in units)


CAT_CMD = ("systemctl", "cat", "--no-pager", "bluetooth.service", "cron.service", OTHER_UNIT)
CAT_ONE = cat_text(("bluetooth.service", "/usr/libexec/bluetooth/bluetoothd"),
                   ("cron.service", "/usr/sbin/cron -f"),
                   (OTHER_UNIT, "/opt/bridge/venv/bin/python /opt/bridge/mctomqtt.py --config /etc/x.toml"))


def unit_table(extra=None):
    table = {UNIT_CMD: NOT_FOUND, LIST_CMD: LISTING, CAT_CMD: CAT_ONE,
             OTHER_CMD: UNIT_OK.replace("mctomqtt", "observer")}
    table.update(extra or {})
    return table


def test_wrong_unit_name_picks_the_single_obvious_candidate(tmp_path):
    write_cfg(tmp_path, GOOD)
    out = io.StringIO()
    env = make_env(unit_table())
    args = argparse.Namespace(config_dir=str(tmp_path), unit="mctomqtt.service", config_files=None)
    doc.run(args, env, out)
    text = out.getvalue()
    assert "no systemd unit installed" not in text
    assert f"unit {OTHER_UNIT} picked because no unit is named mctomqtt.service" in text
    assert f"[PASS] {OTHER_UNIT} ordering" in text
    assert f"unit: {OTHER_UNIT} (requested mctomqtt.service)" in text.splitlines()[0]


def test_wrong_unit_name_uses_service_user_of_the_picked_unit(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env(unit_table())
    env.lookup_user = lambda n: (990, {990}) if n == "observer" else None
    results = doc.run_checks(env, tmp_path)
    assert "service user observer" in [r for r in results if r.name == "Helper access"][0].message


def test_wrong_unit_name_with_several_candidates_warns_and_lists_them(tmp_path):
    write_cfg(tmp_path, GOOD)
    listing = LISTING + "second-bridge.service enabled enabled\n"
    cat_cmd = CAT_CMD + ("second-bridge.service",)
    cat = CAT_ONE + "\n" + cat_text(("second-bridge.service", "/usr/bin/python3 /srv/mctomqtt.py"))
    env = make_env(unit_table({LIST_CMD: listing, cat_cmd: cat}))
    results = doc.run_checks(env, tmp_path)
    r = [x for x in results if x.name == "mctomqtt.service ordering"][0]
    assert r.status == WARN and OTHER_UNIT in r.message and "second-bridge.service" in r.message
    assert "--unit" in r.hint


def test_candidate_found_through_helper_path_in_exec_start():
    cat = cat_text(("bluetooth.service", "/usr/libexec/bluetooth/bluetoothd"), ("cron.service", "/usr/sbin/cron"),
                   (OTHER_UNIT, f"{HELPER} --test-mode"))
    env = make_env(unit_table({CAT_CMD: cat}))
    assert doc.find_unit_candidates(env, "mctomqtt.service", HELPER) == [OTHER_UNIT]


def test_dropin_headers_do_not_create_extra_candidates():
    cat = (cat_text((OTHER_UNIT, "/opt/mctomqtt.py")) +
           "\n# /etc/systemd/system/meshcore-observer.service.d/override.conf\n[Service]\n"
           "ExecStart=\nExecStart=/opt/mctomqtt.py\n")
    env = make_env(unit_table({CAT_CMD: cat}))
    assert doc.find_unit_candidates(env, "mctomqtt.service", None) == [OTHER_UNIT]


def test_no_unit_anywhere_is_warn_information_not_pass(tmp_path):
    write_cfg(tmp_path, GOOD)
    cat = cat_text(("bluetooth.service", "/usr/libexec/bluetooth/bluetoothd"), ("cron.service", "/usr/sbin/cron"),
                   (OTHER_UNIT, "/usr/bin/other-daemon"))
    env = make_env(unit_table({CAT_CMD: cat}))
    results = doc.run_checks(env, tmp_path)
    r = [x for x in results if x.name == "mctomqtt.service ordering"][0]
    assert r.status == WARN and "not run as a service" in r.message


def test_requested_unit_that_exists_is_not_searched_for_candidates(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env({UNIT_CMD: UNIT_OK})
    doc.run_checks(env, tmp_path)
    assert not any(c[:2] == ["systemctl", "list-unit-files"] for c in env.run.calls)


# --- permissions as the wrong user ----------------------------------------------------

def test_other_user_gets_warning_instead_of_authoritative_l2cap_verdict():
    env = make_env(uid=1001, gids={1001}, user="observer")
    r = [x for x in doc.check_permissions(env, COC, props(UNIT_OK), True) if x.name == "L2CAP socket"][0]
    assert r.status == WARN and r.perm_skipped
    assert "checked as observer, service runs as mctomqtt; re-run with sudo -u mctomqtt" in r.message


def test_other_user_failure_is_also_only_a_warning():
    env = make_env(uid=1001, gids={1001}, user="observer", can_open_l2cap=lambda: (False, "Operation not permitted"))
    r = [x for x in doc.check_permissions(env, COC, props(UNIT_OK), True) if x.name == "L2CAP socket"][0]
    assert r.status == WARN and "Operation not permitted" in r.message


def test_other_user_still_checks_modes_against_service_user():
    env = make_env(uid=1001, gids={1001}, user="observer")
    env.files[HELPER] = st(0o100750, 0, 990)
    r = doc.check_permissions(env, COC, props(UNIT_OK), True)
    assert status(r, "Helper access") == PASS and status(r, "Socket directory") == PASS


def test_root_checks_modes_against_service_user_not_root():
    env = make_env(uid=0, gids={0}, user="root")
    env.files["/run/mctomqtt-coc"] = st(0o040755, 0, 0)   # root could write here, the service user cannot
    env.files[HELPER] = st(0o100750, 0, 0)                # no execute bit for the service user
    r = doc.check_permissions(env, COC, props(UNIT_OK), True)
    assert status(r, "Socket directory") == FAIL and status(r, "Helper access") == WARN
    l2cap = [x for x in r if x.name == "L2CAP socket"][0]
    assert l2cap.status == PASS and "as root, not as mctomqtt" in l2cap.message


def test_root_l2cap_failure_fails():
    env = make_env(uid=0, gids={0}, user="root", can_open_l2cap=lambda: (False, "Operation not permitted"))
    assert status(doc.check_permissions(env, COC, props(UNIT_OK), True), "L2CAP socket") == FAIL


def test_service_user_run_is_authoritative():
    r = doc.check_permissions(make_env(), COC, props(UNIT_OK), True)
    assert status(r, "L2CAP socket") == PASS and not any(x.perm_skipped for x in r)


def test_no_unit_means_current_user_is_checked_without_warning():
    assert status(doc.check_permissions(make_env(), COC, None, False), "L2CAP socket") == PASS


# --- helper access is never silently omitted --------------------------------------------

def test_helper_access_reports_skip_when_helper_not_inspectable():
    env = make_env(uid=1001, gids={1001}, user="observer")
    env.stat = lambda p: None if p == HELPER else st(0o040700, 990, 990)
    env.access_denied = lambda p: p == HELPER
    r = [x for x in doc.check_permissions(env, COC, props(UNIT_OK), True) if x.name == "Helper access"]
    assert len(r) == 1 and r[0].status == WARN and r[0].perm_skipped and "skipped" in r[0].message
    assert "sudo -u mctomqtt" in r[0].hint


def test_helper_access_reports_skip_when_helper_missing():
    env = make_env()
    env.stat = lambda p: None if p == HELPER else st(0o040700, 990, 990)
    r = [x for x in doc.check_permissions(env, COC, props(UNIT_OK), True) if x.name == "Helper access"]
    assert len(r) == 1 and "does not exist" in r[0].message and not r[0].perm_skipped


def test_socket_directory_not_inspectable_is_warning():
    env = make_env(uid=1001, gids={1001}, user="observer")
    env.files.pop("/run/mctomqtt-coc")
    env.access_denied = lambda p: p == "/run/mctomqtt-coc"
    assert status(doc.check_permissions(env, COC, props(UNIT_OK), True), "Socket directory") == WARN


# --- report header and hint --------------------------------------------------------------

def test_report_header_names_user_config_and_unit(tmp_path):
    write_cfg(tmp_path, GOOD)
    env = make_env({UNIT_CMD: UNIT_OK}, user="observer")
    out = io.StringIO()
    doc.run(argparse.Namespace(config_dir=str(tmp_path), unit="mctomqtt.service", config_files=None), env, out)
    first = out.getvalue().splitlines()[0]
    assert "ran as user observer" in first and str(tmp_path) in first and "unit: mctomqtt.service" in first


def test_report_header_lists_explicit_config_files(tmp_path):
    header = doc.format_header(make_env(), tmp_path, "u.service", ["/a/one.toml", "/a/two.toml"])
    assert "/a/one.toml, /a/two.toml" in header


def test_final_hint_only_when_something_was_skipped_for_permissions():
    assert "file permissions" not in doc.format_results([doc.Result(PASS, "x", "fine")])
    loud = doc.format_results([doc.Result(WARN, "x", "skipped", perm_skipped=True)])
    assert loud.splitlines()[-1].startswith("Some checks were skipped") and "sudo -u" in loud
