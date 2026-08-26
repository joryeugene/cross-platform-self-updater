from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from self_updater import __main__ as app
from self_updater.models import InstallState, Target, Version
from self_updater.paths import InstallLayout, write_state


def test_standalone_executable_routes_to_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Sequence[str] | None] = []

    def cli_main(
        argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
    ) -> int:
        seen.append(argv)
        return 4

    monkeypatch.setattr(app.cli, "main", cli_main)

    result = app.main(["version"], {}, tmp_path / "standalone")

    assert result == 4
    assert seen == [["version"]]


def test_payload_mode_marks_ready_before_routing_to_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness = tmp_path / "ready"
    environment = {
        "WORDSHIFT_PAYLOAD": "1",
        "WORDSHIFT_READY_FILE": str(readiness),
        "WORDSHIFT_READY_TOKEN": "nonce",
    }
    monkeypatch.setattr(app.cli, "main", lambda argv, env: 0)

    assert app.main(["version"], environment, tmp_path / "payload") == 0
    assert readiness.read_text(encoding="utf-8") == "nonce\n"


def test_installed_launcher_routes_to_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = InstallLayout.for_root(tmp_path / "install", Target.current())
    layout.ensure_directories()
    layout.launcher_path.write_bytes(b"launcher")
    write_state(layout, InstallState.initial(Version(1, 0, 0)))
    seen: list[tuple[InstallLayout, Sequence[str]]] = []

    def launcher(selected: InstallLayout, arguments: Sequence[str]) -> int:
        seen.append((selected, arguments))
        return 6

    monkeypatch.setattr(app, "run_launcher", launcher)

    assert app.main(["version"], {}, layout.launcher_path) == 6
    assert seen == [(layout, ["version"])]
