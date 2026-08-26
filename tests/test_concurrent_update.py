import multiprocessing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock

from self_updater.installer import InstallationError, update_lock
from self_updater.models import Target
from self_updater.paths import InstallLayout


def hold_lock(path: str, ready: Any, release: Any) -> None:
    with FileLock(path, timeout=2):
        ready.set()
        release.wait(timeout=5)


@pytest.fixture
def lock_holder(tmp_path: Path) -> Iterator[InstallLayout]:
    layout = InstallLayout.for_root(tmp_path / "app", Target("linux", "x86_64"))
    layout.ensure_directories()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=hold_lock, args=(str(layout.lock_path), ready, release))
    process.start()
    assert ready.wait(timeout=5)
    try:
        yield layout
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_second_updater_times_out_without_mutating_state(lock_holder: InstallLayout) -> None:
    with (
        pytest.raises(InstallationError, match="another update is already running"),
        update_lock(lock_holder, timeout=0.1),
    ):
        pytest.fail("lock must not be acquired")
    assert not lock_holder.state_path.exists()
