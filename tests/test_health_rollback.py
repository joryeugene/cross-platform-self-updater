import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from self_updater.health import HealthError, mark_ready_from_environment, run_candidate_health
from self_updater.launcher import LauncherError, run_launcher
from self_updater.models import InstallState, Target, Version
from self_updater.paths import InstallLayout, read_state, write_state


class FakeChild:
    def __init__(self, return_code: int | None = 0) -> None:
        self.return_code = return_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.return_code is None else self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -1


class CallbackChild(FakeChild):
    def __init__(self, callback: Callable[[], None], return_code: int) -> None:
        super().__init__(return_code)
        self.callback = callback

    def wait(self, timeout: float | None = None) -> int:
        self.callback()
        return super().wait(timeout)


class ChildFactory:
    def __init__(self, ready_versions: set[Version]) -> None:
        self.ready_versions = ready_versions
        self.versions: list[Version] = []

    def __call__(
        self, executable: Path, args: Sequence[str], env: Mapping[str, str]
    ) -> FakeChild:
        version = Version.parse(executable.parent.name)
        self.versions.append(version)
        assert list(args) == ["version"]
        if version in self.ready_versions:
            Path(env["WORDSHIFT_READY_FILE"]).write_text(
                env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
            )
            return FakeChild(0)
        return FakeChild(1)


def layout_with_versions(
    tmp_path: Path,
    *,
    current: Version,
    previous: Version | None = None,
    pending: Version | None = None,
    existing: set[Version] | None = None,
) -> InstallLayout:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))
    layout.ensure_directories()
    versions = existing or {
        version for version in (current, previous, pending) if version is not None
    }
    for version in versions:
        payload = layout.payload_path(version)
        payload.parent.mkdir(parents=True)
        payload.write_bytes(str(version).encode())
        payload.chmod(0o755)
    write_state(layout, InstallState(1, current, previous, pending))
    return layout


def test_candidate_health_requires_exact_machine_readable_result(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"binary")
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "ok", "version": "1.1.0"}) + "\n",
            stderr="",
        )

    run_candidate_health(candidate, Version(1, 1, 0), 2, run=run)

    assert calls == [[str(candidate), "--internal-health-check", "--expect-version", "1.1.0"]]


def test_candidate_health_rejects_wrong_version(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"binary")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok","version":"1.0.0"}\n',
            stderr="",
        )

    with pytest.raises(HealthError, match="unexpected result"):
        run_candidate_health(candidate, Version(1, 1, 0), 2, run=run)


def test_payload_marks_nonce_specific_readiness(tmp_path: Path) -> None:
    readiness = tmp_path / "ready" / "token"

    mark_ready_from_environment(
        {
            "WORDSHIFT_READY_FILE": str(readiness),
            "WORDSHIFT_READY_TOKEN": "abc123",
        }
    )

    assert readiness.read_text(encoding="utf-8") == "abc123\n"


def test_incomplete_readiness_environment_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(HealthError, match="both be set"):
        mark_ready_from_environment({"WORDSHIFT_READY_FILE": str(tmp_path / "ready")})


def test_pending_version_is_promoted_after_readiness(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)
    start = ChildFactory({new})

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0

    state = read_state(layout)
    assert state.current_version == new
    assert state.previous_version == old
    assert state.pending_version is None
    assert start.versions == [new]


