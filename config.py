"""
Global project configuration.
"""

import os
from pathlib import Path

# -------------------------------------------------
# Project Information
# -------------------------------------------------

APP_NAME = "NovelDownloader"
VERSION = "3.0.0"

# -------------------------------------------------
# Base Directories
# -------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent

DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = PROJECT_DIR / "cache"
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

for _directory in (DATA_DIR, CACHE_DIR, OUTPUT_DIR, LOG_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------
# Android Storage
# -------------------------------------------------
# On Termux, ANDROID_STORAGE points at shared storage so PDFs are visible
# to other apps. On desktop/CI this simply falls back gracefully because we
# never require it to exist unless the user picks "Android storage" output.

ANDROID_STORAGE = Path.home() / "storage" / "shared" / "NovelDownloader"

try:
    ANDROID_STORAGE.mkdir(parents=True, exist_ok=True)
    ANDROID_STORAGE_AVAILABLE = True
except OSError:
    # Not running on Termux / no shared storage symlink - that's fine.
    ANDROID_STORAGE_AVAILABLE = False

# -------------------------------------------------
# Network
# -------------------------------------------------

# A small pool of real-world desktop user agents. Rotated per-request so a
# long download doesn't look like a single obviously-scripted client.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 "
    "Firefox/127.0",
]

USER_AGENT = USER_AGENTS[0]

REQUEST_TIMEOUT = 30

MAX_RETRIES = 5

# Delay ladder (seconds) used when a site returns HTTP 429 (Too Many
# Requests). Index 0 is the wait before the 1st retry, etc.
RATE_LIMIT_RETRY_DELAYS = [5, 15, 30, 60, 120]

# Delay ladder used for transient network errors (timeouts, connection
# resets, 5xx server errors) - these usually recover faster than a 429.
NETWORK_ERROR_RETRY_DELAYS = [2, 4, 8, 16, 30]

# Proactive rate limiting: no more than RATE_LIMIT_REQUESTS requests in any
# rolling RATE_LIMIT_WINDOW_SECONDS window, regardless of worker count.
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 20

# Optional HTTP/HTTPS/SOCKS proxy, e.g. "http://127.0.0.1:8080" or
# "socks5://127.0.0.1:1080". Useful when a novel site is blocked/filtered
# in the user's region and they already run a local proxy/VPN client.
# Set via environment variable so nothing sensitive lives in source code:
#   export NOVEL_DOWNLOADER_PROXY="socks5://127.0.0.1:1080"
PROXY_URL = os.environ.get("NOVEL_DOWNLOADER_PROXY", "").strip() or None

# -------------------------------------------------
# Download
# -------------------------------------------------

DEFAULT_START_CHAPTER = 1
DEFAULT_DOWNLOAD_BATCH = 10
DEFAULT_CHAPTERS_PER_PDF = 50
DEFAULT_MAX_WORKERS = 3

DOWNLOAD_DELAY = 0.2

# A chapter whose extracted text is shorter than this is very likely a
# parsing failure (paywall, "chapter not found" page, JS-only content,
# etc.) rather than a genuinely short chapter. It is still saved, but
# flagged to the user as "possibly broken".
MIN_CHAPTER_LENGTH = 200

# Chapters whose parsed content is *shorter* than this are rejected outright
# and treated as a failed download (worth retrying), since it's almost
# certainly an error page rather than real content.
MIN_VALID_CHAPTER_LENGTH = 30

# Safety cap when following "next page" links inside a single multi-page
# chapter, so a bad pagination loop can never hang the downloader forever.
MAX_CHAPTER_SUBPAGES = 20

# -------------------------------------------------
# PDF
# -------------------------------------------------

PDF_FONT_NAME = "Helvetica"
PDF_FONT_SIZE = 11
PDF_TITLE_SIZE = 18
PDF_MARGIN = 40

# -------------------------------------------------
# Fonts (for Persian/RTL PDF rendering)
# -------------------------------------------------

FONTS_DIR = PROJECT_DIR / "fonts"
FONTS_DIR.mkdir(parents=True, exist_ok=True)

# Place a Persian-capable TTF here for correctly-rendered Persian PDFs -
# reportlab's built-in fonts (Helvetica/Times) have no Persian/Arabic
# glyphs at all. Recommended: Vazirmatn (free, open source, SIL license) -
# download the "Regular" TTF from
# https://github.com/rastikerdar/vazirmatn/releases/latest
# (inside the release zip: fonts/ttf/Vazirmatn-Regular.ttf) and save it at
# exactly this path. If it's missing, Persian PDFs still get created but
# Persian text won't display correctly.
PERSIAN_FONT_FILE = FONTS_DIR / "Vazirmatn-Regular.ttf"
PERSIAN_FONT_NAME = "Vazirmatn"

# -------------------------------------------------
# Translation
# -------------------------------------------------

TRANSLATION_TARGET_LANG = "fa"

# -------------------------------------------------
# Search (sitemap-based, since site search UIs are JS/AJAX driven and
# can't be reliably scraped as static HTML)
# -------------------------------------------------

SITEMAP_CACHE_TTL_HOURS = 6

# -------------------------------------------------
# Supported Websites
# -------------------------------------------------
# Maps a domain suffix to "module.ClassName". Parsers are imported lazily
# by the factory, so adding a new site is just: write parsers/mysite.py
# with a BaseParser subclass, then add one line here. No other file needs
# to change - this is the project's lightweight "plugin" mechanism.

SUPPORTED_SITES = {
    "freewebnovel.com": "parsers.freewebnovel.FreeWebNovelParser",
}
