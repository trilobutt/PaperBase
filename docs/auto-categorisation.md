# PaperBase: auto-categorisation and keywording

Exploration document. Nothing here is built yet. This replaces the earlier RAG and cloud
documents, both dropped.

## Scope

**RAG is dropped.** Answer quality over a 150,000-paper library needs either a paid API or
hardware that is not on the table, and a 3B model on a 4 GB card writing prose over academic
passages is not worth the weekend of embedding it would cost. If that changes, the work is
described in this repository's git history and none of it is prerequisite to anything below.

**Auto-categorisation and keywording stay.** They were a separate request, they need no LLM,
they cost nothing to run, and dropping RAG makes them cheaper rather than harder. If you
want these gone too, say so and this document goes with them.

Everything below is sized for the **target machine**: 4 to 6 cores, 8 to 16 GB of RAM, a
GTX 1050 Ti, and a single spinning 18 TB disk holding the papers. This development machine
has no library on it and is not where any of this runs.

---

## 1. The GPU stops mattering

Worth recording, because it was about to shape the design and no longer does.

PyTorch removed Pascal (`sm_61`, which is what a 1050 Ti is) from its CUDA 12.8 and 12.9
builds, from release 2.8 onward. That would have been a real problem for a job embedding 4.5
million text chunks, and it is why the previous draft moved to ONNX Runtime.

Without RAG, the embedding workload drops from **4.5 million chunks to 150,000 papers**, one
vector each. That runs on the CPU in about an hour. So:

- **No ONNX, no GPU, and no change to the embedding stack.** The declared
  `sentence-transformers` dependency is adequate as it stands. (`yake` is added for
  keywording, which is a separate matter; see section 2.)
- Installing `torch` from PyPI on Windows gives the CPU build by default, which works on any
  machine. The Pascal problem only appears if someone deliberately installs a CUDA build
  expecting the 1050 Ti to help. It will not, and it does not need to.
- Record this in `CLAUDE.md` so nobody spends a day debugging
  `no kernel image is available for execution on the device` later.

---

## 2. The design

The current `EmbeddingCategoriser` already does the hard part: it embeds a paper, takes a
cosine against category descriptions, and extracts keywords with KeyBERT. Three things are
wrong with it at 150,000 papers, and all three have the same root cause.

**It never stores the vector it computed.** Every retroactive run re-embeds the entire
library from scratch, which means changing one category description costs another full pass.
That is what makes the feature painful today, and it is the only structural change needed.

### Persist the paper vectors

One `numpy` file, `paper_vectors.npy`, memory-mapped, plus an id-to-row map:

- 150,000 × 384 float32 = **230 MB**. Comfortable at 8 GB, trivial at 16.
- A separate file rather than a BLOB column on `papers`, because a 1.5 KB blob per row would
  be dragged along by every `SELECT *` and would need excluding from the slim column list in
  `db.py`. A file keeps the hot listing path exactly as it is.
- Written once during the backfill, appended per paper at import.

With that in place, everything downstream becomes arithmetic:

| Operation | Before | After |
|---|---|---|
| Categorise the whole library | re-read and re-embed 150k PDFs | **~1 second** (one 150k×384 by 384×200 matmul) |
| Change a category definition and re-run | hours | **~1 second** |
| Categorise a newly imported paper | one embedding, already paid | unchanged |

That single change is most of the value in this document.

### A taxonomy instead of free-text categories

Settings currently hold a list of `{"name", "description"}` categories. At 150,000 papers
that wants to be a proper taxonomy: **150 to 300 topic labels**, written once, stored in a
file rather than buried in `settings.json`. Seed it from the journals and the tags already
present in the library rather than from a blank page.

Embed the labels once (instant, 300 vectors), then assign each paper its top 3 to 5 labels
above a similarity threshold. The result is a consistent, filterable vocabulary that behaves
like collections, which is the thing free-text keywords never give you.

### Free-text keywords alongside

A fixed taxonomy will never contain the specific things: a technique, an organism, a site
name, a named formation. **YAKE is approved and is what gets used.** It needs no model, no
GPU, and no network.

Measured on this machine rather than estimated, because the earlier description of it was
wrong in two ways.

**Footprint.** It is not the ~50 KB pure-Python package described in the previous draft.
`yake` 0.7.3 is a 91 KB wheel that pulls seven transitive dependencies: `click`, `colorama`,
`jellyfish` (compiled), `networkx`, `segtok`, `tabulate`, `regex` (compiled), plus `numpy`,
which the project already has through `sentence-transformers` and which the vector store
needs anyway. All have prebuilt cp312 Windows wheels, so there is nothing to compile. Call
it 3 MB and seven new names in the lock file.

**Throughput.** 4.1 ms per abstract, so about **10 minutes** for 150,000 papers on one core.

