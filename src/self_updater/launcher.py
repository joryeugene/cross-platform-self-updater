from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from self_updater.installer import update_lock
from self_updater.models import InstallState, Version
from self_updater.paths import InstallationError, InstallLayout, read_state, write_state

RESTART_FOR_UPDATE = 75
MAX_RESTARTS = 3


class LauncherError(RuntimeError):
    pass


class ChildProcess(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...


StartChild = Callable[[Path, Sequence[str], Mapping[str, str]], ChildProcess]


@contextmanager
def _state_lock(layout: InstallLayout, timeout: float) -> Iterator[None]:
    try:
        with update_lock(layout, timeout):
            yield
    except InstallationError as error:
        raise LauncherError(str(error)) from error


def _start_child(
    executable: Path,
    args: Sequence[str],
    env: Mapping[str, str],
) -> ChildProcess:
    return subprocess.Popen([str(executable), *args], env=dict(env))


def _readiness_matches(path: Path, token: str) -> bool:
    try:
        if path.stat().st_size > 256:
            return False
        return path.read_text(encoding="utf-8") == token + "\n"
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False


def _stop(process: ChildProcess) -> None:
    if process.poll() is None:
        process.terminate()
    with suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=2)


def _payload_exists(layout: InstallLayout, version: Version) -> bool:
    executable = layout.payload_path(version)
    if executable.parent.is_symlink() or executable.is_symlink():
        raise LauncherError(f"payload for version {version} contains a symbolic link")
    return executable.is_file()


def _run_once(
    layout: InstallLayout,
    version: Version,
    args: Sequence[str],
    readiness_timeout: float,
    start_child: StartChild,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[ChildProcess, bool]:
    executable = layout.payload_path(version)
    if not _payload_exists(layout, version):
        raise LauncherError(f"payload for version {version} is missing")
    token = uuid.uuid4().hex
    readiness = layout.readiness_dir / token
    env = os.environ.copy()
    env.update(
        {
            "WORDSHIFT_PAYLOAD": "1",
            "WORDSHIFT_INSTALL_ROOT": str(layout.root),
            "WORDSHIFT_READY_FILE": str(readiness),
            "WORDSHIFT_READY_TOKEN": token,
        }
    )
    try:
        process = start_child(executable, args, env)
    except OSError as error:
        raise LauncherError(f"could not start version {version}: {error}") from error
    deadline = monotonic() + readiness_timeout
    ready = False
    while monotonic() < deadline:
        if _readiness_matches(readiness, token):
            ready = True
            break
        if process.poll() is not None:
            break
        sleep(0.02)
    readiness.unlink(missing_ok=True)
    return process, ready


def _recover_missing_current(layout: InstallLayout, state: InstallState) -> InstallState:
    if _payload_exists(layout, state.current_version):
        return state
    previous = state.previous_version
    if previous is not None and _payload_exists(layout, previous):
        recovered = InstallState(1, previous, state.current_version, state.pending_version)
        write_state(layout, recovered)
        return recovered
    pending = state.pending_version
    if pending is not None and _payload_exists(layout, pending):
        return state
    raise LauncherError(f"payload for version {state.current_version} is missing")


def run_launcher(
    layout: InstallLayout,
    args: Sequence[str],
    readiness_timeout: float = 10.0,
    lock_timeout: float = 2.0,
    *,
    start_child: StartChild = _start_child,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    _restart_count: int = 0,
) -> int:
    process: ChildProcess | None = None
    launched_version: Version
    failed_pending: Version | None = None
    with _state_lock(layout, lock_timeout):
        state = _recover_missing_current(layout, read_state(layout))
        if state.pending_version is not None:
            pending = state.pending_version
            if _payload_exists(layout, pending):
                process, ready = _run_once(
                    layout,
                    pending,
                    args,
                    readiness_timeout,
                    start_child,
                    monotonic,
                    sleep,
                )
                if ready:
                    try:
                        write_state(
                            layout,
                            InstallState(1, pending, state.current_version, None),
                        )
                    except InstallationError:
                        _stop(process)
                        raise
                    launched_version = pending
                else:
                    _stop(process)
                    process = None
                    failed_pending = pending
                    write_state(layout, replace(state, pending_version=None))
                    launched_version = state.current_version
            else:
                state = replace(state, pending_version=None)
                write_state(layout, state)
                launched_version = state.current_version
        else:
            launched_version = state.current_version

    if failed_pending is not None and args and args[0] == "update":
        raise LauncherError(
            f"payload {failed_pending} failed readiness; kept {launched_version}"
        )

    if process is None:
        process, ready = _run_once(
            layout,
            launched_version,
            args,
            readiness_timeout,
            start_child,
            monotonic,
            sleep,
        )
    else:
        ready = True
    if not ready:
        _stop(process)
        if failed_pending is None:
            raise LauncherError(f"payload {launched_version} failed readiness")
        raise LauncherError("rollback payload failed readiness")

    exit_code = process.wait()
    if exit_code != RESTART_FOR_UPDATE:
        return exit_code
    with _state_lock(layout, lock_timeout):
        latest = read_state(layout)
        if latest.pending_version is None and latest.current_version <= launched_version:
            raise LauncherError(
                "payload requested restart without a pending or newer update"
            )
    if _restart_count >= MAX_RESTARTS:
        raise LauncherError("too many consecutive update restarts")
    return run_launcher(
        layout,
        args,
        readiness_timeout,
        lock_timeout,
        start_child=start_child,
        monotonic=monotonic,
        sleep=sleep,
        _restart_count=_restart_count + 1,
    )
