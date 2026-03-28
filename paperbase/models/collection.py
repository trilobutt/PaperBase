from dataclasses import dataclass
from typing import Optional


@dataclass
class Collection:
    id: Optional[int]
    name: str
    parent_id: Optional[int]
