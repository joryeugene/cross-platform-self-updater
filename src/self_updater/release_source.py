from __future__ import annotations

import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi

from self_updater.models import Artifact

MANIFEST_LIMIT = 256 * 1024
SIGNATURE_LIMIT = 8 * 1024
CHUNK_SIZE = 64 * 1024


class ReleaseSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseBundle:
    manifest_bytes: bytes
    signature_bytes: bytes


class ReleaseSource(Protocol):
    def fetch(self) -> ReleaseBundle: ...


def _require_safe_url(url: str, allow_http_for_tests: bool) -> None:
    parsed = urlsplit(url)
    allowed = {"https", "http"} if allow_http_for_tests else {"https"}
    if parsed.scheme not in allowed or not parsed.netloc:
        raise ReleaseSourceError("release URL must use HTTPS")


def _content_length(headers: object) -> int | None:
    get = getattr(headers, "get", None)
    if get is None:
        return None
    raw = get("Content-Length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except (TypeError, ValueError) as error:
        raise ReleaseSourceError("response Content-Length is invalid") from error
    if length < 0:
        raise ReleaseSourceError("response Content-Length is invalid")
    return length


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=os.environ.get("SSL_CERT_FILE") or certifi.where())
    return context


def _fetch_bytes(
    url: str,
    timeout: float,
    limit: int,
    allow_http_for_tests: bool,
) -> bytes:
    _require_safe_url(url, allow_http_for_tests)
    request = Request(url, headers={"User-Agent": "WordShift-Updater/1"})
    try:
        with urlopen(request, timeout=timeout, context=_tls_context()) as response:
            _require_safe_url(response.geturl(), allow_http_for_tests)
            length = _content_length(response.headers)
            if length is not None and length > limit:
                raise ReleaseSourceError("release response exceeds size limit")
            body = cast(bytes, response.read(limit + 1))
    except ReleaseSourceError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ReleaseSourceError(f"release request failed: {error}") from error
    if len(body) > limit:
        raise ReleaseSourceError("release response exceeds size limit")
    return body


@dataclass(frozen=True)
class HttpReleaseSource:
    manifest_url: str
    signature_url: str
    timeout: float = 15.0
    allow_http_for_tests: bool = False

    def fetch(self) -> ReleaseBundle:
        return ReleaseBundle(
            manifest_bytes=_fetch_bytes(
                self.manifest_url,
                self.timeout,
                MANIFEST_LIMIT,
                self.allow_http_for_tests,
            ),
            signature_bytes=_fetch_bytes(
                self.signature_url,
                self.timeout,
                SIGNATURE_LIMIT,
                self.allow_http_for_tests,
            ),
        )


def stream_artifact(
    artifact: Artifact,
    destination: Path,
    timeout: float,
    max_bytes: int,
    allow_http_for_tests: bool = False,
) -> str:
    if artifact.size > max_bytes:
        raise ReleaseSourceError("artifact exceeds global size limit")
    _require_safe_url(artifact.url, allow_http_for_tests)
    request = Request(artifact.url, headers={"User-Agent": "WordShift-Updater/1"})
    digest = hashlib.sha256()
    received = 0
    try:
        with urlopen(request, timeout=timeout, context=_tls_context()) as response:
            _require_safe_url(response.geturl(), allow_http_for_tests)
            length = _content_length(response.headers)
            if length is not None and length > artifact.size:
                raise ReleaseSourceError("artifact exceeds signed size")
            with destination.open("xb") as output:
                while chunk := response.read(CHUNK_SIZE):
                    received += len(chunk)
                    if received > artifact.size:
                        raise ReleaseSourceError("artifact exceeds signed size")
                    if received > max_bytes:
                        raise ReleaseSourceError("artifact exceeds global size limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except ReleaseSourceError:
        destination.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        destination.unlink(missing_ok=True)
        raise ReleaseSourceError(f"artifact download failed: {error}") from error

    if received != artifact.size:
        destination.unlink(missing_ok=True)
        raise ReleaseSourceError(f"expected {artifact.size} bytes, received {received}")
    actual = digest.hexdigest()
    if actual != artifact.sha256:
        destination.unlink(missing_ok=True)
        raise ReleaseSourceError("artifact SHA-256 does not match signed manifest")
    return actual
