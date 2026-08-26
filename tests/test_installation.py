import base64
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from self_updater.installer import InstallationError, UpdateInstaller, install_artifact
from self_updater.models import Artifact, InstallState, Target, Version
from self_updater.paths import (
    InstallLayout,
    default_install_root,
    executable_name,
    read_state,
    write_state,
)
from self_updater.release_source import ReleaseBundle
from self_updater.security import SecurityError, sign_bytes

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
CANDIDATE = b"version 1.1.0 candidate"


class StaticSource:
    def __init__(self, bundle: ReleaseBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def fetch(self) -> ReleaseBundle:
        self.calls += 1
        return self.bundle


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def release_bundle(
    private_key: Ed25519PrivateKey,
    *,
    version: str = "1.1.0",
    target: Target | None = None,
) -> ReleaseBundle:
    selected_target = target or Target("linux", "x86_64")
    manifest = {
        "schema_version": 1,
        "version": version,
        "target": selected_target.to_dict(),
        "published_at": "2026-08-26T18:00:00Z",
        "expires_at": "2026-09-02T18:00:00Z",
        "artifact": {
            "url": "https://github.com/example/updates/releases/download/v1.1.0/wordshift",
            "sha256": hashlib.sha256(CANDIDATE).hexdigest(),
            "size": len(CANDIDATE),
        },
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return ReleaseBundle(raw, sign_bytes(raw, private_key))


def copy_candidate(
    artifact: Artifact,
    destination: Path,
    timeout: float,
    max_bytes: int,
    allow_http_for_tests: bool,
) -> str:
    assert timeout > 0
    assert max_bytes >= len(CANDIDATE)
    assert not allow_http_for_tests
    assert artifact.size == len(CANDIDATE)
    destination.write_bytes(CANDIDATE)
    return hashlib.sha256(CANDIDATE).hexdigest()


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def layout(tmp_path: Path) -> InstallLayout:
    return InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))


def installed_layout(layout: InstallLayout) -> InstallLayout:
    source = layout.root.parent / "source"
    source.write_bytes(b"version 1.0.0")
    install_artifact(source, layout, Version(1, 0, 0))
    return layout


def test_initial_install_creates_launcher_payload_and_state(layout: InstallLayout) -> None:
    source = layout.root.parent / "source"
    source.write_bytes(b"binary")

    state = install_artifact(source, layout, Version(1, 0, 0))

    assert state == InstallState.initial(Version(1, 0, 0))
    assert layout.launcher_path.read_bytes() == b"binary"
    assert layout.payload_path(Version(1, 0, 0)).read_bytes() == b"binary"
    assert read_state(layout) == state
    assert os.access(layout.launcher_path, os.X_OK)


def test_initial_install_is_idempotent_for_identical_artifact(layout: InstallLayout) -> None:
    source = layout.root.parent / "source"
    source.write_bytes(b"binary")

    first = install_artifact(source, layout, Version(1, 0, 0))
    second = install_artifact(source, layout, Version(1, 0, 0))

    assert first == second


def test_install_refuses_conflicting_existing_artifact(layout: InstallLayout) -> None:
    source = layout.root.parent / "source"
    source.write_bytes(b"binary")
    install_artifact(source, layout, Version(1, 0, 0))
    source.write_bytes(b"different")

    with pytest.raises(InstallationError, match="different content"):
        install_artifact(source, layout, Version(1, 0, 0))


