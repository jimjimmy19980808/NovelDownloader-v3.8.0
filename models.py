"""
Data models used across the application.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Chapter:
    """
    Represents a single novel chapter.
    """

    number: int
    title: str
    url: str
    content: str = ""


@dataclass
class Novel:
    """
    Represents a novel and its chapters.
    """

    title: str
    url: str
    slug: str
    site_name: str = "Unknown Source"
    author: str = "Unknown Author"
    cover_url: Optional[str] = None
    is_completed: Optional[bool] = None
    chapters: List[Chapter] = field(default_factory=list)