**Quality, and the settings that get there.** Out of the box YAKE is disappointing on
academic abstracts: it is position-biased toward the first sentence and returns overlapping
fragments of the same phrase. With `n=3` it returned "Mycorrhizal fungi form", "fungi form
symbiotic" and "form symbiotic associations" while missing *Pinus sylvestris*, *Suillus
bovinus* and *ectomycorrhizal colonisation*, which are exactly the terms this field exists to
capture. Two changes fix most of it:

1. **`n=2`.** Bigrams surface the binomials and place names; `n=3` fills the list with
   overlapping fragments and `n=1` loses multi-word terms entirely.
2. **A token-overlap filter.** Take the top 25 raw candidates, walk them best-first, and drop
   any whose words intersect a candidate already kept. Keep 6.

```python
def keywords(text: str, top: int = 6) -> list[str]:
    raw = yake.KeywordExtractor(lan="en", n=2, top=25, dedupLim=0.9).extract_keywords(text)
    kept: list[str] = []
    seen: set[str] = set()
    for phrase, _score in raw:          # yake yields best-first; lower score is better
        tokens = {t.lower().strip(".,;:()") for t in phrase.split()}
        if tokens & seen:
            continue
        kept.append(phrase)
        seen |= tokens
        if len(kept) == top:
            break
    return kept
```

On three sample abstracts (mycorrhizal ecology, Cambrian palaeontology, stable-isotope
archaeology) that yields *Mycorrhizal fungi, land plants, northern Sweden, Pinus sylvestris,
Suillus bovinus* / *British Columbia, Burgess Shale, middle Cambrian, soft-tissue detail* /
*Stable isotope, bone collagen, Catalhoyuk, marine protein*.

**Expect roughly four or five useful keywords out of six.** The residue is verb phrases
("form symbiotic", "specimens preserve", "including gut") that a short stop-list of
connectives and reporting verbs will mostly remove once you have seen a few hundred real
outputs. Tune that list against the library rather than guessing it up front.

Two fields doing two jobs: the taxonomy for filtering and navigation, free-text keywords for
specificity.

---

## 3. What it costs to run on the target machine

| Stage | Estimate |
|---|---|
| Extract title and abstract where missing (PDF text, first page only) | 1 to 3 hours |
| Embed 150,000 papers on 4 to 6 CPU cores | **~1 to 2 hours** |
| Embed the taxonomy labels | instant |
| Assign labels across the library | ~1 second |
| YAKE keywords across the library (measured: 4.1 ms/abstract) | ~10 minutes |

Call it an afternoon, once, unattended and resumable. Every subsequent change to the
taxonomy is seconds.

Most papers already have an abstract from Crossref, so the extraction stage only touches the
ones that do not. Embedding title plus abstract is both cheaper and better than embedding
full text for this purpose: categorisation is a question about what a paper is about, and
the abstract is the part written to answer it.

---

## 4. What changes in the code

- `core/categoriser.py`: load and memory-map `paper_vectors.npy`; `categorise_paper` takes a
  stored vector when one exists rather than embedding again; the taxonomy replaces the
  free-text category list.
- `core/db.py`: nothing. The vectors live in a file, and category assignments continue to go
  into the existing `collection_ids` and `tags` JSON arrays, so the flat-schema rule is
  untouched.
- `core/importer.py`: after `_apply_categorisation`, append the new paper's vector. This is
  the same five call sites the `place_file` gotcha in `CLAUDE.md` already warns about.
- `ui/categorisation_dialog.py`: the retroactive run becomes fast enough that its progress
  dialog is mostly redundant, though the first backfill still needs it.
- `ui/settings_dialog.py`: point at a taxonomy file instead of editing categories in a table.

| Piece | Estimate |
|---|---|
| Paper vector store, backfill job (resumable) | 2 days |
| Taxonomy file, label embedding, assignment pass | 1 to 2 days |
| YAKE keywording plus the overlap filter and stop-list | 1 day |
| Settings and dialog changes | 1 day |

---

## 5. Order

1. **First: `tasks/todo.md`.** Import counting, startup performance, redesign. The startup
   work matters more on the target machine than it would here: loading 150,000 `Paper`
   objects before the window is usable is a slow launch on 64 GB and NVMe, and swapping on
   8 GB and a platter.
2. Paper vector store and backfill.
3. Taxonomy and assignment.
4. Keywording.

---

## 6. Still open

1. **Where does the taxonomy come from?** Writing 200 good labels for your own library is a
   couple of hours of your time and cannot be delegated to the tool. Seeding a draft from
   the existing journals and tags makes it an editing job rather than a blank page.
2. **Remote access**, if it is still wanted: Parsec or Tailscale with remote desktop, both
   free. Not designed here, and unaffected by anything above.

Backup is handled outside PaperBase and is deliberately out of scope.