def test_atomic_state_preserves_original_on_replace_failure(
    layout: InstallLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_layout(layout)
    original = layout.state_path.read_bytes()
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk failure")))

    with pytest.raises(InstallationError, match="write state"):
        write_state(layout, InstallState.initial(Version(1, 1, 0)))

    assert layout.state_path.read_bytes() == original
    assert not list(layout.root.glob(".state.json.*"))


def test_update_stages_immutable_payload_and_records_pending(
    layout: InstallLayout, private_key: Ed25519PrivateKey
) -> None:
    installed_layout(layout)
    source = StaticSource(release_bundle(private_key))
    health_calls: list[tuple[Path, Version, float]] = []

    def health_check(path: Path, version: Version, timeout: float) -> None:
        assert path.read_bytes() == CANDIDATE
        health_calls.append((path, version, timeout))

    installer = UpdateInstaller(
        layout,
        source,
        public_key_b64(private_key),
        Target("linux", "x86_64"),
        now=lambda: NOW,
        health_check=health_check,
        download=copy_candidate,
    )

    staged = installer.check_and_stage()

    assert staged == Version(1, 1, 0)
    assert layout.payload_path(staged).read_bytes() == CANDIDATE
    assert read_state(layout).pending_version == staged
    assert len(health_calls) == 1
    assert not list(layout.staging_dir.iterdir())


def test_failed_health_check_never_marks_pending(
    layout: InstallLayout, private_key: Ed25519PrivateKey
) -> None:
    installed_layout(layout)

    def fail_health(path: Path, version: Version, timeout: float) -> None:
        raise InstallationError("candidate failed health check")

    installer = UpdateInstaller(
        layout,
        StaticSource(release_bundle(private_key)),
        public_key_b64(private_key),
        Target("linux", "x86_64"),
        now=lambda: NOW,
        health_check=fail_health,
        download=copy_candidate,
    )

    with pytest.raises(InstallationError, match="failed health"):
        installer.check_and_stage()

    assert read_state(layout).pending_version is None
    assert not layout.payload_path(Version(1, 1, 0)).exists()
    assert not list(layout.staging_dir.iterdir())


def test_current_release_reports_no_update(
    layout: InstallLayout, private_key: Ed25519PrivateKey
) -> None:
    installed_layout(layout)
    installer = UpdateInstaller(
        layout,
        StaticSource(release_bundle(private_key, version="1.0.0")),
        public_key_b64(private_key),
        Target("linux", "x86_64"),
        now=lambda: NOW,
        health_check=lambda path, version, timeout: None,
        download=copy_candidate,
    )

    assert installer.check_and_stage() is None
    assert read_state(layout).pending_version is None


def test_signed_downgrade_fails_closed(
    layout: InstallLayout, private_key: Ed25519PrivateKey
) -> None:
    installed_layout(layout)
    installer = UpdateInstaller(
        layout,
        StaticSource(release_bundle(private_key, version="0.9.0")),
        public_key_b64(private_key),
        Target("linux", "x86_64"),
        now=lambda: NOW,
        health_check=lambda path, version, timeout: None,
        download=copy_candidate,
    )

    with pytest.raises(SecurityError, match="not newer"):
        installer.check_and_stage()


def test_executable_name_is_platform_specific() -> None:
    assert executable_name("windows") == "wordshift.exe"
    assert executable_name("linux") == "wordshift"


@pytest.mark.skipif(os.name == "nt", reason="Windows uses inherited per-user ACLs")
def test_managed_directories_are_private_with_permissive_umask(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))
    previous_umask = os.umask(0)
    try:
        layout.ensure_directories()
    finally:
        os.umask(previous_umask)

    for directory in (
        layout.root,
        layout.versions_dir,
        layout.staging_dir,
        layout.readiness_dir,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows uses inherited per-user ACLs")
def test_managed_directories_normalize_existing_permissions(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))
    layout.root.mkdir(mode=0o777)
    layout.versions_dir.mkdir(mode=0o777)
    layout.ensure_directories()

    assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(layout.versions_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink behavior")
def test_managed_directory_symlink_is_rejected(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))
    layout.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.versions_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallationError, match="symbolic link"):
        layout.ensure_directories()


@pytest.mark.skipif(os.name == "nt", reason="Windows uses inherited per-user ACLs")
def test_permission_normalization_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))

    def deny_chmod(path: Path, mode: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "chmod", deny_chmod)

    with pytest.raises(InstallationError, match="secure directory"):
        layout.ensure_directories()


@pytest.mark.parametrize("xdg_data_home", ["", "relative/data"])
def test_invalid_xdg_data_home_uses_home_fallback(
    tmp_path: Path, xdg_data_home: str
) -> None:
    assert default_install_root(
        system="Linux", env={"XDG_DATA_HOME": xdg_data_home}, home=tmp_path
    ) == tmp_path / ".local" / "share" / "wordshift"


def test_absolute_xdg_data_home_is_used(tmp_path: Path) -> None:
    data_home = tmp_path / "xdg"
    assert default_install_root(
        system="Linux", env={"XDG_DATA_HOME": str(data_home)}, home=tmp_path
    ) == data_home / "wordshift"
