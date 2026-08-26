from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from self_updater import cli
from self_updater.health import HealthError, mark_ready_from_environment
from self_updater.launcher import LauncherError, run_launcher
from self_updater.models import Target
from self_updater.paths import InstallLayout


def current_executable() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(sys.argv[0]).resolve()


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    executable: Path | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    try:
        if "--internal-health-check" in arguments:
            return cli.main(arguments, values)
        if values.get("WORDSHIFT_PAYLOAD") == "1":
            mark_ready_from_environment(values)
            return cli.main(arguments, values)
        running = (executable or current_executable()).resolve()
        layout = InstallLayout.for_root(running.parent, Target.current())
        if layout.state_path.is_file() and running == layout.launcher_path:
            return run_launcher(layout, arguments)
        return cli.main(arguments, values)
    except (HealthError, LauncherError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
