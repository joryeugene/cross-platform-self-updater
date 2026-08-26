import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from scripts.create_release_manifest import (
    ReleaseCreationError,
    create_signed_release,
    main,
)
from self_updater.models import Target, Version

PUBLISHED = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def private_key_file(tmp_path: Path) -> Path:
    seed = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    path = tmp_path / "release.key"
    path.write_bytes(base64.b64encode(seed) + b"\n")
    path.chmod(0o600)
    return path


def _public_key(private_key_file: Path) -> Ed25519PublicKey:
    seed = base64.b64decode(private_key_file.read_bytes().strip(), validate=True)
    return Ed25519PrivateKey.from_private_bytes(seed).public_key()


def test_release_script_signs_exact_written_bytes(
    tmp_path: Path, private_key_file: Path
) -> None:
    artifact = tmp_path / "wordshift-linux-x86_64"
    artifact.write_bytes(b"release bytes")
    raw, signature = create_signed_release(
        artifact,
        "https://github.com/example/updates/releases/download/v1.1.0/wordshift-linux-x86_64",
        Version(1, 1, 0),
        Target("linux", "x86_64"),
        PUBLISHED,
        EXPIRES,
        private_key_file,
    )

    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert raw == canonical + b"\n"
    decoded_signature = base64.b64decode(signature.strip(), validate=True)
    _public_key(private_key_file).verify(decoded_signature, raw)
    assert json.loads(raw)["artifact"] == {
        "sha256": "ff7a5e6429d2c8511521e4abf41cd54a3e525ef4a1f24f8d1c67ede9d17874dd",
        "size": 13,
        "url": (
            "https://github.com/example/updates/releases/download/"
            "v1.1.0/wordshift-linux-x86_64"
        ),
    }


def test_cli_writes_manifest_and_signature_without_exposing_key(
    tmp_path: Path, private_key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "wordshift"
    artifact.write_bytes(b"binary")
    output = tmp_path / "manifest.json"

    result = main(
        [
            "--artifact",
            str(artifact),
            "--artifact-url",
            "https://example.com/wordshift",
            "--version",
            "1.1.0",
            "--os",
            "linux",
            "--arch",
            "x86_64",
            "--published-at",
            "2026-08-26T12:00:00Z",
            "--expires-at",
            "2026-09-02T12:00:00Z",
            "--private-key",
            str(private_key_file),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert output.is_file()
    signature_path = output.with_name(output.name + ".sig")
    _public_key(private_key_file).verify(
        base64.b64decode(signature_path.read_bytes().strip(), validate=True), output.read_bytes()
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert private_key_file.read_text().strip() not in captured.out


def test_cli_refuses_to_overwrite_either_output(
    tmp_path: Path, private_key_file: Path
) -> None:
    artifact = tmp_path / "wordshift"
    artifact.write_bytes(b"binary")
    output = tmp_path / "manifest.json"
    output.write_text("existing")

    with pytest.raises(ReleaseCreationError, match="already exists"):
        main(
            [
                "--artifact",
                str(artifact),
                "--artifact-url",
                "https://example.com/wordshift",
                "--version",
                "1.1.0",
                "--os",
                "linux",
                "--arch",
                "x86_64",
                "--published-at",
                "2026-08-26T12:00:00Z",
                "--expires-at",
                "2026-09-02T12:00:00Z",
                "--private-key",
                str(private_key_file),
                "--output",
                str(output),
            ]
        )

    assert output.read_text() == "existing"


@pytest.mark.skipif(os.name == "nt", reason="POSIX key permissions")
def test_release_script_rejects_group_readable_key(
    tmp_path: Path, private_key_file: Path
) -> None:
    artifact = tmp_path / "wordshift"
    artifact.write_bytes(b"binary")
    private_key_file.chmod(0o640)

    with pytest.raises(ReleaseCreationError, match="permissions"):
        create_signed_release(
            artifact,
            "https://example.com/wordshift",
            Version(1, 1, 0),
            Target("linux", "x86_64"),
            PUBLISHED,
            EXPIRES,
            private_key_file,
        )
