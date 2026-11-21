"""Entry point: python3 -m installer <subcommand> [options]"""
from __future__ import annotations

import argparse
import os
import sys

from . import InstallerContext
from .system import require_root


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mctomqtt-installer",
        description="MeshCore to MQTT installer",
    )
    parser.add_argument("--repo", default=os.environ.get("INSTALL_REPO", "Cisien/meshcoretomqtt"))
    parser.add_argument("--branch", default=os.environ.get("INSTALL_BRANCH", "main"))

    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser("install", help="Fresh installation")
    install_p.add_argument("--config", dest="config_url", default="", help="URL to download 99-user.toml from")
    install_p.add_argument("--update", action="store_true", help="Non-interactive update mode")

    sub.add_parser("update", help="Update existing installation")

    sub.add_parser("migrate", help="Migrate legacy ~/.meshcoretomqtt installation")

    doctor_p = sub.add_parser("coc-doctor", help="Read-only check of the Bluetooth (CoC) setup; no root needed")
    from .coc_doctor import add_arguments as add_doctor_arguments
    add_doctor_arguments(doctor_p)

    coc_p = sub.add_parser("configure-coc", help="Configure optional Bluetooth on an existing Linux/systemd install")
    coc_p.add_argument("--address", required=True, help="Already enrolled repeater Bluetooth address")
    coc_p.add_argument("--adapter", default="hci0")
    source = coc_p.add_mutually_exclusive_group(required=True)
    source.add_argument("--helper", help="Local prebuilt helper executable")
    source.add_argument("--helper-version", help="Fixed helper release version, for example 0.1.0")
    coc_p.add_argument("--sha256", help="Expected SHA-256 (required for a local helper)")

    args: argparse.Namespace = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "coc-doctor":
        from .coc_doctor import run as run_doctor
        sys.exit(run_doctor(args))

    if args.command == "configure-coc" and args.helper and not args.sha256:
        parser.error("--helper requires an independently verified --sha256")

    require_root()

    ctx = InstallerContext(
        repo=args.repo,
        branch=args.branch,
        local_install=os.environ.get("LOCAL_INSTALL", ""),
    )

    if args.command == "install":
        ctx.config_url = args.config_url
        ctx.update_mode = args.update
        from .install_cmd import run_install
        run_install(ctx)

    elif args.command == "update":
        ctx.update_mode = True
        from .update_cmd import run_update
        run_update(ctx)

    elif args.command == "migrate":
        from .migrate_cmd import run_migrate
        if not run_migrate(ctx):
            print("No legacy installation found to migrate.")
            sys.exit(0)

    elif args.command == "configure-coc":
        from pathlib import Path
        import platform
        import tempfile
        from .coc import configure_coc, download_helper
        try:
            if args.helper:
                configure_coc(ctx, address=args.address, adapter=args.adapter,
                              helper=Path(args.helper), sha256=args.sha256)
            else:
                data, digest = download_helper(args.helper_version, platform.machine().lower())
                with tempfile.TemporaryDirectory(prefix="mctomqtt-coc-") as directory:
                    helper = Path(directory) / "helper"
                    helper.write_bytes(data)
                    configure_coc(ctx, address=args.address, adapter=args.adapter,
                                  helper=helper, sha256=args.sha256 or digest)
        except ValueError as exc:
            parser.exit(1, f"CoC setup refused: {exc}\n")
        except OSError:
            parser.exit(1, "CoC setup failed: check release availability, permissions and installed paths.\n")
        print("CoC files configured; no pairing, service restart or clock write was performed.")
        print("After enrollment and configuration review, activate with:")
        print("  sudo systemctl daemon-reload")
        print("  sudo systemctl restart mctomqtt.service")
        print("  journalctl -u mctomqtt.service -n 30 --no-pager")
        from .coc_doctor import run_after_setup
        if run_after_setup(ctx.config_dir):
            print("\nThe files above were written, but resolve the failures before restarting the service.")
            print("Run 'python3 -m installer coc-doctor' again afterwards.")


if __name__ == "__main__":
    main()
