import base64
import hashlib
import ipaddress
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from scripts.create_release_manifest import create_signed_release
from self_updater.health import run_candidate_health
from self_updater.installer import UpdateInstaller, install_artifact
from self_updater.models import Artifact, Target, Version
from self_updater.paths import InstallLayout, read_state
from self_updater.release_config import RELEASE_PUBLIC_KEY_B64
from self_updater.release_source import ReleaseBundle

OLD_VERSION = Version(1, 0, 0)
NEW_VERSION = Version(1, 2, 0)


class StaticSource:
    def __init__(self, bundle: ReleaseBundle) -> None:
        self.bundle = bundle

    def fetch(self) -> ReleaseBundle:
        return self.bundle


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _required_path(name: str) -> Path:
    configured = os.environ.get(name)
    if configured is None:
        pytest.skip(f"{name} is not set")
    return Path(configured).resolve()


def _public_key(private_key: Path) -> str:
    seed = base64.b64decode(private_key.read_bytes().strip(), validate=True)
    return base64.b64encode(
        Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode()


@contextmanager
def _https_server(directory: Path) -> Iterator[tuple[str, Path]]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "test-ca.pem"
    key_path = directory / "test-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"https://{host}:{port}", certificate_path
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _prepare_update(
    tmp_path: Path, current: Path, candidate: Path, private_key: Path
) -> tuple[InstallLayout, UpdateInstaller]:
    target = Target.current()
    published = datetime.now(UTC) - timedelta(minutes=1)
    expires = published + timedelta(days=1)
    raw, signature = create_signed_release(
        candidate,
        "https://example.invalid/releases/v1.2.0/wordshift",
        NEW_VERSION,
        target,
        published,
        expires,
        private_key,
    )
    run_candidate_health(candidate, NEW_VERSION, 10)
    pinned_key = _public_key(private_key)

    def copy_download(
        artifact: Artifact,
        destination: Path,
        timeout: float,
        max_bytes: int,
        allow_http_for_tests: bool,
    ) -> str:
        assert timeout > 0
        assert artifact.size <= max_bytes
        assert not allow_http_for_tests
        shutil.copyfile(candidate, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        assert destination.stat().st_size == artifact.size
        assert digest == artifact.sha256
        return digest

    layout = InstallLayout.for_root(tmp_path / "install", target)
    install_artifact(current, layout, OLD_VERSION)
    installer = UpdateInstaller(
        layout,
        StaticSource(ReleaseBundle(raw, signature)),
        pinned_key,
        target,
        now=lambda: datetime.now(UTC),
        health_check=run_candidate_health,
        download=copy_download,
    )
    return layout, installer


def _live_https_update(
    tmp_path: Path, candidate: Path, *, tamper: bool = False
) -> tuple[InstallLayout, subprocess.CompletedProcess[str], str]:
    current = _required_path("WORDSHIFT_CURRENT_BINARY")
    private_key = _required_path("WORDSHIFT_TEST_PRIVATE_KEY")
    standalone = tmp_path / current.name
    shutil.copyfile(current, standalone)
    if os.name != "nt":
        standalone.chmod(0o755)
    root = tmp_path / "install"
    subprocess.run(
        [str(standalone), "install", "--root", str(root), "--artifact", str(standalone)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    layout = InstallLayout.for_root(root, Target.current())
    launcher_hash = hashlib.sha256(layout.launcher_path.read_bytes()).hexdigest()
    feed = tmp_path / "feed"
    feed.mkdir()
    with _https_server(feed) as (base_url, certificate):
        served_candidate = feed / layout.launcher_path.name
        shutil.copyfile(candidate, served_candidate)
        published = datetime.now(UTC) - timedelta(minutes=1)
        raw, signature = create_signed_release(
            served_candidate,
            f"{base_url}/{served_candidate.name}",
            NEW_VERSION,
            Target.current(),
            published,
            published + timedelta(days=1),
            private_key,
        )
        manifest = feed / "manifest.json"
        manifest.write_bytes(raw)
        manifest.with_name(manifest.name + ".sig").write_bytes(signature)
        if tamper:
            with served_candidate.open("ab") as artifact:
                artifact.write(b"tampered")

        command = [
            str(layout.launcher_path),
            "update",
            "--manifest-url",
            f"{base_url}/{manifest.name}",
        ]
        public_key = _public_key(private_key)
        if public_key != RELEASE_PUBLIC_KEY_B64:
            command.extend(["--public-key", public_key])
        environment = os.environ.copy()
        environment["SSL_CERT_FILE"] = str(certificate)
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    return layout, result, launcher_hash


@pytest.mark.packaging
def test_baseline_does_not_have_candidate_json_output() -> None:
    result = subprocess.run(
        [
            str(_required_path("WORDSHIFT_CURRENT_BINARY")),
            "transform",
            "--json",
            "Hello, world!",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --json" in result.stderr


@pytest.mark.packaging
def test_baseline_does_not_have_candidate_rot13_output() -> None:
    result = subprocess.run(
        [
            str(_required_path("WORDSHIFT_CURRENT_BINARY")),
            "transform",
            "--rot13",
            "Hello, world!",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --rot13" in result.stderr


@pytest.mark.packaging
def test_signed_packaged_update_promotes_through_real_launcher(tmp_path: Path) -> None:
    layout, installer = _prepare_update(
        tmp_path,
        _required_path("WORDSHIFT_CURRENT_BINARY"),
        _required_path("WORDSHIFT_CANDIDATE_BINARY"),
        _required_path("WORDSHIFT_TEST_PRIVATE_KEY"),
    )

    assert installer.check_and_stage() == NEW_VERSION
    launched = subprocess.run(
        [str(layout.launcher_path), "version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert launched.stdout == "WordShift 1.2.0\n"
    state = read_state(layout)
    assert state.current_version == NEW_VERSION
    assert state.previous_version == OLD_VERSION
    assert state.pending_version is None


@pytest.mark.packaging
def test_unready_packaged_update_rolls_back_through_real_launcher(tmp_path: Path) -> None:
    layout, installer = _prepare_update(
        tmp_path,
        _required_path("WORDSHIFT_CURRENT_BINARY"),
        _required_path("WORDSHIFT_BROKEN_BINARY"),
        _required_path("WORDSHIFT_TEST_PRIVATE_KEY"),
    )

    assert installer.check_and_stage() == NEW_VERSION
    launched = subprocess.run(
        [str(layout.launcher_path), "version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert launched.stdout.splitlines()[-1] == "WordShift 1.0.0"
    state = read_state(layout)
    assert state.current_version == OLD_VERSION
    assert state.previous_version is None
    assert state.pending_version is None


@pytest.mark.packaging
def test_https_update_downloads_and_promotes_through_installed_launcher(
    tmp_path: Path,
) -> None:
    layout, result, launcher_hash = _live_https_update(
        tmp_path, _required_path("WORDSHIFT_CANDIDATE_BINARY")
    )

    assert result.returncode == 0, result.stderr
    state = read_state(layout)
    assert state.current_version == NEW_VERSION
    assert state.previous_version == OLD_VERSION
    assert state.pending_version is None
    assert hashlib.sha256(layout.launcher_path.read_bytes()).hexdigest() == launcher_hash
    transformed = subprocess.run(
        [
            str(layout.launcher_path),
            "transform",
            "--rot13",
            "--json",
            "Hello, world!",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert transformed.stdout == (
        '{"input": "Hello, world!", "output": "Uryyb, jbeyq!"}\n'
    )


@pytest.mark.packaging
def test_https_update_rolls_back_an_unready_candidate(tmp_path: Path) -> None:
    layout, result, launcher_hash = _live_https_update(
        tmp_path, _required_path("WORDSHIFT_BROKEN_BINARY")
    )

    assert result.returncode == 1
    assert "kept 1.0.0" in result.stderr
    state = read_state(layout)
    assert state.current_version == OLD_VERSION
    assert state.previous_version is None
    assert state.pending_version is None
    assert hashlib.sha256(layout.launcher_path.read_bytes()).hexdigest() == launcher_hash


@pytest.mark.packaging
def test_https_update_rejects_a_tampered_artifact(tmp_path: Path) -> None:
    layout, result, launcher_hash = _live_https_update(
        tmp_path,
        _required_path("WORDSHIFT_CANDIDATE_BINARY"),
        tamper=True,
    )

    assert result.returncode == 1
    assert "signed size" in result.stderr
    state = read_state(layout)
    assert state.current_version == OLD_VERSION
    assert state.previous_version is None
    assert state.pending_version is None
    assert not any(layout.staging_dir.iterdir())
    assert hashlib.sha256(layout.launcher_path.read_bytes()).hexdigest() == launcher_hash
