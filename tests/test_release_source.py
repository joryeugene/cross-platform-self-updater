import hashlib
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from self_updater.models import Artifact
from self_updater.release_source import (
    HttpReleaseSource,
    ReleaseSourceError,
    stream_artifact,
)

ARTIFACT = b"candidate binary"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        routes = {
            "/manifest": b'{"schema_version":1}\n',
            "/signature": b"c2lnbmF0dXJl\n",
            "/artifact": ARTIFACT,
            "/truncated": ARTIFACT[:3],
            "/oversized": ARTIFACT + b"x",
        }
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/artifact")
            self.end_headers()
            return
        body = routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def test_http_source_fetches_bounded_raw_assets(base_url: str) -> None:
    source = HttpReleaseSource(
        f"{base_url}/manifest",
        f"{base_url}/signature",
        timeout=1,
        allow_http_for_tests=True,
    )

    bundle = source.fetch()

    assert bundle.manifest_bytes == b'{"schema_version":1}\n'
    assert bundle.signature_bytes == b"c2lnbmF0dXJl\n"


def test_http_is_rejected_outside_tests(base_url: str) -> None:
    source = HttpReleaseSource(f"{base_url}/manifest", f"{base_url}/signature", timeout=1)

    with pytest.raises(ReleaseSourceError, match="HTTPS"):
        source.fetch()


def test_https_combines_system_and_packaged_ca_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_with: list[str | None] = []
    loaded: list[str] = []
    contexts: list[object | None] = []

    class Context:
        def load_verify_locations(self, *, cafile: str) -> None:
            loaded.append(cafile)

    context = Context()

    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Length": "2"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def geturl(self) -> str:
            return "https://example.test/release"

        def read(self, limit: int) -> bytes:
            return b"ok"

    def create_default_context(*, cafile: str | None = None) -> object:
        created_with.append(cafile)
        return context

    def open_url(
        request: object,
        timeout: float,
        context: object | None = None,
    ) -> Response:
        contexts.append(context)
        return Response()

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    monkeypatch.setattr("self_updater.release_source.urlopen", open_url)

    HttpReleaseSource(
        "https://example.test/manifest",
        "https://example.test/manifest.sig",
    ).fetch()

    assert created_with == [None, None]
    assert [Path(cafile).name for cafile in loaded] == ["cacert.pem", "cacert.pem"]
    assert contexts == [context, context]


def test_stream_artifact_hashes_while_writing(base_url: str, tmp_path: Path) -> None:
    artifact = Artifact(f"{base_url}/artifact", digest(ARTIFACT), len(ARTIFACT))
    destination = tmp_path / "candidate"

    result = stream_artifact(artifact, destination, 1, 1024, allow_http_for_tests=True)

    assert result == digest(ARTIFACT)
    assert destination.read_bytes() == ARTIFACT


def test_stream_artifact_follows_safe_redirect(base_url: str, tmp_path: Path) -> None:
    artifact = Artifact(f"{base_url}/redirect", digest(ARTIFACT), len(ARTIFACT))

    stream_artifact(artifact, tmp_path / "candidate", 1, 1024, allow_http_for_tests=True)

    assert (tmp_path / "candidate").read_bytes() == ARTIFACT


@pytest.mark.parametrize(
    ("route", "size", "sha256", "message"),
    [
        ("/truncated", len(ARTIFACT), digest(ARTIFACT), "expected 16 bytes, received 3"),
        ("/oversized", len(ARTIFACT), digest(ARTIFACT), "exceeds signed size"),
        ("/artifact", len(ARTIFACT), digest(b"wrong"), "SHA-256"),
    ],
)
def test_invalid_artifact_is_removed(
    base_url: str,
    tmp_path: Path,
    route: str,
    size: int,
    sha256: str,
    message: str,
) -> None:
    destination = tmp_path / "candidate"
    artifact = Artifact(f"{base_url}{route}", sha256, size)

    with pytest.raises(ReleaseSourceError, match=message):
        stream_artifact(artifact, destination, 1, 1024, allow_http_for_tests=True)

    assert not destination.exists()


def test_global_size_limit_is_checked_before_download(base_url: str, tmp_path: Path) -> None:
    artifact = Artifact(f"{base_url}/artifact", digest(ARTIFACT), len(ARTIFACT))

    with pytest.raises(ReleaseSourceError, match="global size limit"):
        stream_artifact(artifact, tmp_path / "candidate", 1, 4, allow_http_for_tests=True)
