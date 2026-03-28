from typing import Optional

from paperbase.models.paper import Paper


class LLMCategoriser:
    """
    Stub for v1. Provides the interface that v2 will implement.
    """

    def categorise(
        self,
        paper: Paper,
        target_collections: list[str],
        existing_tags: list[str],
    ) -> tuple[Optional[str], list[str]]:
        """
        Returns (suggested_collection_name, suggested_tags).
        v1: always returns (None, []).
        v2: send paper.title + paper.abstract to an LLM API with a structured prompt
        instructing it to select from target_collections and suggest tags.
        Results shown to user for review before being applied.
        """
        return None, []
