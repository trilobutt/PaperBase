from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Paper:
    id: Optional[int]
    doi: Optional[str]
    title: str
    authors: list[str]          # ["Lastname, Firstname", ...]
    journal: str
    year: Optional[int]
    volume: str
    issue: str
    pages: str
    abstract: str
    keywords: list[str]
    tags: list[str]
    collection_ids: list[int]
    file_path: str
    date_added: str             # ISO8601 UTC
    date_modified: str          # ISO8601 UTC
    metadata_source: str        # "crossref" | "xmp" | "manual" | "filename"
    needs_review: bool
    open_access: bool
