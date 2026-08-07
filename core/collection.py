"""
"My Library": the list of novels the user has chosen to track - separate
from core/library.py, which only answers "which chapters of THIS novel are
downloaded". This is the Tachiyomi/Mihon-style saved list: add a novel
once by URL (or later, search), then come back to it anytime without
re-pasting the link.

Stored at data/my_novels.json, keyed by slug.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from config import DATA_DIR
from core.utils import load_json, save_json
from models import Novel

COLLECTION_FILE = DATA_DIR / "my_novels.json"


@dataclass
class SavedNovel:
    title: str
    url: str
    slug: str
    site_name: str = "Unknown Source"
    cover_url: Optional[str] = None
    added_at: str = ""
    known_chapter_count: int = 0


class Collection:
    """
    Manages the user's saved novel list.
    """

    def __init__(self):
        self._data = load_json(COLLECTION_FILE, default={}) or {}

    def _persist(self) -> None:
        save_json(COLLECTION_FILE, self._data)

    def list_all(self) -> list[SavedNovel]:
        return [SavedNovel(**entry) for entry in self._data.values()]

    def get(self, slug: str) -> Optional[SavedNovel]:
        entry = self._data.get(slug)
        return SavedNovel(**entry) if entry else None

    def contains(self, slug: str) -> bool:
        return slug in self._data

    def add(self, novel: Novel) -> SavedNovel:
        existing = self._data.get(novel.slug, {})

        saved = SavedNovel(
            title=novel.title,
            url=novel.url,
            slug=novel.slug,
            site_name=novel.site_name,
            cover_url=novel.cover_url,
            added_at=existing.get("added_at") or datetime.now().isoformat(timespec="seconds"),
            known_chapter_count=existing.get("known_chapter_count", 0),
        )

        self._data[novel.slug] = asdict(saved)
        self._persist()

        return saved

    def update_known_count(self, slug: str, chapter_count: int) -> None:
        if slug not in self._data:
            return

        self._data[slug]["known_chapter_count"] = chapter_count
        self._persist()

    def remove(self, slug: str) -> bool:
        if slug not in self._data:
            return False

        del self._data[slug]
        self._persist()

        return True
