from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class ManifestError(ValueError):
    pass


class UnsupportedPlatformError(ManifestError):
    pass


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ManifestError(f"{field} must be an object")
    return value


def _keys(data: dict[str, Any], expected: set[str], field: str) -> None:
    missing = sorted(expected - data.keys())
    unknown = sorted(data.keys() - expected)
    if missing:
        raise ManifestError(f"{field} missing keys: {', '.join(missing)}")
    if unknown:
        raise ManifestError(f"{field} unknown keys: {', '.join(unknown)}")


def _string(data: dict[str, Any], key: str, field: str | None = None) -> str:
    value = data[key]
    name = field or key
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be a non-empty string")
    return value


def _timestamp(data: dict[str, Any], key: str) -> datetime:
    value = _string(data, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"{key} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ManifestError(f"{key} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
        if match is None:
            raise ManifestError("version must be MAJOR.MINOR.PATCH")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Target:
    os: str
    arch: str

    @classmethod
    def current(cls, system: str | None = None, machine: str | None = None) -> Target:
        system_name = (system or platform.system()).lower()
        machine_name = (machine or platform.machine()).lower()
        os_name = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(
            system_name
        )
        arch_name = {
            "amd64": "x86_64",
            "x86_64": "x86_64",
            "arm64": "arm64",
            "aarch64": "arm64",
        }.get(machine_name)
        if os_name is None or arch_name is None:
            raise UnsupportedPlatformError(f"unsupported platform: {system_name}/{machine_name}")
        return cls(os_name, arch_name)

    @classmethod
    def from_dict(cls, value: object) -> Target:
        data = _object(value, "target")
        _keys(data, {"os", "arch"}, "target")
        target = cls(_string(data, "os", "target.os"), _string(data, "arch", "target.arch"))
        if target.os not in {"windows", "macos", "linux"}:
            raise ManifestError(f"unsupported target operating system: {target.os}")
        if target.arch not in {"x86_64", "arm64"}:
            raise ManifestError(f"unsupported target architecture: {target.arch}")
        return target

    def to_dict(self) -> dict[str, str]:
        return {"os": self.os, "arch": self.arch}


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    size: int

    @classmethod
    def from_dict(cls, value: object) -> Artifact:
        data = _object(value, "artifact")
        _keys(data, {"url", "sha256", "size"}, "artifact")
        digest = _string(data, "sha256", "artifact.sha256")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ManifestError("artifact.sha256 must be 64 lowercase hexadecimal characters")
        size = data["size"]
        if type(size) is not int:
            raise ManifestError("artifact.size must be an integer")
        return cls(_string(data, "url", "artifact.url"), digest, size)

    def to_dict(self) -> dict[str, object]:
        return {"url": self.url, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    version: Version
    target: Target
    published_at: datetime
    expires_at: datetime
    artifact: Artifact

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReleaseManifest:
        try:
            decoded = raw.decode("utf-8")
            data = _object(json.loads(decoded), "manifest")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestError("manifest must be valid UTF-8 JSON") from error
        _keys(
            data,
            {"schema_version", "version", "target", "published_at", "expires_at", "artifact"},
            "manifest",
        )
        schema_version = data["schema_version"]
        if type(schema_version) is not int:
            raise ManifestError("schema_version must be an integer")
        if schema_version != 1:
            raise ManifestError(f"unsupported manifest schema: {schema_version}")
        return cls(
            schema_version=schema_version,
            version=Version.parse(_string(data, "version")),
            target=Target.from_dict(data["target"]),
            published_at=_timestamp(data, "published_at"),
            expires_at=_timestamp(data, "expires_at"),
            artifact=Artifact.from_dict(data["artifact"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "version": str(self.version),
            "target": self.target.to_dict(),
            "published_at": self.published_at.isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True)
class InstallState:
    schema_version: int
    current_version: Version
    previous_version: Version | None
    pending_version: Version | None

    @classmethod
    def initial(cls, version: Version) -> InstallState:
        return cls(1, version, None, None)

    @classmethod
    def from_dict(cls, value: object) -> InstallState:
        data = _object(value, "state")
        _keys(
            data,
            {"schema_version", "current_version", "previous_version", "pending_version"},
            "state",
        )
        schema_version = data["schema_version"]
        if type(schema_version) is not int:
            raise ManifestError("state.schema_version must be an integer")
        if schema_version != 1:
            raise ManifestError(f"unsupported state schema: {schema_version}")

        def optional_version(key: str) -> Version | None:
            raw = data[key]
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise ManifestError(f"state.{key} must be a version string or null")
            return Version.parse(raw)

        current = data["current_version"]
        if not isinstance(current, str):
            raise ManifestError("state.current_version must be a version string")
        return cls(
            schema_version,
            Version.parse(current),
            optional_version("previous_version"),
            optional_version("pending_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "current_version": str(self.current_version),
            "previous_version": None
            if self.previous_version is None
            else str(self.previous_version),
            "pending_version": None if self.pending_version is None else str(self.pending_version),
        }
