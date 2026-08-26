from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from self_updater.models import ManifestError, ReleaseManifest, Target, Version


class SecurityError(ManifestError):
    pass


class ReleaseNotNewerError(SecurityError):
    def __init__(self, candidate: Version, current: Version) -> None:
        self.candidate = candidate
        self.current = current
        super().__init__("release version is not newer than current version")


@dataclass(frozen=True)
class ReleasePolicy:
    current_version: Version
    target: Target
    now: datetime
    max_artifact_bytes: int = 200 * 1024 * 1024
    max_future_skew: timedelta = timedelta(minutes=5)


def _decode(value: bytes | str, label: str, expected_size: int) -> bytes:
    encoded = value.strip()
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SecurityError(f"{label} must be valid base64") from error
    if len(decoded) != expected_size:
        raise SecurityError(f"{label} must decode to {expected_size} bytes")
    return decoded


def sign_bytes(raw: bytes, private_key: Ed25519PrivateKey) -> bytes:
    return base64.b64encode(private_key.sign(raw)) + b"\n"


def verify_release(
    raw_manifest: bytes,
    signature_file: bytes,
    public_key_b64: str,
    policy: ReleasePolicy,
) -> ReleaseManifest:
    public_key = _decode(public_key_b64, "public key", 32)
    signature = _decode(signature_file, "signature", 64)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, raw_manifest)
    except (InvalidSignature, ValueError) as error:
        raise SecurityError("release signature is invalid") from error

    manifest = ReleaseManifest.from_bytes(raw_manifest)
    if manifest.version <= policy.current_version:
        raise ReleaseNotNewerError(manifest.version, policy.current_version)
    if manifest.target != policy.target:
        raise SecurityError("release target does not match this platform")
    if manifest.expires_at <= policy.now:
        raise SecurityError("release manifest is expired")
    if manifest.published_at > policy.now + policy.max_future_skew:
        raise SecurityError("release publication time is too far in the future")
    if not 0 < manifest.artifact.size <= policy.max_artifact_bytes:
        raise SecurityError("artifact size is outside the allowed range")
    url = urlsplit(manifest.artifact.url)
    if url.scheme != "https" or not url.netloc:
        raise SecurityError("artifact URL must use HTTPS")
    return manifest
