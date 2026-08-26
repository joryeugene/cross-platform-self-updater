from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from self_updater.models import InstallState, ManifestError, Target, Version


class InstallationError(RuntimeError):
    pass


def executable_name(os_name: str | None = None) -> str:
    selected = os_name or Target.current().os
    return "wordshift.exe" if selected == "windows" else "wordshift"


@dataclass(frozen=True)
class InstallLayout:
    root: Path
    target: Target

    @classmethod
    def for_root(cls, root: Path, target: Target | None = None) -> InstallLayout:
        return cls(root.absolute(), target or Target.current())

    @property
    def launcher_path(self) -> Path:
        return self.root / executable_name(self.target.os)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.root / "update.lock"

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def readiness_dir(self) -> Path:
        return self.root / "readiness"

    def payload_path(self, version: Version) -> Path:
        return self.versions_dir / str(version) / executable_name(self.target.os)

    def staging_path(self, token: str) -> Path:
        if not token or any(character not in "0123456789abcdef" for character in token):
            raise InstallationError("staging token is invalid")
        return self.staging_dir / token

    def ensure_directories(self) -> None:
        for directory in (self.root, self.versions_dir, self.staging_dir, self.readiness_dir):
            ensure_private_directory(directory)


def ensure_private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise InstallationError(f"managed directory is a symbolic link: {directory}")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise InstallationError(f"managed path is not a directory: {directory}")
        if os.name != "nt":
            directory.chmod(0o700)
            mode = directory.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
                raise InstallationError(f"could not secure directory: {directory}")
    except OSError as error:
        raise InstallationError(f"could not secure directory {directory}: {error}") from error


def default_install_root(
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    target = Target.current(system=system)
    values = os.environ if env is None else env
    user_home = Path.home() if home is None else home
    if target.os == "windows":
        local = values.get("LOCALAPPDATA")
        if not local:
            raise InstallationError("LOCALAPPDATA is not set")
        return Path(local) / "WordShift"
    if target.os == "macos":
        return user_home / "Library" / "Application Support" / "WordShift"
    configured = values.get("XDG_DATA_HOME")
    data_home = Path(configured) if configured and Path(configured).is_absolute() else None
    return (data_home or user_home / ".local" / "share") / "wordshift"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_state(layout: InstallLayout, state: InstallState) -> None:
    layout.ensure_directories()
    payload = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".state.json.", dir=layout.root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, layout.state_path)
        _fsync_directory(layout.root)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise InstallationError(f"could not write state: {error}") from error


def read_state(layout: InstallLayout) -> InstallState:
    try:
        raw = layout.state_path.read_bytes()
        if len(raw) > 64 * 1024:
            raise InstallationError("state file exceeds size limit")
        return InstallState.from_dict(json.loads(raw))
    except InstallationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ManifestError) as error:
        raise InstallationError(f"could not read state: {error}") from error
