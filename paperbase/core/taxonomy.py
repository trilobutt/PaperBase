"""
Topic taxonomy: the plain-text label file the user hand-edits.

Topic labels are hand-edited, so they live in a plain text file the user owns rather
than in `settings.json`. The file is untrusted input and is validated at the boundary: a
bad line is logged and skipped rather than raised, because one bad line must not cost
the user every other label.

File format, one label per line:

- `Name`, `Parent > Name`, or deeper, each optionally followed by `: description`. Only
  the first colon splits; colons inside a description are kept.
- A label's parent path must be a label defined on an earlier line. Each label becomes a
  collection nested under its parent's.
- Lines whose first non-space character is `#` are comments. Blank lines are ignored.
- Names and description are stripped of surrounding whitespace.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_NAME = 120

_HEADER = """\
# PaperBase topic taxonomy
#
# One label per line: `Name`, or `Parent > Name` for a nested label, optionally
# followed by `: description`. A parent must appear on an earlier line.
# Only the first colon splits a line; colons inside a description are kept.
# Lines starting with '#' are comments and blank lines are ignored.
# Name and description are stripped of surrounding whitespace.
"""


class TaxonomyError(Exception):
    """Raised when the taxonomy file itself cannot be read, as opposed to a bad line in it.

    `reason` is the predicate on its own ("is not valid UTF-8"), so a caller already
    showing the path, as the Settings dialog does, can name the file without repeating it.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Taxonomy file {path} {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Label:
    """A single topic label, with an optional description used to steer embedding.

    `parents` names the enclosing labels from the top level down, and is empty for a
    top-level label. The label's collection nests under the collection of `parents`.
    """

    name: str
    description: str = ""
    parents: tuple[str, ...] = ()

    @property
    def path(self) -> tuple[str, ...]:
        """`parents` plus `name`: the collection path this label resolves to."""
        return self.parents + (self.name,)

    @property
    def text(self) -> str:
        """The text embedded for this label: `"{name}. {description}"` when described."""
        if self.description:
            return f"{self.name}. {self.description}"
        return self.name


def parse_taxonomy(text: str) -> list[Label]:
    """Parse taxonomy file contents into labels, skipping invalid lines with a warning.

    Args:
        text: the raw file contents.

    Returns:
        Valid, deduplicated labels, in file order.
    """
    labels: list[Label] = []
    seen: set[tuple[str, ...]] = set()
    canonical: dict[tuple[str, ...], tuple[str, ...]] = {}

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        head, _, description = line.partition(":")
        segments = [s.strip() for s in head.split(">")]
        description = description.strip()
        name = segments[-1]

        if not all(segments):
            logger.warning("Taxonomy line %d: empty name; skipping.", lineno)
            continue
        if any(len(s) > MAX_NAME for s in segments):
            logger.warning(
                "Taxonomy line %d: name longer than MAX_NAME (%d); skipping.",
                lineno,
                MAX_NAME,
            )
            continue

        key = tuple(s.casefold() for s in segments)
        if key in seen:
            logger.warning("Taxonomy line %d: duplicate name %r; skipping.", lineno, name)
            continue
        if len(key) > 1 and key[:-1] not in seen:
            logger.warning(
                "Taxonomy line %d: parent %r is not a label defined above; skipping.",
                lineno,
                " > ".join(segments[:-1]),
            )
            continue

        # Parents are spelled as their own lines spell them, so a case slip in a child's
        # path still lands it under the one existing collection.
        parents = canonical[key[:-1]] if len(key) > 1 else ()
        seen.add(key)
        canonical[key] = parents + (name,)
        labels.append(Label(name=name, description=description, parents=parents))

    return labels


def load_taxonomy(path: Optional[Path]) -> list[Label]:
    """Load and parse the taxonomy file at `path`.

    Args:
        path: the taxonomy file, or `None` when the user has not configured one.

    Returns:
        `[]` when `path` is `None` or does not exist, else the parsed labels.

    Raises:
        TaxonomyError: `path` exists but is not a file, cannot be read, or its contents
            are not valid UTF-8.
    """
    if path is None or not path.exists():
        return []
    if not path.is_file():
        raise TaxonomyError(path, "is not a file")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TaxonomyError(path, "is not valid UTF-8") from exc
    except OSError as exc:
        # A file held open by an editor, or one the user cannot read. MainWindow loads
        # the taxonomy during construction, so anything but TaxonomyError here would
        # stop the application from starting over a file it can run without.
        raise TaxonomyError(path, f"cannot be read ({exc.strerror})") from exc

    return parse_taxonomy(text)


def save_taxonomy(path: Path, labels: Sequence[Label]) -> None:
    """Write `labels` to `path`, one per line, preceded by a header documenting the format."""
    lines = [_HEADER]
    for label in labels:
        head = " > ".join(label.path)
        lines.append(f"{head}: {label.description}" if label.description else head)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
