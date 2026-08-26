from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from self_updater.models import Version


class HealthError(RuntimeError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def run_candidate_health(
    candidate: Path,
    expected: Version,
    timeout: float,
    *,
    run: RunCommand = subprocess.run,
) -> None:
    command = [
        str(candidate),
        "--internal-health-check",
        "--expect-version",
        str(expected),
    ]
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HealthError(f"candidate health check could not run: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[:200]
        raise HealthError(f"candidate health check failed with exit {result.returncode}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HealthError("candidate health check returned invalid JSON") from error
    expected_payload = {"status": "ok", "version": str(expected)}
    if payload != expected_payload:
        raise HealthError("candidate health check returned an unexpected result")


def mark_ready_from_environment(env: Mapping[str, str] | None = None) -> None:
    values = os.environ if env is None else env
    raw_path = values.get("WORDSHIFT_READY_FILE")
    token = values.get("WORDSHIFT_READY_TOKEN")
    if raw_path is None and token is None:
        return
    if not raw_path or not token:
        raise HealthError("WORDSHIFT_READY_FILE and WORDSHIFT_READY_TOKEN must both be set")
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(token.encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise HealthError(f"could not write readiness file: {error}") from error
