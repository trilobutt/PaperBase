"""
Taxon tree: the hand-edited `taxa.txt`, and the lexical matcher that assigns its taxa.

Taxa are matched by name and alias in a paper's title, abstract and keywords rather than
by embedding: MiniLM cannot separate sibling families, and systematic papers name their
taxa outright ("(Coleoptera: Histeridae)"). The file is untrusted input validated at the
boundary, like the topic taxonomy: a bad line is logged and skipped, never raised.

File format, one taxon per line:

- `Parent > Child > Name: alias, alias`. Only the first colon splits. The last path
  segment is the taxon; the segments before it must be the path of a taxon defined on an
  earlier line.
- `Name [informal]` marks a non-monophyletic grouping kept for convenience. The marker is
  not part of the name, and may also appear on the parent segments of later lines.
- Names are unique across the file, case-insensitively. An alias already claimed by an
  earlier taxon, or equal to another taxon's name, is dropped with a warning.
- `#` comments and blank lines are ignored.

Matching (`TaxonTree.match`):

- A name, and an alias holding any capital letter, match in exact case, or in full
  capitals when they hold at least `_CAPS_MIN` letters. Old titles set taxa in capitals;
  shorter capitalised forms collide with acronyms such as HOMO and SAR.
- An all-lowercase alias matches in lower case, with its first letter capitalised, or in
  Title Case, singular or plural (`plural_variants`, last word only).
- Whole words: text is split into words, a hyphen or apostrophe joining a word rather
  than ending it, so "rays" never matches inside "X-rays". A multi-word form matches
  only across plain spaces, never across punctuation or a line break.
- Longest form first, and matched words are consumed: "sea spiders" (Pycnogonida) is
  never also read as "spiders" (Araneae).
- Only the most specific taxa are kept: a matched taxon whose descendant also matched is
  dropped, since ancestors are resolved from the tree at query time.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from paperbase.core.taxonomy import MAX_NAME, TaxonomyError

logger = logging.getLogger(__name__)

INFORMAL_MARKER = " [informal]"
_CAPS_MIN = 6
_WORD = re.compile(r"[^\W_]+(?:['\-][^\W_]+)*")


@dataclass(frozen=True)
class Taxon:
    """One node of the taxon tree. `parent` is None for a root."""

    name: str
    parent: Optional[str] = None
    aliases: tuple[str, ...] = ()
    informal: bool = False


def plural_variants(alias: str) -> set[str]:
    """Singular and plural spellings of a lowercase alias, varying its last word only.

    Deliberately crude: it serves aliases written as ordinary English plurals or
    singulars. An irregular pair (louse/lice, goose/geese) is listed as two aliases.
    """
    head, _, word = alias.rpartition(" ")
    prefix = head + " " if head else ""
    out = {word}
    if word.endswith("ies") and len(word) > 4:
        out.add(word[:-3] + "y")
    elif word.endswith(("ses", "xes", "zes", "ches", "shes", "oes")):
        out.add(word[:-2])
    elif word.endswith("s") and not word.endswith(("ss", "us")):
        out.add(word[:-1])
    else:
        out.add(word + "s")
        if word.endswith(("s", "x", "z", "ch", "sh", "o")):
            out.add(word + "es")
        if len(word) > 1 and word.endswith("y") and word[-2] not in "aeiou":
            out.add(word[:-1] + "ies")
    return {prefix + v for v in out}


def surface_forms(term: str, *, is_name: bool) -> set[str]:
    """Every spelling of `term` the matcher accepts, per the module docstring."""
    if is_name or not term.islower():
        forms = {term}
        if sum(c.isalpha() for c in term) >= _CAPS_MIN:
            forms.add(term.upper())
        return forms
    forms = set()
    for variant in plural_variants(term):
        forms.add(variant)
        forms.add(variant[0].upper() + variant[1:])
        forms.add(" ".join(w[0].upper() + w[1:] for w in variant.split(" ")))
    return forms


def _spaced(text: str, words: list[re.Match[str]]) -> bool:
    """Whether consecutive `words` are separated by plain spaces and nothing else."""
    return all(text[a.end() : b.start()].strip(" ") == "" for a, b in zip(words, words[1:]))


class TaxonTree:
    """The parsed taxon tree plus its form lookup. Immutable once built."""

    def __init__(self, taxa: list[Taxon]) -> None:
        self._taxa: dict[str, Taxon] = {t.name: t for t in taxa}
        self._order: list[str] = [t.name for t in taxa]
        self._folded: dict[str, str] = {t.name.casefold(): t.name for t in taxa}
        self._children: dict[Optional[str], list[str]] = {}
        for t in taxa:
            self._children.setdefault(t.parent, []).append(t.name)

        # Names first, so a name always outranks a colliding alias form.
        owner: dict[str, str] = {}
        for t in taxa:
            for form in surface_forms(t.name, is_name=True):
                owner.setdefault(form, t.name)
        for t in taxa:
            for alias in t.aliases:
                for form in surface_forms(alias, is_name=False):
                    owner.setdefault(form, t.name)
        self._owner = owner
        self._max_words = max((f.count(" ") + 1 for f in owner), default=0)

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, name: object) -> bool:
        return name in self._taxa

    @property
    def names(self) -> list[str]:
        """Every taxon name, in file order (a parent always before its children)."""
        return list(self._order)

    def get(self, name: str) -> Optional[Taxon]:
        return self._taxa.get(name)

    def canonical(self, text: str) -> Optional[str]:
        """The taxon name `text` spells, ignoring case and surrounding space, or None."""
        return self._folded.get(text.strip().casefold())

    def children(self, name: Optional[str]) -> list[str]:
        """Direct children of `name` in file order; `None` gives the roots."""
        return list(self._children.get(name, []))

    def ancestors(self, name: str) -> list[str]:
        """`name`'s ancestors, nearest first. Empty for a root or an unknown name."""
        out: list[str] = []
        taxon = self._taxa.get(name)
        while taxon is not None and taxon.parent is not None:
            out.append(taxon.parent)
            taxon = self._taxa.get(taxon.parent)
        return out

    def descendants_and_self(self, name: str) -> list[str]:
        """`name` and every taxon below it. Empty for an unknown name."""
        out: list[str] = []
        stack = [name]
        while stack:
            current = stack.pop()
            if current in self._taxa:
                out.append(current)
                stack.extend(self._children.get(current, []))
        return out

    def most_specific(self, names: set[str]) -> list[str]:
        """`names` minus any that is an ancestor of another, sorted."""
        covered: set[str] = set()
        for name in names:
            covered.update(self.ancestors(name))
        return sorted(n for n in names if n not in covered)

    def match(self, text: str) -> list[str]:
        """The most specific taxa `text` names, sorted. See the module docstring."""
        if not self._owner or not text:
            return []
        words = list(_WORD.finditer(text))
        hits: set[str] = set()
        i = 0
        while i < len(words):
            for n in range(min(self._max_words, len(words) - i), 0, -1):
                window = words[i : i + n]
                if not _spaced(text, window):
                    continue
                owner = self._owner.get(" ".join(m.group() for m in window))
                if owner is not None:
                    hits.add(owner)
                    i += n
                    break
            else:
                i += 1
        return self.most_specific(hits)


