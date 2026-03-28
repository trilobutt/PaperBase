from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchResult:
    paper_id: int
    title: str
    authors: list[str]
    journal: str
    year: Optional[int]
    snippet: str        # Tantivy-generated excerpt with search terms highlighted
    score: float
