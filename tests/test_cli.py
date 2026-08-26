import json
from pathlib import Path

import pytest

from self_updater import cli
from self_updater.installer import UpdateInstaller, install_artifact
from self_updater.models import ReleaseManifest, Target, Version
from self_updater.paths import InstallLayout, read_state
from self_updater.release_source import HttpReleaseSource
from self_updater.version import APP_VERSION


def test_application_version_is_1_2_0() -> None:
    assert APP_VERSION == "1.2.0"


def test_internal_health_check_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--internal-health-check", "--expect-version", APP_VERSION]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "ok", "version": APP_VERSION}


def test_internal_health_check_rejects_wrong_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--internal-health-check", "--expect-version", "9.9.9"]) == 1
    assert "version mismatch" in capsys.readouterr().err


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == f"WordShift {APP_VERSION}\n"


def test_transform_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["transform", "Hello, world!"]) == 0
    assert capsys.readouterr().out == "Ellohay, orldway!\n"


def test_transform_command_can_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["transform", "--json", "Hello, world!"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "input": "Hello, world!",
        "output": "Ellohay, orldway!",
    }


def test_transform_command_can_emit_rot13(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["transform", "--rot13", "Hello, world!"]) == 0
    assert capsys.readouterr().out == "Uryyb, jbeyq!\n"


def test_transform_command_can_emit_rot13_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["transform", "--rot13", "--json", "Hello, world!"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "input": "Hello, world!",
        "output": "Uryyb, jbeyq!",
    }


def test_install_command_uses_explicit_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "wordshift"
    artifact.write_bytes(b"frozen binary")
    root = tmp_path / "install"

    assert cli.main(["install", "--root", str(root), "--artifact", str(artifact)]) == 0

    layout = InstallLayout.for_root(root, Target.current())
    assert read_state(layout).current_version == Version.parse(APP_VERSION)
    assert layout.launcher_path.read_bytes() == b"frozen binary"
    assert "installed WordShift" in capsys.readouterr().out


def test_check_command_uses_pinned_release_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Target.current()
    layout = InstallLayout.for_root(tmp_path / "install", target)
    artifact = tmp_path / "wordshift"
    artifact.write_bytes(b"frozen binary")
    install_artifact(artifact, layout, Version.parse(APP_VERSION))

    seen: dict[str, object] = {}

    def check(installer: UpdateInstaller) -> ReleaseManifest | None:
        seen["source"] = installer.source
        seen["public_key"] = installer.public_key_b64
        return None

    monkeypatch.setattr(UpdateInstaller, "check", check)

    assert cli.main(["check", "--root", str(layout.root)]) == 0
    source = seen["source"]
    assert isinstance(source, HttpReleaseSource)
    assert "wordshift-update-feed" in source.manifest_url
    assert seen["public_key"]


def test_custom_feed_supports_target_absent_from_default_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Target("linux", "arm64")
    monkeypatch.setattr(Target, "current", classmethod(lambda cls: target))
    arguments = cli._parser().parse_args(
        [
            "check",
            "--root",
            str(tmp_path / "install"),
            "--manifest-url",
            "https://example.com/custom.json",
        ]
    )

    installer, _ = cli._installer(arguments, {})

    assert isinstance(installer.source, HttpReleaseSource)
    assert installer.source.manifest_url == "https://example.com/custom.json"
    assert installer.source.signature_url == "https://example.com/custom.json.sig"
