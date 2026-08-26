import pytest

from self_updater.payload import to_pig_latin


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("hello", "ellohay"),
        ("apple", "appleyay"),
        ("Hello, world!", "Ellohay, orldway!"),
        ("SMILE", "ILESMAY"),
        ("  hello\tapple\n", "  ellohay\tappleyay\n"),
        ("123 --", "123 --"),
    ],
)
def test_pig_latin_preserves_spacing_case_and_outer_punctuation(
    source: str, expected: str
) -> None:
    assert to_pig_latin(source) == expected
