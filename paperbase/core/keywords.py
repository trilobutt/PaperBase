"""
YAKE-based keyword extraction with a token-overlap filter.

`yake` is optional at import time: the import happens inside `extract_keywords`, so a
machine without it still runs everything else. The extractor itself is stateless, so a
single module-level instance is built lazily and reused across every call rather than
being reconstructed per paper.
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_TOP = 6
CANDIDATES = 25
MIN_TEXT_CHARS = 40

STOP_TOKENS: frozenset[str] = frozenset({
    "form", "forms", "formed", "including", "include", "includes", "using", "used", "use",
    "based", "show", "shows", "showed", "shown", "present", "presents", "presented",
    "report", "reports", "reported", "suggest", "suggests", "suggested", "found", "find",
    "observed", "examined", "described", "describe", "provide", "provides", "results",
    "result", "study", "studies", "paper", "here", "these", "this", "those", "their",
    "our", "may", "can", "also", "well", "however", "whereas", "thus", "therefore",
    "between", "within", "across", "during", "over", "under", "both", "two", "three",
})

_STRIP_CHARS = ".,;:()"

_extractor = None
# Set once the missing-yake error has been logged: a retroactive run calls extract_keywords
# once per paper, and 150,000 copies of the same line bury everything else in the log.
_import_error_logged = False


def _get_extractor():
    """Build and cache the module-level `yake.KeywordExtractor`.

    Lazy so importing this module never requires `yake` to be installed; the import
    itself lives here and in `extract_keywords`, matching the optional-import pattern in
    `core/categoriser.py`.
    """
    global _extractor
    if _extractor is None:
        import yake

        _extractor = yake.KeywordExtractor(lan="en", n=2, top=CANDIDATES, dedupLim=0.9)
    return _extractor


def _edge_token(phrase: str, *, first: bool) -> str:
    tokens = phrase.split()
    token = tokens[0] if first else tokens[-1]
    return token.lower().strip(_STRIP_CHARS)


def extract_keywords(text: str, top: int = DEFAULT_TOP) -> list[str]:
    """Extract up to `top` keyword phrases from `text` using YAKE.

    Pulls `CANDIDATES` raw candidates best-first (YAKE scores ascending, lower is
    better), drops any candidate whose first or last token is a stop word, drops any
    candidate sharing a token with a phrase already kept, and stops once `top` phrases
    are collected.

    Args:
        text: The source text (typically a paper's abstract or title).
        top: Maximum number of keyword phrases to return.

    Returns:
        Up to `top` keyword phrases, best-first. Empty when `text` is too short or
        `yake` is not installed.
    """
    if len(text.strip()) < MIN_TEXT_CHARS:
        return []

    global _import_error_logged
    try:
        extractor = _get_extractor()
    except ImportError:
        if not _import_error_logged:
            logger.error("yake is required for keyword extraction. Run: pip install yake")
            _import_error_logged = True
        return []

    candidates = extractor.extract_keywords(text)
    candidates.sort(key=lambda pair: pair[1])

    kept: list[str] = []
    kept_tokens: set[str] = set()
    for phrase, _score in candidates:
        if len(kept) >= top:
            break
        if _edge_token(phrase, first=True) in STOP_TOKENS:
            continue
        if _edge_token(phrase, first=False) in STOP_TOKENS:
            continue
        phrase_tokens = {token.lower() for token in phrase.split()}
        if phrase_tokens & kept_tokens:
            continue
        kept.append(phrase)
        kept_tokens |= phrase_tokens

    return kept