def test_pending_state_stays_durable_until_candidate_is_ready(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        state = read_state(layout)
        assert state.current_version == old
        assert state.previous_version is None
        assert state.pending_version == new
        Path(env["WORDSHIFT_READY_FILE"]).write_text(
            env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
        )
        return FakeChild(0)

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0
    assert read_state(layout) == InstallState(1, new, old, None)


def test_pending_readiness_holds_update_lock(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        with pytest.raises(Timeout), FileLock(layout.lock_path, timeout=0):
            pytest.fail("pending readiness must retain the update lock")
        Path(env["WORDSHIFT_READY_FILE"]).write_text(
            env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
        )
        return FakeChild(0)

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0


def test_interruption_before_readiness_keeps_pending_candidate(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)

    def fail_start(
        executable: Path, args: Sequence[str], env: Mapping[str, str]
    ) -> FakeChild:
        raise OSError("interrupted")

    with pytest.raises(LauncherError, match="could not start"):
        run_launcher(layout, ["version"], start_child=fail_start, readiness_timeout=0.1)

    assert read_state(layout) == InstallState(1, old, None, new)


def test_failed_readiness_rolls_back_once(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    previous = Version(0, 9, 0)
    layout = layout_with_versions(tmp_path, current=old, previous=previous, pending=new)
    start = ChildFactory({old})

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.01) == 0

    state = read_state(layout)
    assert state.current_version == old
    assert state.previous_version == previous
    assert state.pending_version is None
    assert start.versions == [new, old]


def test_failed_update_readiness_does_not_repeat_the_update(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)
    calls: list[tuple[Version, list[str]]] = []

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        version = Version.parse(executable.parent.name)
        calls.append((version, list(args)))
        if version == old:
            Path(env["WORDSHIFT_READY_FILE"]).write_text(
                env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
            )
        return FakeChild(0)

    with pytest.raises(LauncherError, match=r"kept 1\.0\.0"):
        run_launcher(layout, ["update"], start_child=start, readiness_timeout=0.01)

    state = read_state(layout)
    assert state.current_version == old
    assert state.previous_version is None
    assert state.pending_version is None
    assert calls == [(new, ["update"])]


def test_missing_pending_payload_is_cleared_without_promotion(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    missing = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=missing, existing={old})
    start = ChildFactory({old})

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0

    state = read_state(layout)
    assert state.current_version == old
    assert state.pending_version is None
    assert start.versions == [old]


def test_missing_current_recovers_previous_payload(tmp_path: Path) -> None:
    missing = Version(1, 1, 0)
    previous = Version(1, 0, 0)
    layout = layout_with_versions(
        tmp_path,
        current=missing,
        previous=previous,
        existing={previous},
    )
    start = ChildFactory({previous})

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0
    assert read_state(layout).current_version == previous


def test_pending_payload_can_recover_when_current_and_previous_are_missing(
    tmp_path: Path,
) -> None:
    missing = Version(1, 0, 0)
    pending = Version(1, 1, 0)
    layout = layout_with_versions(
        tmp_path,
        current=missing,
        pending=pending,
        existing={pending},
    )
    start = ChildFactory({pending})

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0
    assert read_state(layout) == InstallState(1, pending, missing, None)


def test_failed_new_and_rollback_payloads_stop(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, pending=new)
    start = ChildFactory(set())

    with pytest.raises(LauncherError, match="rollback payload failed readiness"):
        run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.01)

    assert start.versions == [new, old]


def test_restart_exit_promotes_pending_version(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, existing={old, new})
    calls: list[Version] = []

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        version = Version.parse(executable.parent.name)
        calls.append(version)
        Path(env["WORDSHIFT_READY_FILE"]).write_text(
            env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
        )
        if version == old:
            write_state(layout, InstallState(1, old, None, new))
            return FakeChild(75)
        return FakeChild(0)

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0
    assert calls == [old, new]
    assert read_state(layout).current_version == new


def test_restart_accepts_update_confirmed_by_another_launcher(tmp_path: Path) -> None:
    old = Version(1, 0, 0)
    new = Version(1, 1, 0)
    layout = layout_with_versions(tmp_path, current=old, existing={old, new})
    calls: list[Version] = []

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        version = Version.parse(executable.parent.name)
        calls.append(version)
        Path(env["WORDSHIFT_READY_FILE"]).write_text(
            env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
        )
        if version == old:
            return CallbackChild(
                lambda: write_state(layout, InstallState(1, new, old, None)), 75
            )
        return FakeChild(0)

    assert run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1) == 0
    assert calls == [old, new]
    assert read_state(layout) == InstallState(1, new, old, None)


def test_restart_rejects_state_without_pending_or_newer_current(tmp_path: Path) -> None:
    current = Version(1, 0, 0)
    layout = layout_with_versions(tmp_path, current=current)

    def start(executable: Path, args: Sequence[str], env: Mapping[str, str]) -> FakeChild:
        Path(env["WORDSHIFT_READY_FILE"]).write_text(
            env["WORDSHIFT_READY_TOKEN"] + "\n", encoding="utf-8"
        )
        return FakeChild(75)

    with pytest.raises(LauncherError, match="without a pending or newer update"):
        run_launcher(layout, ["version"], start_child=start, readiness_timeout=0.1)


def test_launcher_refuses_to_race_locked_state(tmp_path: Path) -> None:
    current = Version(1, 0, 0)
    layout = layout_with_versions(tmp_path, current=current)

    with (
        FileLock(layout.lock_path),
        pytest.raises(LauncherError, match="another update is already running"),
    ):
        run_launcher(layout, ["version"], lock_timeout=0.01)
