"""Build a throwaway PaperBase data directory for manual testing.

    py -3.12 tools/make_fixture.py --papers 5000 --out %TEMP%\\pb_fix
    set PAPERBASE_DATA_DIR=%TEMP%\\pb_fix
    py -3.12 -m paperbase.main

Writes paperbase.db, index/ and settings.json. No PDFs are created, so opening a paper
will fail; everything else (listing, search, filters, editing, layout) works.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.core.taxonomy import Label, save_taxonomy
from paperbase.models.collection import Collection
from paperbase.models.paper import Paper

JOURNALS = [
    "Nature", "Science", "Cell", "PNAS", "Journal of Ecology", "Systematic Biology",
    "Palaeontology", "Molecular Biology and Evolution", "Ecology Letters",
    "Philosophical Transactions of the Royal Society B",
]
WORDS = [
    "mycorrhizal", "phylogenetic", "sediment", "isotope", "cambrian", "predation",
    "symbiosis", "trilobite", "stratigraphy", "genome", "morphology", "biogeography",
    "taphonomy", "speciation", "extinction", "pollination", "carbonate", "clade",
]
ORGANISMS = [
    "trilobites", "brachiopods", "ammonites", "early tetrapods", "foraminifera",
    "gastropods", "conodonts", "mycorrhizal fungi", "coral reefs", "graptolites",
]
PLACES = [
    "the Burgess Shale", "the Chengjiang biota", "the Welsh Borderlands",
    "coastal Madagascar", "the Ediacaran outcrops of Namibia", "the Baltic basin",
    "the Atacama Desert", "the Yangtze Platform",
]
TECHNIQUES = [
    "stable isotope analysis", "phylogenetic reconstruction",
    "computed tomography scanning", "geochemical proxy analysis",
    "morphometric analysis", "molecular clock dating",
]
TAGS = ["palaeo", "ecology", "genomics", "methods", "review", "fieldwork", "taxonomy"]


def _title(rng: random.Random) -> str:
    return " ".join(rng.sample(WORDS, 6)).capitalize()


def _abstract(rng: random.Random) -> str:
    """Build a three or four sentence abstract from templates with real phrase structure.

    A word bag produces meaningless YAKE keywords; these sentences give it position and
    co-occurrence to score against, the same as a real abstract would.
    """
    organism = rng.choice(ORGANISMS)
    place = rng.choice(PLACES)
    technique = rng.choice(TECHNIQUES)
    sentences = [
        f"We examined {organism} from {place} to investigate patterns of {rng.choice(WORDS)}.",
        f"Using {technique}, we reconstructed the timing of these changes across "
        f"multiple {rng.choice(WORDS)} horizons.",
        f"{organism.capitalize()} assemblages show clear evidence of "
        f"{rng.choice(WORDS)} linked to shifts in local {rng.choice(WORDS)}.",
    ]
    if rng.random() < 0.5:
        sentences.append(
            f"These findings, supported by {rng.choice(TECHNIQUES)}, suggest that "
            f"{rng.choice(WORDS)} played a larger role than previously recognised."
        )
    return " ".join(sentences)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--papers", type=int, default=5000)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--index", action="store_true",
                    help="also build the Tantivy index (slow; needed to test search)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out: Path = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)

    db = Database(out / "paperbase.db")
    db.open()

    collections = [db.insert_collection(Collection(id=None, name=n, parent_id=None))
                   for n in ("Palaeobiology", "Ecology", "Methods")]

    indexer = Indexer(out / "index") if args.index else None
    if indexer:
        indexer.open()

    base = datetime.now(timezone.utc)
    for i in range(args.papers):
        added = (base - timedelta(minutes=i)).isoformat()
        paper = Paper(
            id=None,
            doi=f"10.1234/fixture.{i}",
            title=_title(rng),
            authors=[f"{rng.choice(WORDS).capitalize()}, {chr(65 + i % 26)}."
                     for _ in range(rng.randint(1, 4))],
            journal=rng.choice(JOURNALS),
            year=rng.randint(1968, 2026),
            volume=str(rng.randint(1, 300)),
            issue=str(rng.randint(1, 12)),
            pages=f"{rng.randint(1, 900)}-{rng.randint(901, 1200)}",
            abstract=_abstract(rng),
            keywords=rng.sample(WORDS, 3),
            tags=rng.sample(TAGS, rng.randint(0, 3)),
            collection_ids=rng.sample(collections, rng.randint(0, 2)),
            file_path=str(out / "papers" / f"fixture_{i:06d}.pdf"),
            date_added=added,
            date_modified=added,
            metadata_source="crossref",
            needs_review=(i % 37 == 0),
            open_access=(i % 3 == 0),
            isbn=None,
            document_type="book" if i % 50 == 0 else "article",
        )
        paper.id = db.insert_paper(paper)
        if indexer:
            indexer.add_document(paper, " ".join(rng.choices(WORDS, k=400)))
            if i % 200 == 0:
                indexer.commit()
    if indexer:
        indexer.commit()
        indexer.close()

    settings = {
        "version": 2,
        "library_root": str(out / "library"),
        "user_email": "fixture@example.com",
        "folder_pattern": "{journal}/{year}/{author} ({year}) {title}.pdf",
        "last_import_dir": "",
        "secondary_dest": "",
        "categories": [],
        "auto_categorise": False,
        "category_threshold": 0.35,
        "tag_count": 5,
        "taxonomy_path": "",
        "reduce_motion": False,
    }
    (out / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    (out / "library").mkdir(exist_ok=True)
    taxonomy_labels = [
        Label(name=name, description=description)
        for name, description in (
            ("Palaeoecology", "Interactions between fossil organisms and ancient environments"),
            ("Phylogenetics", ""),
            ("Sedimentology", "Formation and structure of sedimentary rock layers"),
            ("Mycorrhizal symbiosis", ""),
            ("Isotope geochemistry", "Stable and radiogenic isotopes as environmental tracers"),
            ("Cambrian biota", ""),
            ("Predator-prey dynamics", "Trophic interactions in fossil and modern communities"),
            ("Genome evolution", ""),
            ("Biogeography", "Spatial distribution of species and populations"),
            ("Taphonomy", ""),
            ("Speciation", "Processes by which new species arise"),
            ("Pollination ecology", ""),
        )
    ]
    save_taxonomy(out / "library" / "taxonomy.txt", taxonomy_labels)
    db.close()
    print(f"fixture ready: {out} ({args.papers} papers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
