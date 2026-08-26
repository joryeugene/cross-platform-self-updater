import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from self_updater.models import Artifact, InstallState, Target, UnsupportedPlatformError, Version
from self_updater.paths import InstallationError, InstallLayout, executable_name, read_state
from self_updater.release_config import ReleaseUrls, release_asset_name
from self_updater.release_source import ReleaseSourceError, stream_artifact
from self_updater.version import APP_VERSION


class TaskError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parent
BASELINE_RELEASE = "https://github.com/joryeugene/wordshift-update-feed/releases/download/v1.0.0"
DEMO_RELEASE = "https://github.com/joryeugene/wordshift-update-feed/releases/download/v1.1.0"
BASELINE_ASSETS: dict[Target, tuple[str, str, int]] = {
    Target("linux", "x86_64"): (
        "wordshift-linux-x86_64",
        "cef23378acd011cd232fffb628bb85fc475dda311fca8f500fda2b0792cc4fbd",
        14_833_168,
    ),
    Target("macos", "arm64"): (
        "wordshift-macos-arm64",
        "fb59bba973099f44085a9cea4494082f93afd633f79b3c96b0ca0c82255fbeeb",
        13_431_920,
    ),
    Target("windows", "x86_64"): (
        "wordshift-windows-x86_64.exe",
        "d56cf2865d80c9852b452ad5f746fdbf8f2a67435567c8b1a191cbd24d00b621",
        14_555_258,
    ),
}


def run_command(
    command: Sequence[str],
    *,
    capture: bool = False,
    expected_codes: set[int] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {subprocess.list2cmdline(command)}", flush=True)
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        capture_output=capture,
        text=True,
        check=False,
        timeout=timeout,
    )
    if capture:
        print(result.stdout or "", end="")
        print(result.stderr or "", end="")
    if result.returncode not in (expected_codes or {0}):
        result.check_returncode()
    return result


def require_output(
    result: subprocess.CompletedProcess[str],
    *,
    stdout: str | None = None,
    stderr_contains: str | None = None,
) -> None:
    if stdout is not None and result.stdout != stdout:
        raise TaskError(f"expected stdout {stdout!r}, received {result.stdout!r}")
    if stderr_contains is not None and stderr_contains not in (result.stderr or ""):
        raise TaskError(
            f"expected stderr to contain {stderr_contains!r}, received {result.stderr!r}"
        )


def baseline_artifact(target: Target) -> Artifact:
    try:
        filename, sha256, size = BASELINE_ASSETS[target]
    except KeyError as error:
        raise UnsupportedPlatformError(
            f"no v1.0.0 demo artifact for {target.os}/{target.arch}"
        ) from error
    return Artifact(f"{BASELINE_RELEASE}/{filename}", sha256, size)


def demo_release_urls(target: Target) -> ReleaseUrls:
    baseline_artifact(target)
    manifest = f"{DEMO_RELEASE}/{release_asset_name(target)}.demo.json"
    return ReleaseUrls(manifest, manifest + ".sig")


def require_rollback_payload(
    layout: InstallLayout, version: Version, expected_sha256: str
) -> None:
    payload = layout.payload_path(version)
    if not payload.is_file():
        raise TaskError(f"rollback payload {version} is missing")
    actual_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise TaskError(f"rollback payload {version} changed")


def test_project() -> None:
    run_command([sys.executable, "-m", "ruff", "check", "."])
    run_command([sys.executable, "-m", "mypy"])
    run_command(
        [
            sys.executable,
            "-m",
            "pytest",
            "--cov=self_updater",
            "--cov-report=term-missing",
            "-q",
        ]
    )


def build_project() -> None:
    run_command(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--log-level=ERROR",
            "updater.spec",
        ]
    )
    binary = ROOT / "dist" / executable_name()
    result = run_command([str(binary), "version"], capture=True, timeout=15)
    require_output(result, stdout=f"WordShift {APP_VERSION}\n")


def demo_update() -> None:
    target = Target.current()
    artifact = baseline_artifact(target)
    demo_urls = demo_release_urls(target)
    with tempfile.TemporaryDirectory(prefix="wordshift-demo-") as temporary_directory:
        workspace = Path(temporary_directory)
        bootstrap = workspace / executable_name(target.os)
        print(f"$ download and verify {artifact.url}", flush=True)
        stream_artifact(artifact, bootstrap, timeout=30, max_bytes=artifact.size)
        if target.os != "windows":
            bootstrap.chmod(0o755)

        root = workspace / "install"
        installed = run_command(
            [str(bootstrap), "install", "--root", str(root), "--artifact", str(bootstrap)],
            capture=True,
            timeout=30,
        )
        require_output(installed, stdout=f"installed WordShift 1.0.0 at {root.resolve()}\n")

        layout = InstallLayout.for_root(root, target)
        launcher = layout.launcher_path
        launcher_digest = hashlib.sha256(launcher.read_bytes()).hexdigest()

        before = run_command([str(launcher), "version"], capture=True, timeout=15)
        require_output(before, stdout="WordShift 1.0.0\n")
        unavailable = run_command(
            [str(launcher), "transform", "--json", "Hello, world!"],
            capture=True,
            expected_codes={2},
            timeout=15,
        )
        require_output(unavailable, stderr_contains="unrecognized arguments: --json")
        release_arguments = [
            "--manifest-url",
            demo_urls.manifest,
            "--signature-url",
            demo_urls.signature,
        ]
        available = run_command(
            [str(launcher), "check", *release_arguments], capture=True, timeout=30
        )
        require_output(available, stdout="update available: 1.1.0\n")
        updated = run_command(
            [str(launcher), "update", *release_arguments], capture=True, timeout=60
        )
        require_output(updated, stdout="staged 1.1.0; restarting\nup to date: 1.1.0\n")
        after = run_command([str(launcher), "version"], capture=True, timeout=15)
        require_output(after, stdout="WordShift 1.1.0\n")
        transformed = run_command(
            [str(launcher), "transform", "--json", "Hello, world!"],
            capture=True,
            timeout=15,
        )
        require_output(
            transformed,
            stdout='{"input": "Hello, world!", "output": "Ellohay, orldway!"}\n',
        )

        state = read_state(layout)
        expected = InstallState(1, Version(1, 1, 0), Version(1, 0, 0), None)
        if state != expected:
            raise TaskError(f"unexpected final state: {state.to_dict()}")
        if hashlib.sha256(launcher.read_bytes()).hexdigest() != launcher_digest:
            raise TaskError("the stable launcher changed during the update")
        require_rollback_payload(layout, Version(1, 0, 0), artifact.sha256)
        print(json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")))
        print("verified: updated 1.0.0 -> 1.1.0; launcher unchanged; rollback retained")


def verify_project() -> None:
    test_project()
    build_project()
    demo_update()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Develop and verify WordShift")
    parser.add_argument("command", choices=("test", "build", "demo", "verify"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    command = _parser().parse_args(argv).command
    actions = {
        "test": test_project,
        "build": build_project,
        "demo": demo_update,
        "verify": verify_project,
    }
    try:
        actions[command]()
        return 0
    except (
        OSError,
        InstallationError,
        ReleaseSourceError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        TaskError,
        UnsupportedPlatformError,
    ) as error:
        print(f"task failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
