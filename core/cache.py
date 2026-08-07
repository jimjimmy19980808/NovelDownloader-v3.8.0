"""
Novel chapter cache management.

The cache is a *performance* layer only (avoid re-downloading a chapter we
already fetched this run/session) - it is never the source of truth for
"what has been downloaded". See core/library.py for why: PDFs are the
source of truth, so deleting the cache folder is always safe.
"""

from pathlib import Path

from config import CACHE_DIR


class CacheManager:
    """
    Handles chapter cache storage for each novel.
    """

    def __init__(self, novel_slug: str):
        self.novel_slug = novel_slug
        self.novel_cache_dir = CACHE_DIR / novel_slug
        self.novel_cache_dir.mkdir(parents=True, exist_ok=True)

    def chapter_file(self, chapter_number: int) -> Path:
        """
        Return chapter cache file path.
        """
        return self.novel_cache_dir / f"chapter_{chapter_number}.txt"

    def exists(self, chapter_number: int) -> bool:
        """
        Check if chapter exists in cache.
        """
        return self.chapter_file(chapter_number).exists()

    def save(self, chapter_number: int, content: str) -> None:
        """
        Save chapter content.
        """
        self.chapter_file(chapter_number).write_text(content, encoding="utf-8")

    def load(self, chapter_number: int) -> str | None:
        """
        Load cached chapter.
        """
        path = self.chapter_file(chapter_number)

        if not path.exists():
            return None

        return path.read_text(encoding="utf-8")

    def cached_numbers(self) -> set[int]:
        """
        Return the set of chapter numbers currently sitting in cache, so
        callers can e.g. rebuild PDFs for chapters that are cached but not
        yet in any PDF, without re-downloading them.
        """
        numbers = set()

        for file in self.novel_cache_dir.glob("chapter_*.txt"):
            try:
                numbers.add(int(file.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue

        return numbers

    def clear(self) -> None:
        """
        Delete all cached chapter files for this novel. Safe to call at any
        time since PDFs (not cache) are the source of truth for what has
        been downloaded.
        """
        for file in self.novel_cache_dir.glob("chapter_*.txt"):
            file.unlink(missing_ok=True)
