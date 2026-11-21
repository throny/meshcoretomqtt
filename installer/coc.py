"""Opt-in CoC configuration for the existing Linux/systemd installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import tempfile
import tomllib
import urllib.parse
import urllib.request

from . import InstallerContext

MARKER = "# Managed by meshcoretomqtt configure-coc\n"
ARCHES = {"x86_64": ("x86_64", 2, 62), "amd64": ("x86_64", 2, 62),
          "aarch64": ("aarch64", 2, 183), "arm64": ("aarch64", 2, 183),
          "armv7l": ("armv7", 1, 40), "armv8l": ("armv7", 1, 40)}
MAX_HELPER = 32 * 1024 * 1024
# Wants= (not only After=) so that a host where bluetooth.service is not yet
# running starts it instead of leaving the bridge in a restart loop.
DROPIN_BODY = ("[Unit]\nWants=bluetooth.service\nAfter=bluetooth.service\n\n"
               "[Service]\nRuntimeDirectory=mctomqtt-coc\nRuntimeDirectoryMode=0700\n")


def release_asset(version: str, machine: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or machine not in ARCHES:
        raise ValueError("A fixed helper version and supported architecture are required")
    name = "observer-coc-helper-" + ARCHES[machine][0]
    return f"https://github.com/throny/meshcoretomqtt/releases/download/coc-helper-v{version}/{name}", name


def verify_helper(data: bytes, digest: str, machine: str) -> None:
    if (not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            or hashlib.sha256(data).hexdigest() != digest.lower()):
        raise ValueError("Helper SHA-256 mismatch")
    if machine not in ARCHES or len(data) < 64 or len(data) > MAX_HELPER:
        raise ValueError("Unsupported helper platform or size")
    _, elf_class, elf_machine = ARCHES[machine]
    if (data[:4] != b"\x7fELF" or data[4] != elf_class or data[5] != 1
            or struct.unpack_from("<H", data, 18)[0] != elf_machine
            or struct.unpack_from("<H", data, 16)[0] not in (2, 3)):
        raise ValueError("Helper ELF architecture does not match this host")
    if elf_machine == 40 and not struct.unpack_from("<I", data, 36)[0] & 0x400:
        raise ValueError("ARM helper must use the hard-float ABI")


def _https_release_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Helper download must remain on GitHub HTTPS release hosts")


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_release_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_release(url: str, limit: int) -> bytes:
    _https_release_url(url)
    opener = urllib.request.build_opener(_ReleaseRedirect())
    with opener.open(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Helper release asset exceeds the size limit")
    return data


def checksum_for(data: bytes, name: str) -> str:
    matches = []
    for line in data.decode("ascii").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == name:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise ValueError("Release checksum entry missing, duplicated or invalid")
    return matches[0].lower()


def download_helper(version: str, machine: str) -> tuple[bytes, str]:
    url, name = release_asset(version, machine)
    digest = checksum_for(_read_release(url.rsplit("/", 1)[0] + "/SHA256SUMS", 32768), name)
    data = _read_release(url, MAX_HELPER)
    verify_helper(data, digest, machine)
    return data, digest


def _no_symlinks(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("CoC setup paths must not contain symlinks")


def _owned_file(path: Path) -> None:
    _no_symlinks(path)
    if path.exists() and (not path.is_file() or not path.read_text().startswith(MARKER)):
        raise ValueError("Refusing to replace an operator-owned configuration file")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".coc-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            os.fchmod(file.fileno(), mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure_coc(ctx: InstallerContext, *, address: str, helper: Path, sha256: str,
                  adapter: str = "hci0", machine: str | None = None,
                  unit_dir: Path = Path("/etc/systemd/system"),
                  helper_dir: Path = Path("/usr/local/libexec/meshcoretomqtt")) -> dict[str, Path]:
    """Stage verified files only. Never pair, reload systemd, restart or set time."""
    if platform.system() != "Linux":
        raise ValueError("CoC setup supports native Linux/systemd installations only")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
        raise ValueError("A Bluetooth address is required")
    if not re.fullmatch(r"hci\d+", adapter):
        raise ValueError("Adapter must be an hci device name")
    app, config_dir = Path(ctx.install_dir), Path(ctx.config_dir)
    for path in (app, config_dir, helper, unit_dir, helper_dir):
        _no_symlinks(path)
    if (not (app / "mctomqtt.py").is_file() or not (app / ".install_type").is_file()
            or (app / ".install_type").read_text().strip() != "systemd"):
        raise ValueError("Configure CoC after a native systemd installation")
    unit = unit_dir / "mctomqtt.service"
    _no_symlinks(unit)
    if not unit.is_file():
        raise ValueError("Existing mctomqtt.service is required")
    users = re.findall(r"(?m)^User=(.*)$", unit.read_text())
    if (len(users) != 1 or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", users[0].strip())
            or users[0].strip() == "root"):
        raise ValueError("The existing service must have a static service account")
    if helper.stat().st_size > MAX_HELPER:
        raise ValueError("Helper exceeds size limit")
    data = helper.read_bytes()
    verify_helper(data, sha256, machine or platform.machine().lower())
    binary = helper_dir / ("observer-coc-helper-" + sha256.lower()[:16])
    profile = config_dir / "config.d/90-coc.toml"
    dropin = unit_dir / "mctomqtt.service.d/90-coc.conf"
    for path in (profile, dropin):
        _owned_file(path)
    _no_symlinks(binary)
    if binary.exists() and (not binary.is_file() or binary.read_bytes() != data):
        raise ValueError("Installed helper path contains different bytes")
    if helper_dir.exists() and (helper_dir.stat().st_uid != os.geteuid()
                               or stat.S_IMODE(helper_dir.stat().st_mode) & 0o022):
        raise ValueError("Helper directory must be administrator-owned and not group/world writable")
    settings = {"address": address.upper(), "adapter": adapter, "helper": str(binary),
                "data_socket": "/run/mctomqtt-coc/data.sock", "control_socket": "/run/mctomqtt-coc/control.sock"}
    text = MARKER + '[general]\ntransport = "coc"\n\n[coc]\n'
    text += "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
    # Respect later user overrides rather than silently installing an ineffective profile.
    effective = tomllib.loads(text)
    for path in sorted(profile.parent.glob("*.toml")):
        _no_symlinks(path)
        if path.name > profile.name:
            override = tomllib.loads(path.read_text())
            for section in ("general", "coc"):
                if section in override:
                    if not isinstance(override[section], dict):
                        raise ValueError("Invalid later configuration table")
                    effective[section].update(override[section])
    if (effective["general"]["transport"] != "coc"
            or any(effective["coc"].get(key) != value for key, value in settings.items())):
        raise ValueError("Later user configuration conflicts with CoC; resolve it before setup")
    # All validation precedes filesystem changes. Activation is deliberately manual.
    helper_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    profile.parent.mkdir(parents=True, exist_ok=True)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(binary, data, 0o755)
    _atomic_write(dropin, (MARKER + DROPIN_BODY).encode(), 0o644)
    _atomic_write(profile, text.encode(), 0o644)
    return {"helper": binary, "config": profile, "dropin": dropin}
