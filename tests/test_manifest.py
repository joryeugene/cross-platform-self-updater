import json

import pytest

from self_updater.models import InstallState, ManifestError, ReleaseManifest, Version


def manifest_bytes(**changes: object) -> bytes:
    data: dict[str, object] = {
        "schema_version": 1,
        "version": "1.1.0",
        "target": {"os": "linux", "arch": "x86_64"},
        "published_at": "2026-08-26T18:00:00Z",
        "expires_at": "2026-09-02T18:00:00Z",
        "artifact": {
            "url": "https://github.com/example/updates/releases/download/v1.1.0/wordshift",
            "sha256": "ab" * 32,
            "size": 12,
        },
    }
    data.update(changes)
    return json.dumps(data).encode()


def test_version_is_strict_and_ordered() -> None:
    assert Version.parse("1.10.0") > Version.parse("1.2.9")
    assert str(Version.parse("0.0.1")) == "0.0.1"

    for invalid in ("v1.2.3", "1.2", "01.2.3", "1.2.3-alpha", "1.-2.3"):
        with pytest.raises(ManifestError, match=r"MAJOR\.MINOR\.PATCH"):
            Version.parse(invalid)


def test_manifest_parses_strict_schema() -> None:
    manifest = ReleaseManifest.from_bytes(manifest_bytes())

    assert manifest.version == Version(1, 1, 0)
    assert manifest.target.os == "linux"
    assert manifest.artifact.size == 12
    assert manifest.published_at.tzinfo is not None


def test_manifest_rejects_unknown_keys() -> None:
    with pytest.raises(ManifestError, match="unknown keys: surprise"):
        ReleaseManifest.from_bytes(manifest_bytes(surprise=True))


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema_version": True}, "schema_version must be an integer"),
        ({"schema_version": 2}, "unsupported manifest schema"),
        ({"published_at": "not-a-date"}, "published_at"),
        ({"target": {"os": "linux"}}, "target missing keys: arch"),
        (
            {
                "artifact": {
                    "url": "https://example.test/app",
                    "sha256": "AB" * 32,
                    "size": 12,
                }
            },
            "sha256",
        ),
    ],
)
def test_manifest_rejects_invalid_fields(change: dict[str, object], message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        ReleaseManifest.from_bytes(manifest_bytes(**change))


def test_install_state_round_trips() -> None:
    state = InstallState(
        schema_version=1,
        current_version=Version(1, 1, 0),
        previous_version=Version(1, 0, 0),
        pending_version=None,
    )

    assert InstallState.from_dict(state.to_dict()) == state


def test_install_state_rejects_unknown_keys() -> None:
    with pytest.raises(ManifestError, match="unknown keys: extra"):
        InstallState.from_dict(
            {
                "schema_version": 1,
                "current_version": "1.0.0",
                "previous_version": None,
                "pending_version": None,
                "extra": "bad",
            }
        )
