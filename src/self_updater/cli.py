from __future__ import annotations

import argparse
import codecs
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from self_updater.health import HealthError, run_candidate_health
from self_updater.installer import InstallationError, UpdateInstaller, install_artifact
from self_updater.models import ManifestError, Target, Version
from self_updater.paths import InstallLayout, default_install_root, read_state
from self_updater.payload import to_pig_latin
from self_updater.release_config import RELEASE_PUBLIC_KEY_B64, release_urls
from self_updater.release_source import HttpReleaseSource, ReleaseSourceError
from self_updater.version import APP_NAME, APP_VERSION

RESTART_FOR_UPDATE = 75


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path)
    parser.add_argument("--manifest-url", help="override the built-in release manifest URL")
    parser.add_argument("--signature-url", help="override the built-in signature URL")
    parser.add_argument("--public-key", help="override the pinned release public key")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wordshift")
    parser.add_argument("--internal-health-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expect-version", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("version", help="print the running version")
    transform = commands.add_parser("transform", help="transform text")
    transform.add_argument("--json", action="store_true", dest="json_output")
    transform.add_argument("--rot13", action="store_true")
    transform.add_argument("text")
    install = commands.add_parser("install", help="install a stable per-user launcher")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--artifact", type=Path, required=True)
    check = commands.add_parser("check", help="check a signed release manifest")
    _source_arguments(check)
    update = commands.add_parser("update", help="stage and switch to a signed release")
    _source_arguments(update)
    return parser


def _layout(root: Path | None, env: Mapping[str, str], target: Target) -> InstallLayout:
    configured = root
    if configured is None and env.get("WORDSHIFT_INSTALL_ROOT"):
        configured = Path(env["WORDSHIFT_INSTALL_ROOT"])
    return InstallLayout.for_root(configured or default_install_root(env=env), target)


def _installer(
    arguments: argparse.Namespace,
    env: Mapping[str, str],
) -> tuple[UpdateInstaller, InstallLayout]:
    target = Target.current()
    layout = _layout(arguments.root, env, target)
    if arguments.manifest_url:
        manifest_url = arguments.manifest_url
        signature_url = arguments.signature_url or manifest_url + ".sig"
    else:
        defaults = release_urls(target)
        manifest_url = defaults.manifest
        signature_url = arguments.signature_url or defaults.signature
    source = HttpReleaseSource(manifest_url, signature_url)
    return (
        UpdateInstaller(
            layout,
            source,
            arguments.public_key or RELEASE_PUBLIC_KEY_B64,
            target,
            health_check=run_candidate_health,
        ),
        layout,
    )


def _run(arguments: argparse.Namespace, env: Mapping[str, str]) -> int:
    if arguments.internal_health_check:
        if arguments.expect_version != APP_VERSION:
            print(
                f"version mismatch: expected {arguments.expect_version}, running {APP_VERSION}",
                file=sys.stderr,
            )
            return 1
        print(json.dumps({"status": "ok", "version": APP_VERSION}, sort_keys=True))
        return 0
    if arguments.command == "version":
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if arguments.command == "transform":
        output = (
            codecs.encode(arguments.text, "rot_13")
            if arguments.rot13
            else to_pig_latin(arguments.text)
        )
        if arguments.json_output:
            print(json.dumps({"input": arguments.text, "output": output}, sort_keys=True))
        else:
            print(output)
        return 0
    if arguments.command == "install":
        target = Target.current()
        layout = InstallLayout.for_root(arguments.root, target)
        install_artifact(arguments.artifact, layout, Version.parse(APP_VERSION))
        print(f"installed {APP_NAME} {APP_VERSION} at {layout.root}")
        return 0
    if arguments.command in {"check", "update"}:
        installer, layout = _installer(arguments, env)
        if arguments.command == "check":
            release = installer.check()
            if release is None:
                print(f"up to date: {read_state(layout).current_version}")
            else:
                print(f"update available: {release.version}")
            return 0
        staged = installer.check_and_stage()
        if staged is None:
            print(f"up to date: {read_state(layout).current_version}")
            return 0
        print(f"staged {staged}; restarting")
        return RESTART_FOR_UPDATE
    raise InstallationError("choose a command")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return _run(arguments, os.environ if env is None else env)
    except (HealthError, InstallationError, ManifestError, ReleaseSourceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
