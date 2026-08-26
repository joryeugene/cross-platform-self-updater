import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from self_updater.models import ManifestError, Target, Version
from self_updater.security import ReleasePolicy, SecurityError, sign_bytes, verify_release

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def manifest_bytes(**changes: object) -> bytes:
    data: dict[str, object] = {
        "schema_version": 1,
        "version": "1.1.0",
        "target": {"os": "linux", "arch": "x86_64"},
        "published_at": "2026-08-26T18:00:00Z",
        "expires_at": "2026-09-02T18:00:00Z",
        "artifact": {
            "url": "https://github.com/example/updates/releases/download/v1.1.0/wordshift",
            "sha256": "ab" * 32,
            "size": 12,
        },
    }
    data.update(changes)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def policy(**changes: object) -> ReleasePolicy:
    values: dict[str, Any] = {
        "current_version": Version(1, 0, 0),
        "target": Target("linux", "x86_64"),
        "now": NOW,
    }
    values.update(changes)
    return ReleasePolicy(**values)


def signed(
    private_key: Ed25519PrivateKey, **changes: object
) -> tuple[bytes, bytes, str]:
    raw = manifest_bytes(**changes)
    return raw, sign_bytes(raw, private_key), public_key_b64(private_key)


def test_signature_covers_exact_manifest_bytes(private_key: Ed25519PrivateKey) -> None:
    raw, signature, public = signed(private_key)

    manifest = verify_release(raw, signature, public, policy())
    assert manifest.version == Version(1, 1, 0)

    with pytest.raises(SecurityError, match="signature"):
        verify_release(raw + b" ", signature, public, policy())


def test_invalid_signature_is_rejected_before_json_parse(
    private_key: Ed25519PrivateKey,
) -> None:
    raw = b"not json"
    signature = sign_bytes(b"different bytes", private_key)

    with pytest.raises(SecurityError, match="signature"):
        verify_release(raw, signature, public_key_b64(private_key), policy())


@pytest.mark.parametrize(
    ("manifest_changes", "policy_changes", "message"),
    [
        ({"expires_at": "2026-08-26T18:00:00Z"}, {}, "expired"),
        ({"version": "1.0.0"}, {}, "not newer"),
        ({"version": "0.9.0"}, {}, "not newer"),
        ({"target": {"os": "windows", "arch": "x86_64"}}, {}, "target"),
        (
            {"published_at": "2026-08-26T18:05:01Z"},
            {"max_future_skew": timedelta(minutes=5)},
            "future",
        ),
        (
            {
                "artifact": {
                    "url": "http://example.test/wordshift",
                    "sha256": "ab" * 32,
                    "size": 12,
                }
            },
            {},
            "HTTPS",
        ),
        (
            {
                "artifact": {
                    "url": "https://example.test/wordshift",
                    "sha256": "ab" * 32,
                    "size": 0,
                }
            },
            {},
            "size",
        ),
        (
            {
                "artifact": {
                    "url": "https://example.test/wordshift",
                    "sha256": "ab" * 32,
                    "size": 101,
                }
            },
            {"max_artifact_bytes": 100},
            "size",
        ),
    ],
)
def test_release_policy_rejects_unsafe_manifest(
    private_key: Ed25519PrivateKey,
    manifest_changes: dict[str, object],
    policy_changes: dict[str, object],
    message: str,
) -> None:
    raw, signature, public = signed(private_key, **manifest_changes)

    with pytest.raises(SecurityError, match=message):
        verify_release(raw, signature, public, policy(**policy_changes))


@pytest.mark.parametrize(
    ("signature", "public_key", "message"),
    [
        (b"not-base64\n", base64.b64encode(b"x" * 32).decode(), "signature"),
        (base64.b64encode(b"x" * 63) + b"\n", base64.b64encode(b"x" * 32).decode(), "64"),
        (base64.b64encode(b"x" * 64) + b"\n", "not-base64", "public key"),
        (base64.b64encode(b"x" * 64) + b"\n", base64.b64encode(b"x" * 31).decode(), "32"),
    ],
)
def test_malformed_key_material_is_rejected(
    signature: bytes, public_key: str, message: str
) -> None:
    with pytest.raises(SecurityError, match=message):
        verify_release(manifest_bytes(), signature, public_key, policy())


def test_valid_signature_does_not_hide_invalid_manifest(
    private_key: Ed25519PrivateKey,
) -> None:
    raw = manifest_bytes(unexpected=True)

    with pytest.raises(ManifestError, match="unknown keys"):
        verify_release(raw, sign_bytes(raw, private_key), public_key_b64(private_key), policy())
