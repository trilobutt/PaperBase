"""Report how many topic labels and taxa the current files and threshold assign.

    py -3.12 tools/label_stats.py [--thresholds 0.3 0.35 0.4 0.45 0.5] [--sample 5000]

Run it with PaperBase closed: it opens the data directory (PAPERBASE_DATA_DIR, else the
real one) and the vector store beside it. It changes no paper.

Topics: with no per-paper cap, the similarity threshold is the only control on how many
labels a paper gets. For each candidate threshold this prints the distribution of stored
labels per paper (the most specific per path, as categorisation stores them) over every
stored vector. It needs sentence-transformers to embed the labels, and stored vectors
(one categorisation run makes them), and says so and skips the section otherwise.

Taxa: over a random sample of papers this prints the share matched to at least one taxon
and the most frequent taxa, which is where an alias that fires too often shows itself.
"""
import argparse
import random
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core.assign import labels_above
from paperbase.core.categoriser import EmbeddingCategoriser, most_specific_labels, taxa_text
from paperbase.core.db import Database
from paperbase.core.taxa import load_taxa
from paperbase.core.taxonomy import load_taxonomy
from paperbase.core.vectors import VectorStore
from paperbase.main import _data_dir
from paperbase.ui.settings_dialog import Settings


def _taxa_section(settings: Settings, db: Database, sample: int) -> None:
    taxa = load_taxa(settings.taxa_file())
    if len(taxa) == 0:
        print("Taxa: no taxa.txt beside the taxonomy file, skipped.")
        return
    ids = db.get_all_paper_ids()
    chosen = random.Random(0).sample(ids, min(sample, len(ids)))
    hits: Counter[str] = Counter()
    matched = 0
    for paper in db.get_papers_by_ids(chosen):
        found = taxa.match(taxa_text(paper))
        matched += bool(found)
        hits.update(found)
    share = 100 * matched / max(len(chosen), 1)
    print(
        f"Taxa: {len(taxa)} taxa; {matched:,} of {len(chosen):,} sampled papers "
        f"({share:.1f}%) match at least one"
    )
    for name, n in hits.most_common(30):
        print(f"  {n:6,}  {name}")


def _topic_section(settings: Settings, data: Path, thresholds: list[float]) -> None:
    labels = load_taxonomy(settings.taxonomy_file())
    if not labels:
        print("Topics: no taxonomy labels, skipped.")
        return
    store = VectorStore(data / "paper_vectors.f32", model_name=EmbeddingCategoriser.MODEL_NAME)
    store.open()
    matrix, ids = store.matrix()
    if not ids:
        print("Topics: no stored vectors yet (run Categorise once), skipped.")
        return
    cat = EmbeddingCategoriser()
    cat.update_settings(categories=[], threshold=0.0, tag_count=0, labels=labels)
    cat.load_model()
    label_matrix = cat.label_matrix()
    if label_matrix is None:
        print("Topics: sentence-transformers is not installed, skipped.")
        return
    print(f"Topics: {len(labels)} labels over {len(ids):,} stored vectors")
    print("threshold   mean  median    p95    max   none%")
    for t in thresholds:
        rows = labels_above(matrix, label_matrix, t)
        counts = np.array(
            [len(most_specific_labels([i for i, _ in r], labels)) for r in rows]
        )
        print(
            f"{t:9.2f} {counts.mean():6.2f} {np.median(counts):7.1f} "
            f"{np.percentile(counts, 95):6.1f} {int(counts.max()):6d} "
            f"{100 * (counts == 0).mean():7.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--thresholds", type=float, nargs="+", default=[0.30, 0.35, 0.40, 0.45, 0.50]
    )
    parser.add_argument("--sample", type=int, default=5000)
    args = parser.parse_args()

    data = _data_dir()
    settings = Settings.load(data / "settings.json")
    db = Database(data / "paperbase.db")
    db.open()
    try:
        _taxa_section(settings, db, args.sample)
        _topic_section(settings, data, args.thresholds)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
