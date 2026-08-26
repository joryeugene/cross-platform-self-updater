import json
import os
import subprocess
from pathlib import Path

import pytest

from self_updater.version import APP_NAME, APP_VERSION


def _packaged_binary() -> Path:
    configured = os.environ.get("WORDSHIFT_PACKAGED_BINARY")
    if configured is None:
        pytest.skip("WORDSHIFT_PACKAGED_BINARY is not set")
    return Path(configured)


@pytest.mark.packaging
def test_packaged_binary_health_version_and_payload() -> None:
    binary = _packaged_binary()

    health = subprocess.run(
        [str(binary), "--internal-health-check", "--expect-version", APP_VERSION],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    version = subprocess.run(
        [str(binary), "version"], check=True, capture_output=True, text=True, timeout=10
    )
    transform = subprocess.run(
        [str(binary), "transform", "Hello, world!"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    json_transform = subprocess.run(
        [str(binary), "transform", "--json", "Hello, world!"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert json.loads(health.stdout) == {"status": "ok", "version": APP_VERSION}
    assert version.stdout == f"{APP_NAME} {APP_VERSION}\n"
    assert transform.stdout == "Ellohay, orldway!\n"
    assert json.loads(json_transform.stdout) == {
        "input": "Hello, world!",
        "output": "Ellohay, orldway!",
    }
