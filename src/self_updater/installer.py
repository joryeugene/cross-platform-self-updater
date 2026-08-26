from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from self_updater.models import Artifact, InstallState, ReleaseManifest, Target, Version
from self_updater.paths import (
    InstallationError,
    InstallLayout,
    ensure_private_directory,
    read_state,
    write_state,
)
from self_updater.release_source import ReleaseSource, stream_artifact
from self_updater.security import ReleaseNotNewerError, ReleasePolicy, verify_release

MAX_ARTIFACT_BYTES = 200 * 1024 * 1024
HealthCheck = Callable[[Path, Version, float], None]
Download = Callable[[Artifact, Path, float, int, bool], str]


@contextmanager
def update_lock(layout: InstallLayout, timeout: float) -> Iterator[None]:
    layout.ensure_directories()
    try:
        with FileLock(layout.lock_path, timeout=timeout):
            yield
    except Timeout as error:
        raise InstallationError("another update is already running") from error


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _atomic_copy(source: Path, destination: Path, executable: bool) -> None:
    ensure_private_directory(destination.parent)
    if destination.exists():
        if _digest(source) == _digest(destination):
            return
        raise InstallationError(f"{destination} already exists with different content")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=64 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        if executable:
            temporary.chmod(0o755)
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise InstallationError(f"could not install {destination.name}: {error}") from error


def install_artifact(source: Path, layout: InstallLayout, version: Version) -> InstallState:
    if not source.is_file():
        raise InstallationError(f"artifact does not exist: {source}")
    with update_lock(layout, timeout=10):
        _atomic_copy(source, layout.launcher_path, executable=layout.target.os != "windows")
        _atomic_copy(source, layout.payload_path(version), executable=layout.target.os != "windows")
        if layout.state_path.exists():
            state = read_state(layout)
            if state.current_version != version:
                raise InstallationError(
                    f"installation already points to version {state.current_version}"
                )
            return state
        state = InstallState.initial(version)
        write_state(layout, state)
        return state


class UpdateInstaller:
    def __init__(
        self,
        layout: InstallLayout,
        source: ReleaseSource,
        public_key_b64: str,
        target: Target,
        *,
        now: Callable[[], datetime] | None = None,
        health_check: HealthCheck,
        download: Download = stream_artifact,
        request_timeout: float = 15.0,
        health_timeout: float = 10.0,
        lock_timeout: float = 2.0,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        allow_http_for_tests: bool = False,
    ) -> None:
        self.layout = layout
        self.source = source
        self.public_key_b64 = public_key_b64
        self.target = target
        self.now = now or (lambda: datetime.now(UTC))
        self.health_check = health_check
        self.download = download
        self.request_timeout = request_timeout
        self.health_timeout = health_timeout
        self.lock_timeout = lock_timeout
        self.max_artifact_bytes = max_artifact_bytes
        self.allow_http_for_tests = allow_http_for_tests

    def _release(self, current: Version) -> ReleaseManifest | None:
        bundle = self.source.fetch()
        try:
            return verify_release(
                bundle.manifest_bytes,
                bundle.signature_bytes,
                self.public_key_b64,
                ReleasePolicy(
                    current_version=current,
                    target=self.target,
                    now=self.now(),
                    max_artifact_bytes=self.max_artifact_bytes,
                ),
            )
        except ReleaseNotNewerError as error:
            if error.candidate == error.current:
                return None
            raise

    def check(self) -> ReleaseManifest | None:
        with update_lock(self.layout, self.lock_timeout):
            return self._release(read_state(self.layout).current_version)

    def check_and_stage(self) -> Version | None:
        with update_lock(self.layout, self.lock_timeout):
            state = read_state(self.layout)
            manifest = self._release(state.current_version)
            if manifest is None:
                return None
            if state.pending_version is not None and state.pending_version != manifest.version:
                raise InstallationError(f"version {state.pending_version} is already pending")
            token = uuid.uuid4().hex
            staging = self.layout.staging_path(token)
            ensure_private_directory(staging)
            candidate = staging / self.layout.launcher_path.name
            final_payload = self.layout.payload_path(manifest.version)
            try:
                self.download(
                    manifest.artifact,
                    candidate,
                    self.request_timeout,
                    self.max_artifact_bytes,
                    self.allow_http_for_tests,
                )
                if self.layout.target.os != "windows":
                    candidate.chmod(0o755)
                self.health_check(candidate, manifest.version, self.health_timeout)
                if final_payload.exists():
                    if final_payload.parent.is_symlink() or final_payload.is_symlink():
                        raise InstallationError(
                            f"version {manifest.version} contains a symbolic link"
                        )
                    if _digest(final_payload) != manifest.artifact.sha256:
                        raise InstallationError(
                            f"version {manifest.version} already exists with different content"
                        )
                else:
                    os.replace(staging, final_payload.parent)
                write_state(self.layout, replace(state, pending_version=manifest.version))
                return manifest.version
            except InstallationError:
                raise
            except OSError as error:
                raise InstallationError(f"could not stage update: {error}") from error
            finally:
                shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "InstallationError",
    "UpdateInstaller",
    "install_artifact",
    "update_lock",
]