def _strip_marker(segment: str) -> tuple[str, bool]:
    if segment.endswith(INFORMAL_MARKER):
        return segment[: -len(INFORMAL_MARKER)].strip(), True
    return segment, False


def parse_taxa(text: str) -> TaxonTree:
    """Parse `taxa.txt` contents, skipping invalid lines and aliases with a warning."""
    rows: list[tuple[int, str, Optional[str], bool, str]] = []
    paths: dict[str, tuple[str, ...]] = {}      # casefolded name -> casefolded path
    canonical: dict[str, str] = {}              # casefolded name -> name as defined

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, raw_aliases = line.partition(":")
        segments = [_strip_marker(s.strip()) for s in head.split(">")]
        name, informal = segments[-1]
        parents = [s for s, _flag in segments[:-1]]

        if not name or not all(parents):
            logger.warning("Taxa line %d: empty name in path; skipping.", lineno)
            continue
        if len(name) > MAX_NAME:
            logger.warning(
                "Taxa line %d: name longer than MAX_NAME (%d); skipping.", lineno, MAX_NAME
            )
            continue
        key = name.casefold()
        if key in paths:
            logger.warning("Taxa line %d: duplicate name %r; skipping.", lineno, name)
            continue
        folded_parents = tuple(p.casefold() for p in parents)
        if parents and paths.get(folded_parents[-1]) != folded_parents:
            logger.warning(
                "Taxa line %d: %r is not the path of a taxon defined above; skipping.",
                lineno,
                " > ".join(parents),
            )
            continue

        paths[key] = folded_parents + (key,)
        canonical[key] = name
        parent = canonical[folded_parents[-1]] if parents else None
        rows.append((lineno, name, parent, informal, raw_aliases))

    claimed: dict[str, str] = {}                # casefolded alias -> owning taxon
    taxa: list[Taxon] = []
    for lineno, name, parent, informal, raw_aliases in rows:
        kept: list[str] = []
        for alias in (a.strip() for a in raw_aliases.split(",")):
            if not alias or alias == name or alias in kept:
                continue
            key = alias.casefold()
            if key in paths and key != name.casefold():
                logger.warning(
                    "Taxa line %d: alias %r is another taxon's name; dropped.", lineno, alias
                )
                continue
            owner = claimed.setdefault(key, name)
            if owner != name:
                logger.warning(
                    "Taxa line %d: alias %r already belongs to %s; dropped.", lineno, alias, owner
                )
                continue
            kept.append(alias)
        taxa.append(Taxon(name=name, parent=parent, aliases=tuple(kept), informal=informal))
    return TaxonTree(taxa)


def load_taxa(path: Optional[Path]) -> TaxonTree:
    """Load and parse the taxon tree at `path`; an empty tree when `path` is None or absent.

    Raises:
        TaxonomyError: `path` exists but is not a file, cannot be read, or is not UTF-8.
    """
    if path is None or not path.exists():
        return TaxonTree([])
    if not path.is_file():
        raise TaxonomyError(path, "is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TaxonomyError(path, "is not valid UTF-8") from exc
    except OSError as exc:
        # Same reasoning as load_taxonomy: MainWindow loads this during construction.
        raise TaxonomyError(path, f"cannot be read ({exc.strerror})") from exc
    return parse_taxa(text)
