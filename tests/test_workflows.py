from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_release_workflow_only_creates_a_draft() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "gh release create" in release
    assert "--draft" in release
    assert "release-signing" in release
    assert "WORDSHIFT_RELEASE_PRIVATE_KEY_B64" in release
    assert "PRIVATE_KEY_B64:" not in release.replace(
        "PRIVATE_KEY_B64: ${{ secrets.WORDSHIFT_RELEASE_PRIVATE_KEY_B64 }}", ""
    )


def test_release_workflow_locks_every_uv_run() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    for line in release.splitlines():
        if "uv run " in line:
            assert "uv run --locked " in line


def test_release_workflow_tests_before_building() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert release.index("uv run --locked task.py test") < release.index(
        "uv run --locked task.py build"
    )


def test_release_workflow_runs_packaged_lifecycle_before_upload() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert release.index("pytest -m packaging") < release.index(
        "name: Stage named artifact"
    )
