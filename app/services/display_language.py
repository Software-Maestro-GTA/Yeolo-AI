"""Recognize Korean display labels and visit prose without translating routes."""

import unicodedata


def is_korean_text(value: str) -> bool:
    """Return whether nonempty alphabetic content uses only Hangul.

    Args:
        value: Display label or prose; digits and punctuation do not set language.
    Returns:
        True when at least one letter is present and every letter is Hangul.
    """
    text = unicodedata.normalize('NFKC', value)
    letters = [unicodedata.name(char, '') for char in text if unicodedata.category(char).startswith('L')]
    return bool(letters) and all(name.startswith('HANGUL') for name in letters)
