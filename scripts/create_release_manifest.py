from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from self_updater.models import Artifact, ReleaseManifest, Target, Version
from self_updater.security import sign_bytes


class ReleaseCreationError(RuntimeError):
    pass


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseCreationError(f"cannot read artifact: {error}") from error
    return digest.hexdigest()


def create_manifest(
    artifact_path: Path,
    artifact_url: str,
    version: Version,
    target: Target,
    published_at: datetime,
    expires_at: datetime,
) -> bytes:
    try:
        size = artifact_path.stat().st_size
    except OSError as error:
        raise ReleaseCreationError(f"cannot stat artifact: {error}") from error
    if not artifact_path.is_file() or size <= 0:
        raise ReleaseCreationError("artifact must be a non-empty regular file")
    url = urlsplit(artifact_url)
    if url.scheme != "https" or not url.netloc:
        raise ReleaseCreationError("artifact URL must use HTTPS")
    if published_at.tzinfo is None or expires_at.tzinfo is None:
        raise ReleaseCreationError("release timestamps must include a timezone")
    if expires_at <= published_at:
        raise ReleaseCreationError("release expiry must be after publication")

    manifest = ReleaseManifest(
        schema_version=1,
        version=version,
        target=Target.from_dict(target.to_dict()),
        published_at=published_at.astimezone(UTC),
        expires_at=expires_at.astimezone(UTC),
        artifact=Artifact(url=artifact_url, sha256=_sha256(artifact_path), size=size),
    )
    raw = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return raw + b"\n"


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        metadata = path.stat()
        encoded = path.read_bytes().strip()
    except OSError as error:
        raise ReleaseCreationError(f"cannot read private key: {error}") from error
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ReleaseCreationError("private key permissions must be owner-only")
    try:
        seed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ReleaseCreationError("private key must be valid base64") from error
    if len(seed) != 32:
        raise ReleaseCreationError("private key must decode to 32 bytes")
    try:
        return Ed25519PrivateKey.from_private_bytes(seed)
    except ValueError as error:
        raise ReleaseCreationError("private key is invalid") from error


def create_signed_release(
    artifact_path: Path,
    artifact_url: str,
    version: Version,
    target: Target,
    published_at: datetime,
    expires_at: datetime,
    private_key_path: Path,
) -> tuple[bytes, bytes]:
    raw = create_manifest(
        artifact_path,
        artifact_url,
        version,
        target,
        published_at,
        expires_at,
    )
    return raw, sign_bytes(raw, _load_private_key(private_key_path))


def _atomic_write(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=path.parent) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise ReleaseCreationError(f"cannot write {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_outputs(output: Path, raw: bytes, signature: bytes, force: bool) -> Path:
    signature_path = output.with_name(output.name + ".sig")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReleaseCreationError(f"cannot create output directory: {error}") from error
    reserved: list[Path] = []
    try:
        if not force:
            for path in (output, signature_path):
                with path.open("xb"):
                    pass
                reserved.append(path)
        _atomic_write(output, raw)
        _atomic_write(signature_path, signature)
    except FileExistsError as error:
        for path in reserved:
            path.unlink(missing_ok=True)
        raise ReleaseCreationError(f"output already exists: {error.filename}") from error
    except ReleaseCreationError:
        if not force:
            signature_path.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
        raise
    return signature_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and sign a WordShift release manifest")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--version", type=Version.parse, required=True)
    parser.add_argument("--os", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--arch", choices=("x86_64", "arm64"), required=True)
    parser.add_argument("--published-at", type=_timestamp, required=True)
    parser.add_argument("--expires-at", type=_timestamp, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    raw, signature = create_signed_release(
        arguments.artifact,
        arguments.artifact_url,
        arguments.version,
        Target(arguments.os, arguments.arch),
        arguments.published_at,
        arguments.expires_at,
        arguments.private_key,
    )
    signature_path = _write_outputs(arguments.output, raw, signature, arguments.force)
    print(f"wrote {arguments.output} and {signature_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
