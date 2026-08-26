import pytest

from self_updater.models import Target, UnsupportedPlatformError


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", Target("windows", "x86_64")),
        ("Darwin", "x86_64", Target("macos", "x86_64")),
        ("Darwin", "arm64", Target("macos", "arm64")),
        ("Linux", "x86_64", Target("linux", "x86_64")),
        ("Linux", "aarch64", Target("linux", "arm64")),
    ],
)
def test_platform_normalization(system: str, machine: str, expected: Target) -> None:
    assert Target.current(system, machine) == expected


@pytest.mark.parametrize(("system", "machine"), [("FreeBSD", "x86_64"), ("Linux", "i686")])
def test_unsupported_platform_is_explicit(system: str, machine: str) -> None:
    with pytest.raises(UnsupportedPlatformError, match="unsupported platform"):
        Target.current(system, machine)
