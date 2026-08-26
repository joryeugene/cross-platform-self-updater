from dataclasses import dataclass

from self_updater.models import Target, UnsupportedPlatformError

RELEASE_REPOSITORY = "joryeugene/wordshift-update-feed"
RELEASE_PUBLIC_KEY_B64 = "Fm8GrJnK2MGhhgFG6j8pns/YSM5bbovqlWr5grhaS6E="
PUBLISHED_TARGETS = frozenset(
    {
        Target("linux", "x86_64"),
        Target("macos", "arm64"),
        Target("windows", "x86_64"),
    }
)


@dataclass(frozen=True)
class ReleaseUrls:
    manifest: str
    signature: str


def release_asset_name(target: Target) -> str:
    suffix = ".exe" if target.os == "windows" else ""
    return f"wordshift-{target.os}-{target.arch}{suffix}"


def release_urls(target: Target) -> ReleaseUrls:
    if target not in PUBLISHED_TARGETS:
        raise UnsupportedPlatformError(
            f"default release feed is not published for {target.os}/{target.arch}"
        )
    asset = release_asset_name(target)
    manifest = (
        f"https://github.com/{RELEASE_REPOSITORY}/releases/latest/download/{asset}.json"
    )
    return ReleaseUrls(manifest, manifest + ".sig")
