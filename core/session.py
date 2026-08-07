"""
HTTP session management: connection pooling, proxy support, header
rotation, a proactive rate limiter, and typed errors so callers can tell a
"too many requests" response apart from a network hiccup or a Cloudflare
challenge page and react accordingly.
"""

import random
import threading
import time
from collections import deque

import requests

from config import (
    PROXY_URL,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    REQUEST_TIMEOUT,
    USER_AGENTS,
)
from core.logger import get_logger

log = get_logger(__name__)


class FetchError(Exception):
    """Base class for all fetch failures."""


class RateLimitedError(FetchError):
    """Server responded 429 Too Many Requests."""

    def __init__(self, retry_after: float | None = None):
        super().__init__("Rate limited (HTTP 429)")
        self.retry_after = retry_after


class ServerError(FetchError):
    """Server responded with a 5xx status."""

    def __init__(self, status_code: int):
        super().__init__(f"Server error (HTTP {status_code})")
        self.status_code = status_code


class CloudflareBlockedError(FetchError):
    """
    Page looks like a Cloudflare (or similar) anti-bot challenge page
    rather than real content. We deliberately do NOT attempt to solve or
    bypass such challenges - that's out of scope here. We just detect it
    so the downloader can fail fast with a clear message instead of
    silently saving a "Just a moment..." page as a chapter.
    """


class NetworkError(FetchError):
    """Timeout, connection reset, DNS failure, TLS error, etc."""


_CLOUDFLARE_MARKERS = (
    "just a moment",
    "checking your browser before accessing",
    "cf-browser-verification",
    "cf-chl-",
)


class RateLimiter:
    """
    Thread-safe sliding-window rate limiter shared by every worker thread,
    so no more than `max_requests` calls happen in any rolling
    `window_seconds` period regardless of how many parallel workers are
    downloading chapters at once.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()

                while (
                    self._timestamps
                    and now - self._timestamps[0] > self.window_seconds
                ):
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return

                wait_time = self.window_seconds - (now - self._timestamps[0])

            time.sleep(max(wait_time, 0.05))


# One shared limiter for the whole process.
default_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def create_session() -> requests.Session:
    """
    Create a configured HTTP session (with proxy support if configured).
    """
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    if PROXY_URL:
        session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})

    return session


def _looks_like_cloudflare_challenge(html: str) -> bool:
    lowered = html[:4000].lower()
    return any(marker in lowered for marker in _CLOUDFLARE_MARKERS)


def download_binary(
    url: str,
    session: requests.Session | None = None,
    rate_limiter: RateLimiter | None = None,
    max_retries: int = 3,
) -> bytes | None:
    """
    Download raw bytes (used for cover images). Best-effort: returns None
    on failure after a few quick retries rather than raising, since a
    missing cover should never break PDF creation.
    """
    owns_session = session is None
    if session is None:
        session = create_session()

    if rate_limiter is None:
        rate_limiter = default_rate_limiter

    try:
        for attempt in range(max_retries):
            rate_limiter.acquire()

            try:
                response = session.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                return response.content
            except requests.RequestException as error:
                log.warning(f"Cover image download attempt {attempt + 1} failed: {error}")
                if attempt < max_retries - 1:
                    time.sleep(2)

        return None
    finally:
        if owns_session:
            session.close()


def fetch_page(
    url: str,
    session: requests.Session | None = None,
    rate_limiter: RateLimiter | None = None,
    rotate_headers: bool = True,
) -> str:
    """
    Download a webpage and return its HTML.

    Raises RateLimitedError, ServerError, CloudflareBlockedError, or
    NetworkError on failure instead of silently returning None, so the
    caller can pick the right retry/backoff strategy.
    """
    owns_session = session is None
    if session is None:
        session = create_session()

    if rate_limiter is None:
        rate_limiter = default_rate_limiter

    if rotate_headers:
        session.headers["User-Agent"] = random.choice(USER_AGENTS)

    rate_limiter.acquire()

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            raise RateLimitedError(retry_after)

        if response.status_code in (500, 502, 503, 504):
            raise ServerError(response.status_code)

        response.raise_for_status()

        html = response.text

        if _looks_like_cloudflare_challenge(html):
            raise CloudflareBlockedError(
                f"Anti-bot challenge page detected at {url}"
            )

        return html

    except (RateLimitedError, ServerError, CloudflareBlockedError):
        raise
    except (requests.Timeout, requests.ConnectionError) as error:
        raise NetworkError(str(error)) from error
    except requests.RequestException as error:
        raise NetworkError(str(error)) from error
    finally:
        if owns_session:
            session.close()
