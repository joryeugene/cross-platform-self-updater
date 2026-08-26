import base64

import pytest

from self_updater.models import Target, UnsupportedPlatformError
from self_updater.release_config import RELEASE_PUBLIC_KEY_B64, release_urls


def test_pinned_public_key_is_raw_ed25519_key() -> None:
    assert len(base64.b64decode(RELEASE_PUBLIC_KEY_B64, validate=True)) == 32


def test_release_urls_use_stable_latest_assets() -> None:
    urls = release_urls(Target("linux", "x86_64"))

    assert urls.manifest == (
        "https://github.com/joryeugene/wordshift-update-feed/"
        "releases/latest/download/wordshift-linux-x86_64.json"
    )
    assert urls.signature == urls.manifest + ".sig"


def test_windows_manifest_name_matches_executable_release_asset() -> None:
    urls = release_urls(Target("windows", "x86_64"))

    assert urls.manifest.endswith("/wordshift-windows-x86_64.exe.json")


@pytest.mark.parametrize(
    "target",
    [
        Target("windows", "arm64"),
        Target("macos", "x86_64"),
        Target("linux", "arm64"),
    ],
)
def test_default_feed_rejects_unpublished_target(target: Target) -> None:
    with pytest.raises(UnsupportedPlatformError, match="not published"):
        release_urls(target)
