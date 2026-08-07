"""
Chapter downloader: fetches chapters with type-aware retry/backoff,
duplicate detection, minimum-length sanity checks, and RAM cleanup between
batches so multi-thousand-chapter novels don't balloon memory usage.
"""

import gc
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    DEFAULT_DOWNLOAD_BATCH,
    DEFAULT_MAX_WORKERS,
    MIN_CHAPTER_LENGTH,
    MIN_VALID_CHAPTER_LENGTH,
    NETWORK_ERROR_RETRY_DELAYS,
    RATE_LIMIT_RETRY_DELAYS,
)
from core.cache import CacheManager
from core.logger import get_logger
from core.session import CloudflareBlockedError, NetworkError, RateLimitedError, ServerError
from core.utils import progress_bar
from models import Chapter, Novel

log = get_logger(__name__)

MAX_RETRY = max(len(RATE_LIMIT_RETRY_DELAYS), len(NETWORK_ERROR_RETRY_DELAYS))


class ChapterDownloader:
    """
    Handles chapter downloading and caching.
    """

    def __init__(self, parser, novel: Novel, max_workers: int = DEFAULT_MAX_WORKERS):
        self.parser = parser
        self.novel = novel
        self.cache = CacheManager(novel.slug)
        self.max_workers = max_workers
        self.failed_chapters: list[int] = []
        self.broken_chapters: list[int] = []  # downloaded but suspiciously short

    def download_chapter(self, chapter: Chapter) -> Chapter:
        if self.cache.exists(chapter.number):
            chapter.content = self.cache.load(chapter.number) or ""
            return chapter

        retry = 0

        while retry < MAX_RETRY:
            try:
                content = self.parser.get_chapter_content(chapter)

                if not content or len(content.strip()) < MIN_VALID_CHAPTER_LENGTH:
                    raise RuntimeError("Empty or near-empty chapter content")

                if len(content.strip()) < MIN_CHAPTER_LENGTH:
                    log.warning(
                        f"Chapter {chapter.number}: possibly broken "
                        f"({len(content.strip())} chars)"
                    )
                    self.broken_chapters.append(chapter.number)

                chapter.content = content
                self.cache.save(chapter.number, content)

                return chapter

            except RateLimitedError as error:
                retry += 1
                delay = error.retry_after or RATE_LIMIT_RETRY_DELAYS[
                    min(retry - 1, len(RATE_LIMIT_RETRY_DELAYS) - 1)
                ]
                if retry < MAX_RETRY:
                    print(
                        f"Rate limited on chapter {chapter.number} "
                        f"(retry {retry}/{MAX_RETRY}) - waiting {delay}s"
                    )
                    log.warning(f"429 on chapter {chapter.number}, waiting {delay}s")
                    time.sleep(delay)
                else:
                    print(f"Rate limited on chapter {chapter.number} - out of retries")

            except (ServerError, NetworkError) as error:
                retry += 1
                delay = NETWORK_ERROR_RETRY_DELAYS[
                    min(retry - 1, len(NETWORK_ERROR_RETRY_DELAYS) - 1)
                ]
                if retry < MAX_RETRY:
                    print(
                        f"Network issue on chapter {chapter.number} "
                        f"(retry {retry}/{MAX_RETRY}): {error} - waiting {delay}s"
                    )
                    log.warning(f"{error} on chapter {chapter.number}, waiting {delay}s")
                    time.sleep(delay)
                else:
                    print(f"Network issue on chapter {chapter.number}: {error} - out of retries")

            except CloudflareBlockedError as error:
                # Not something we auto-solve. Fail this chapter immediately
                # rather than burning through retries against a wall.
                print(f"Chapter {chapter.number}: blocked by anti-bot challenge")
                log.error(f"Cloudflare-style block on chapter {chapter.number}: {error}")
                break

            except Exception as error:
                retry += 1
                print(f"Retry {retry}/{MAX_RETRY} Chapter {chapter.number}: {error}")
                log.warning(f"Chapter {chapter.number} error: {error}")

                if retry < MAX_RETRY:
                    delay = NETWORK_ERROR_RETRY_DELAYS[
                        min(retry - 1, len(NETWORK_ERROR_RETRY_DELAYS) - 1)
                    ]
                    time.sleep(delay)

        print(f"Failed Chapter {chapter.number}")
        log.error(f"Giving up on chapter {chapter.number} after {retry} retries")
        self.failed_chapters.append(chapter.number)

        chapter.content = ""
        return chapter

    def download_range(
        self,
        chapters: list[Chapter],
        start: int,
        end: int,
        batch_size: int = DEFAULT_DOWNLOAD_BATCH,
        only_numbers: set[int] | None = None,
    ) -> list[Chapter]:
        """
        Download every chapter numbered `start..end`. If `only_numbers` is
        given, only those specific chapter numbers within the range are
        downloaded (used for "download only missing chapters" - request
        #15 - instead of always re-walking a full contiguous range).
        """
        selected = [
            chapter for chapter in chapters if start <= chapter.number <= end
        ]

        if only_numbers is not None:
            selected = [c for c in selected if c.number in only_numbers]

        # Duplicate-number guard (request #4 in the "important sites"
        # list): a parser bug or a site listing the same chapter twice
        # under different URLs shouldn't produce two downloads.
        seen_numbers = set()
        deduped = []
        for chapter in selected:
            if chapter.number in seen_numbers:
                log.warning(f"Duplicate chapter number skipped: {chapter.number}")
                continue
            seen_numbers.add(chapter.number)
            deduped.append(chapter)
        selected = deduped

        downloaded = []

        for batch_start in range(0, len(selected), batch_size):
            batch = selected[batch_start : batch_start + batch_size]

            print(f"\nDownloading batch {batch[0].number}-{batch[-1].number}")

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self.download_chapter, chapter): chapter
                    for chapter in batch
                }

                with progress_bar(total=len(batch), desc="Downloading", unit="chapter") as progress:
                    for future in as_completed(futures):
                        chapter = future.result()

                        if chapter.content:
                            downloaded.append(chapter)

                        progress.update(1)

        downloaded.sort(key=lambda chapter: chapter.number)

        if self.failed_chapters:
            print(f"\nFailed chapters: {sorted(set(self.failed_chapters))}")

        if self.broken_chapters:
            print(f"Possibly broken (very short) chapters: {sorted(set(self.broken_chapters))}")

        return downloaded

    @staticmethod
    def release_memory(*containers) -> None:
        """
        Explicitly drop references and force a GC pass after each PDF is
        written, so downloading a 1000+ chapter novel doesn't keep every
        chapter's text resident in RAM for the whole run (request #10).
        """
        for container in containers:
            try:
                container.clear()
            except AttributeError:
                pass
        gc.collect()
