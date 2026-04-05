import logging
import re
import shutil
from pathlib import Path

from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

_ILLEGAL_CHARS_RE = re.compile(r'[/:*?"<>|\\]')


def _fs_safe(s: str, max_len: int = 80) -> str:
    """Strip filesystem-illegal characters and truncate."""
    s = _ILLEGAL_CHARS_RE.sub("_", s)
    return s[:max_len].strip()


def _authors_short(authors: list[str]) -> str:
    if not authors:
        return "Unknown"
    # First author family name (part before first comma)
    first = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0].split()[0]
    if len(authors) > 2:
        return f"{first} et al"
    return first


def _journal_safe(journal: str) -> str:
    if not journal.strip():
        return "Unsorted"
    return _fs_safe(journal)


DEFAULT_PATTERN = "{journal}/{year}/{author} ({year}) {title}.pdf"


def compute_destination(paper: Paper, library_root: Path, pattern: str = DEFAULT_PATTERN) -> Path:
    """Compute the organised destination path for a paper using the given naming pattern.

    Tokens: {journal} {year} {author} {title}
    Each token value is already filesystem-safe before substitution.
    """
    journal = _journal_safe(paper.journal)
    year = str(paper.year) if paper.year else "Unknown"
    author = _fs_safe(_authors_short(paper.authors))
    title = _fs_safe(paper.title, max_len=80)
    relative = pattern.format(journal=journal, year=year, author=author, title=title)
    return library_root / relative


def place_file(
    source: Path,
    paper: Paper,
    library_root: Path,
    move: bool = False,
    folder_pattern: str = DEFAULT_PATTERN,
) -> Path:
    """
    Copy or move source PDF to the organised hierarchy.
    Returns the final destination Path.
    Updates paper.file_path in-place.

    If metadata_source is "xmp" or "filename" (i.e. all real lookups failed),
    the file is placed in Unsorted/ with its original name instead of applying
    the naming pattern.
    """
    if paper.metadata_source in ("xmp", "filename"):
        dest = library_root / "Unsorted" / source.name
    else:
        dest = compute_destination(paper, library_root, folder_pattern)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Handle collisions
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 2
        while dest.exists():
            dest = dest.parent / f"{stem}_{counter}{suffix}"
            counter += 1
        logger.warning("Destination collision; renamed to %s", dest.name)

    if move:
        shutil.move(str(source), str(dest))
    else:
        shutil.copy2(str(source), str(dest))

    paper.file_path = str(dest)
    return dest
