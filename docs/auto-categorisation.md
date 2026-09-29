# PaperBase: auto-categorisation and keywording

The vector store, taxonomy assignment and YAKE keywording described below are implemented.
What remains is the first backfill run on the target machine and writing the taxonomy
itself. This replaces the earlier RAG and cloud documents, both dropped.

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

---

## 2. The design

The `EmbeddingCategoriser` this design started from already did the hard part: it embedded a
paper, took a cosine against category descriptions, and extracted keywords with KeyBERT. At
150,000 papers one flaw made it painful.

**It never stored the vector it computed.** Every retroactive run re-embedded the entire
library from scratch, so changing one category description cost another full pass. Persisting
the vectors is the only structural change the design needs.

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

- `core/vectors.py`: `VectorStore`, a persistent id-keyed cache of paper embeddings, backed
  by `paper_vectors.f32` (a headerless little-endian float32 blob) plus a JSON sidecar
  holding `dim`, `model` and the row-ordered paper ids. Not `paper_vectors.npy` as first
  proposed above: a `.npy` header carries the array's shape, so every append would rewrite
  the whole header; a headerless blob appends with a plain write and reads back with one
  `np.memmap`, keeping the shape in the sidecar's id list instead.
- `core/taxonomy.py`: `Label` and `parse_taxonomy`/`load_taxonomy`/`save_taxonomy` for the
  taxonomy file, one label per line as `Name` or `Name: description`.
- `core/assign.py`: `top_labels`, the chunked cosine-similarity matmul that turns a document
  matrix and a label matrix into per-paper label assignments without holding the full
  150,000 × labels score matrix in memory at once.
- `core/keywords.py`: `extract_keywords`, YAKE plus the token-overlap filter from section 2.
  `keybert` is removed from the dependency list entirely.
- `core/categoriser.py`: `EmbeddingCategoriser.categorise_papers` persists every paper's
  vector into a `VectorStore` as part of the same batched encode it already does, so a
  caller never has to encode a second time to store what this method already computed.
  `CategorizationWorker` runs two stages: `embedding` (encode whatever the vector store is
  missing) and `assigning` (derive collections and tags from the stored vectors, with no
  re-embedding). The vector store's id map is the resume record for the expensive stage, so
  the JSON progress file this document originally proposed does not exist.
- `core/importer.py`: `ImportWorker` passes its `VectorStore` into `categorise_papers`, so
  every imported paper's vector is stored the moment it is computed, and flushes the store
  alongside the Tantivy index commit.
- `ui/categorisation_dialog.py`: a two-stage progress dialog. An hour-long embedding stage
  carries the ETA; the seconds-long assigning stage that follows does not read as a stall,
  because the status line names which stage is running rather than showing one bar for both.
- `ui/settings_dialog.py`: a taxonomy file path and a labels-per-paper setting, alongside the
  free-text categories table, which stays.

---

## 5. Order

What's left, now that the vector store, taxonomy assignment and keywording are built:

1. **Run the backfill** on the target machine, once `sentence-transformers` is installed and
   the library is in place. This is real-machine work the development machine that built the
   rest of this cannot do.
2. **Seed and edit the taxonomy.** `tools/seed_taxonomy.py` drafts 150 to 300 labels from the
   library's existing tags and keywords; a person edits the draft into shape from there.
3. **Extract title and abstract where missing**, the first row of section 3's cost table.
   Unbuilt: it has to survive real publisher PDFs, and only the target machine has them.
   Until it lands, a paper with neither title nor abstract gets no vector and no labels.

---

## 6. Still open

1. **Where does the taxonomy come from?** Writing 200 good labels for your own library is a
   couple of hours of your time and cannot be delegated to the tool. Seeding a draft from
   the existing journals and tags makes it an editing job rather than a blank page.
2. **Remote access**, if it is still wanted: Parsec or Tailscale with remote desktop, both
   free. Not designed here, and unaffected by anything above.

Backup is handled outside PaperBase and is deliberately out of scope.
