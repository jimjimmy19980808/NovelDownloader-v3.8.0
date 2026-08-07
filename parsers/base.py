"""
Base parser interface.
"""

import time
from abc import ABC, abstractmethod

from config import NETWORK_ERROR_RETRY_DELAYS, RATE_LIMIT_RETRY_DELAYS
from core.logger import get_logger
from core.session import CloudflareBlockedError, NetworkError, RateLimitedError, ServerError, fetch_page
from models import Chapter, Novel

log = get_logger(__name__)


class BaseParser(ABC):
    """
    Abstract base class for all novel parsers.

    Provides `fetch()`, a retrying wrapper around core.session.fetch_page,
    so every parser (site-specific or the Universal fallback) gets the same
    429/5xx/network-error backoff behavior for free, instead of each parser
    reimplementing retry logic.
    """

    MAX_PAGE_RETRIES = 4

    def fetch_raw(self, url: str) -> str:
        """
        Single-attempt fetch that lets typed errors (RateLimitedError,
        ServerError, NetworkError, CloudflareBlockedError) propagate
        instead of swallowing them. Used for per-chapter content fetches,
        where ChapterDownloader already owns the retry/backoff loop -
        retrying here too would double every backoff.
        """
        return fetch_page(url)

    def fetch(self, url: str) -> str | None:
        """
        Fetch a page's HTML with retries. Returns None (rather than
        raising) only after all retries are exhausted, so callers can keep
        their existing "if not html: skip/return" logic.
        """
        retry = 0

        while retry <= self.MAX_PAGE_RETRIES:
            try:
                return fetch_page(url)

            except RateLimitedError as error:
                delay = error.retry_after or RATE_LIMIT_RETRY_DELAYS[
                    min(retry, len(RATE_LIMIT_RETRY_DELAYS) - 1)
                ]
                log.warning(f"Rate limited fetching {url}, waiting {delay}s")
                time.sleep(delay)

            except (ServerError, NetworkError) as error:
                delay = NETWORK_ERROR_RETRY_DELAYS[
                    min(retry, len(NETWORK_ERROR_RETRY_DELAYS) - 1)
                ]
                log.warning(f"{error} fetching {url}, waiting {delay}s")
                time.sleep(delay)

            except CloudflareBlockedError as error:
                log.error(f"Blocked by anti-bot challenge at {url}: {error}")
                print(
                    "This site returned an anti-bot challenge page instead "
                    "of real content. Automatic bypass is not supported; "
                    "try again later, or use a different network/proxy."
                )
                return None

            retry += 1

        log.error(f"Giving up fetching {url} after {retry} retries")
        return None

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Parser name.
        """

    @abstractmethod
    def get_novel(self, url: str) -> Novel:
        """
        Return novel information.
        """

    @abstractmethod
    def get_chapters(self, novel: Novel) -> list[Chapter]:
        """
        Return all chapter links.
        """

    @abstractmethod
    def get_chapter_content(self, chapter: Chapter) -> str:
        """
        Download chapter content.
        """
