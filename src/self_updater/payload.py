import re

VOWELS = frozenset("aeiou")


def _transform_word(word: str) -> str:
    lowered = word.lower()
    if lowered[0] in VOWELS:
        transformed = lowered + "yay"
    else:
        split = 0
        while split < len(lowered) and lowered[split] not in VOWELS:
            split += 1
        transformed = lowered[split:] + lowered[:split] + "ay"
    if word.isupper():
        return transformed.upper()
    if word[0].isupper():
        return transformed.capitalize()
    return transformed


def _transform_token(token: str) -> str:
    match = re.fullmatch(r"([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)", token)
    if match is None:
        return token
    leading, word, trailing = match.groups()
    return leading + _transform_word(word) + trailing


def to_pig_latin(text: str) -> str:
    return "".join(
        part if part.isspace() else _transform_token(part) for part in re.split(r"(\s+)", text)
    )
