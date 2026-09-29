"""Tests for paperbase.core.keywords."""

from paperbase.core.keywords import extract_keywords

MYCO_TEXT = (
    "Mycorrhizal fungi form symbiotic associations with the roots of most land plants.\n"
    "We examined ectomycorrhizal colonisation of Pinus sylvestris seedlings by Suillus "
    "bovinus in a\n"
    "podzol soil in northern Sweden, and measured nitrogen transfer over two growing "
    "seasons."
)


def test_returns_at_most_top() -> None:
    result = extract_keywords(MYCO_TEXT)
    assert 0 < len(result) <= 6


def test_no_shared_tokens() -> None:
    result = extract_keywords(MYCO_TEXT)
    seen: set[str] = set()
    for phrase in result:
        tokens = {token.lower() for token in phrase.split()}
        assert not (tokens & seen)
        seen |= tokens


def test_stop_tokens_filtered() -> None:
    result = extract_keywords(MYCO_TEXT)
    assert "form symbiotic" not in result
    assert "Mycorrhizal fungi" in result


def test_short_text_returns_empty() -> None:
    assert extract_keywords("Too short.") == []


def test_deterministic() -> None:
    assert extract_keywords(MYCO_TEXT) == extract_keywords(MYCO_TEXT)


def test_top_parameter_respected() -> None:
    result = extract_keywords(MYCO_TEXT, top=2)
    assert len(result) <= 2
