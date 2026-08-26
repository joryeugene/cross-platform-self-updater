import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from self_updater.models import Target, UnsupportedPlatformError, Version
from self_updater.paths import InstallLayout
from task import (
    TaskError,
    baseline_artifact,
    demo_release_urls,
    require_output,
    require_rollback_payload,
    run_command,
)

ROOT = Path(__file__).parents[1]


def test_task_help_lists_all_commands() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "task.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "{test,build,demo,verify}" in result.stdout


@pytest.mark.parametrize(
    ("target", "filename"),
    [
        (Target("linux", "x86_64"), "wordshift-linux-x86_64"),
        (Target("macos", "arm64"), "wordshift-macos-arm64"),
        (Target("windows", "x86_64"), "wordshift-windows-x86_64.exe"),
    ],
)
def test_demo_selects_a_pinned_baseline_artifact(target: Target, filename: str) -> None:
    artifact = baseline_artifact(target)

    assert artifact.url.endswith(f"/v1.0.0/{filename}")
    assert len(artifact.sha256) == 64
    assert artifact.size > 0


def test_demo_rejects_a_target_without_a_published_baseline() -> None:
    with pytest.raises(UnsupportedPlatformError, match=r"no v1\.0\.0 demo artifact"):
        baseline_artifact(Target("macos", "x86_64"))


def test_demo_uses_an_immutable_v1_1_manifest() -> None:
    urls = demo_release_urls(Target("linux", "x86_64"))

    assert urls.manifest.endswith("/v1.1.0/wordshift-linux-x86_64.demo.json")
    assert "/latest/" not in urls.manifest
    assert urls.signature == urls.manifest + ".sig"


def test_task_command_shows_captured_output(capsys: pytest.CaptureFixture[str]) -> None:
    result = run_command(
        [sys.executable, "-c", "print('task output')"],
        capture=True,
    )

    assert result.stdout == "task output\n"
    assert "task output" in capsys.readouterr().out


def test_task_command_rejects_an_unexpected_exit() -> None:
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run_command([sys.executable, "-c", "raise SystemExit(4)"], capture=True)

    assert failure.value.returncode == 4


def test_task_command_accepts_an_expected_nonzero_exit() -> None:
    result = run_command(
        [sys.executable, "-c", "raise SystemExit(2)"],
        capture=True,
        expected_codes={2},
    )

    assert result.returncode == 2


def test_task_command_keeps_captured_error_output_in_sequence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_command(
        [sys.executable, "-c", "import sys; print('expected error', file=sys.stderr)"],
        capture=True,
    )

    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == "expected error"
    assert captured.err == ""


def test_demo_output_validation_reports_a_clear_failure() -> None:
    result = subprocess.CompletedProcess(["wordshift", "version"], 0, "wrong\n", "")

    with pytest.raises(TaskError, match="expected stdout"):
        require_output(result, stdout="WordShift 1.0.0\n")


def test_demo_output_validation_accepts_expected_error_text() -> None:
    result = subprocess.CompletedProcess(
        ["wordshift", "transform"],
        2,
        "",
        "wordshift: error: unrecognized arguments: --json\n",
    )

    require_output(result, stderr_contains="unrecognized arguments: --json")


def test_demo_accepts_an_intact_rollback_payload(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path, Target("linux", "x86_64"))
    rollback = layout.payload_path(Version(1, 0, 0))
    rollback.parent.mkdir(parents=True)
    rollback.write_bytes(b"version 1.0.0")

    require_rollback_payload(
        layout,
        Version(1, 0, 0),
        hashlib.sha256(rollback.read_bytes()).hexdigest(),
    )


def test_demo_rejects_a_missing_rollback_payload(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path, Target("linux", "x86_64"))

    with pytest.raises(TaskError, match="rollback payload"):
        require_rollback_payload(layout, Version(1, 0, 0), "0" * 64)


def test_demo_rejects_a_changed_rollback_payload(tmp_path: Path) -> None:
    layout = InstallLayout.for_root(tmp_path, Target("linux", "x86_64"))
    rollback = layout.payload_path(Version(1, 0, 0))
    rollback.parent.mkdir(parents=True)
    rollback.write_bytes(b"changed")

    with pytest.raises(TaskError, match=r"rollback payload 1\.0\.0 changed"):
        require_rollback_payload(layout, Version(1, 0, 0), "0" * 64)
