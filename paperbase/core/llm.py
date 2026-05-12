from typing import Optional

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.models.paper import Paper


class LLMCategoriser:
    """Thin adapter preserving the original stub interface over EmbeddingCategoriser."""

    def __init__(self, categoriser: EmbeddingCategoriser, db: Database) -> None:
        self._categoriser = categoriser
        self._db = db

    def categorise(
        self,
        paper: Paper,
        target_collections: list[str],
        existing_tags: list[str],
    ) -> tuple[Optional[str], list[str]]:
        col_ids, tags = self._categoriser.categorise_paper(paper, self._db)
        col_name: Optional[str] = None
        if col_ids:
            col = self._db.get_collection(col_ids[0])
            if col:
                col_name = col.name
        return col_name, tags
