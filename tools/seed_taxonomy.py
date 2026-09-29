"""Seed a taxonomy file from the tags, keywords and journals already in the database.

    py -3.12 tools/seed_taxonomy.py --db %LOCALAPPDATA%\\PaperBase\\PaperBase\\paperbase.db ^
        --out taxonomy.txt

Writes candidate labels drawn from `papers.tags` and `papers.keywords`, merged
case-insensitively and filtered by minimum count, through `taxonomy.save_taxonomy` so the
result parses through `load_taxonomy` by construction. The user hand-edits the file from
there; no embedding model is involved. Opens the database read-only, since this tool never
writes to it. Refuses to overwrite an existing `--out` unless `--force` is passed.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core import taxonomy

_MIN_NAME_LEN = 3
_JOURNAL_LIMIT = 40


def _candidate_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Case-insensitive candidate counts from tags and keywords, keyed by the most
    frequent exact spelling of each.
    """
    rows = conn.execute(
        "SELECT t.value AS name, COUNT(*) AS cnt FROM papers p, json_each(p.tags) t "
        "WHERE p.tags != '[]' AND t.type = 'text' GROUP BY t.value"
    ).fetchall()
    rows += conn.execute(
        "SELECT t.value AS name, COUNT(*) AS cnt FROM papers p, json_each(p.keywords) t "
        "WHERE p.keywords != '[]' AND t.type = 'text' GROUP BY t.value"
    ).fetchall()

    spelling_totals: dict[str, int] = defaultdict(int)
    for row in rows:
        spelling_totals[row["name"]] += row["cnt"]

    merged_totals: dict[str, int] = defaultdict(int)
    best_spelling: dict[str, str] = {}
    best_spelling_count: dict[str, int] = {}
    for name, cnt in spelling_totals.items():
        key = name.casefold()
        merged_totals[key] += cnt
        if cnt > best_spelling_count.get(key, -1):
            best_spelling_count[key] = cnt
            best_spelling[key] = name

    return {best_spelling[key]: total for key, total in merged_totals.items()}


def _writable(name: str) -> bool:
    """Whether `name` survives save_taxonomy then load_taxonomy as the same label.

    A colon would split it into name and description, a leading '#' would make its line a
    comment, and a line break would make it two labels. Tags are free text, so all three
    turn up.
    """
    return (
        name == name.strip()
        and ":" not in name
        and not name.startswith("#")
        and "\n" not in name
        and "\r" not in name
    )


def _filter_candidates(
    counts: dict[str, int], min_count: int, max_labels: int
) -> list[tuple[str, int]]:
    """Drop invalid names, apply the count floor, sort, and truncate to `max_labels`."""
    kept = [
        (name, cnt)
        for name, cnt in counts.items()
        if len(name) >= _MIN_NAME_LEN
        and not name.isdigit()
        and len(name) <= taxonomy.MAX_NAME
        and _writable(name)
        and cnt >= min_count
    ]
    kept.sort(key=lambda item: (-item[1], item[0]))
    return kept[:max_labels]


def _top_journals(conn: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT journal, COUNT(*) AS cnt FROM papers WHERE journal != '' "
        "GROUP BY journal ORDER BY cnt DESC, journal ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(row["journal"], row["cnt"]) for row in rows]


def _journal_comment_block(journals: list[tuple[str, int]]) -> str:
    lines = [
        "#",
        f"# Top {len(journals)} journals by paper count (raw material only, since journals",
        "# are venues, not topics, and must never become labels):",
        "#",
    ]
    lines.extend(f"# {name} ({cnt})" for name, cnt in journals)
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-count", type=int, default=3)
    ap.add_argument("--max-labels", type=int, default=300)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    if args.out.exists() and not args.force:
        print(f"{args.out} already exists; pass --force to overwrite.", file=sys.stderr)
        return 1

    uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = _candidate_counts(conn)
        kept = _filter_candidates(counts, args.min_count, args.max_labels)
        journals = _top_journals(conn, _JOURNAL_LIMIT)
    finally:
        conn.close()

    labels = [taxonomy.Label(name=name) for name, _cnt in kept]
    taxonomy.save_taxonomy(args.out, labels)
    with args.out.open("a", encoding="utf-8") as f:
        f.write(_journal_comment_block(journals))

    print(
        f"candidates considered: {len(counts)}\n"
        f"labels written: {len(labels)}\n"
        f"output: {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
