"""
FreeWebNovel parser.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import DATA_DIR, MAX_CHAPTER_SUBPAGES, SITEMAP_CACHE_TTL_HOURS
from core.logger import get_logger
from core.utils import load_json, save_json, slugify
from models import Chapter, Novel
from parsers.base import BaseParser

log = get_logger(__name__)

SITEMAP_CACHE_FILE = DATA_DIR / "freewebnovel_sitemap_cache.json"


class FreeWebNovelParser(BaseParser):

    BASE_URL = "https://freewebnovel.com"
    SITEMAP_URL = "https://freewebnovel.com/sitemap.xml"

    @property
    def name(self):
        return "FreeWebNovel"

    def search(self, query: str, limit: int = 20) -> list[tuple[str, str]]:
        """
        Search novels by name.

        FreeWebNovel's own /search page loads results via JavaScript/AJAX,
        so it can't be scraped as static HTML. Instead, this uses the
        site's public sitemap.xml (which lists every novel's URL) as a
        local, offline-searchable index - fetched once and cached on disk
        for SITEMAP_CACHE_TTL_HOURS so repeated searches don't re-download
        it every time.

        Returns a list of (title_guess, url) tuples. The title is guessed
        from the URL slug (e.g. "martial-peak" -> "Martial Peak") since the
        sitemap doesn't include real titles - the exact title is fetched
        for real once the user picks a result and get_novel() runs.
        """
        urls = self._load_sitemap_urls()

        query_words = [w for w in re.split(r"\s+", query.lower().strip()) if w]

        if not query_words:
            return []

        results = []

        for url in urls:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            haystack = slug.replace("-", " ")

            if all(word in haystack for word in query_words):
                title_guess = haystack.title()
                results.append((title_guess, url))

        return results[:limit]

    def _load_sitemap_urls(self) -> list[str]:
        cached = load_json(SITEMAP_CACHE_FILE, default=None)

        if cached:
            fetched_at = datetime.fromisoformat(cached.get("fetched_at", "2000-01-01T00:00:00"))
            if datetime.now() - fetched_at < timedelta(hours=SITEMAP_CACHE_TTL_HOURS):
                return cached.get("urls", [])

        html = self.fetch(self.SITEMAP_URL)

        if not html:
            # Fall back to a stale cache rather than nothing, if we have one.
            if cached:
                log.warning("Sitemap fetch failed - using stale cached novel list")
                return cached.get("urls", [])
            return []

        urls = re.findall(r"<loc>\s*(https?://freewebnovel\.com/novel/[^<\s]+)\s*</loc>", html)
        urls = sorted(set(urls))

        save_json(
            SITEMAP_CACHE_FILE,
            {"fetched_at": datetime.now().isoformat(timespec="seconds"), "urls": urls},
        )

        return urls

    def get_novel(self, url):
        html = self.fetch(url)

        if not html:
            raise RuntimeError("Cannot load novel page")

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Novel"

        cover_tag = soup.find("meta", property="og:image")
        cover_url = cover_tag["content"] if cover_tag and cover_tag.get("content") else None

        is_completed = None
        status_tag = soup.find(string=re.compile(r"\bCompleted\b", re.IGNORECASE))
        if status_tag:
            is_completed = True

        author = self._extract_author(soup)

        return Novel(
            title=title,
            url=url,
            slug=slugify(url.rstrip("/").split("/")[-1]),
            site_name=self.name,
            author=author,
            cover_url=cover_url,
            is_completed=is_completed,
        )

    @staticmethod
    def _extract_author(soup: BeautifulSoup) -> str:
        """
        Best-effort author extraction. Tries a few common patterns used by
        novel-listing sites, falling back to "Unknown Author" rather than
        raising - a missing author should never block downloading.
        """
        # Pattern 1: a dedicated author link (common: /author/slug)
        author_link = soup.find("a", href=re.compile(r"/author/", re.IGNORECASE))
        if author_link and author_link.get_text(strip=True):
            return author_link.get_text(strip=True)

        # Pattern 2: a label ("Author:") followed by the value in the same
        # or a sibling element (common table/definition-list style info box).
        label = soup.find(string=re.compile(r"^\s*Author\s*:?\s*$", re.IGNORECASE))
        if label:
            container = label.find_parent()
            if container:
                sibling = container.find_next_sibling()
                if sibling and sibling.get_text(strip=True):
                    return sibling.get_text(strip=True)

                # Sometimes label and value share the same parent, e.g.
                # <div>Author: <a>Name</a></div>
                link_in_container = container.find("a")
                if link_in_container and link_in_container.get_text(strip=True):
                    return link_in_container.get_text(strip=True)

        # Pattern 3: standard <meta name="author">
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            return meta_author["content"].strip()

        return "Unknown Author"

    def get_chapters(self, novel):
        chapters = []
        exists = set()
        seen_numbers = set()

        first_html = self.fetch(novel.url)

        if not first_html:
            return chapters

        soup = BeautifulSoup(first_html, "html.parser")

        total_page = 1
        page_box = soup.find("div", id="indexListPage")

        if page_box:
            try:
                total_page = int(page_box.get("data-total-page", 1))
            except (TypeError, ValueError):
                total_page = 1

        print(f"Chapter pages: {total_page}")

        for page in range(1, total_page + 1):
            if page == 1:
                html = first_html
            else:
                html = self.fetch(f"{novel.url}?page={page}")

            if not html:
                print(f"Warning: could not load chapter list page {page}, skipping")
                continue

            soup = BeautifulSoup(html, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link["href"]

                if "chapter" not in href.lower():
                    continue

                match = re.search(r"chapter[-_ ]?(\d+)", href.lower())

                if not match:
                    continue

                number = int(match.group(1))
                full_url = urljoin(self.BASE_URL, href)

                if full_url in exists:
                    continue
                exists.add(full_url)

                if number in seen_numbers:
                    # Same chapter number reached via a different URL - the
                    # site is listing a duplicate; keep only the first.
                    continue
                seen_numbers.add(number)

                chapters.append(Chapter(number=number, title=f"Chapter {number}", url=full_url))

        chapters.sort(key=lambda x: x.number)

        novel.chapters = chapters

        return chapters

    def get_chapter_content(self, chapter):
        html = self.fetch_raw(chapter.url)

        if not html:
            return ""

        paragraphs = self._extract_paragraphs(html, chapter.url)

        if not paragraphs:
            return ""

        # Some chapters are split across multiple pages
        # (freewebnovel.com/.../chapter-50_2 style, or ?page=2). Follow a
        # "next" link that stays within the same chapter if present.
        next_url = self._find_chapter_next_page(html, chapter.url)
        visited = {chapter.url}
        hops = 0

        while next_url and next_url not in visited and hops < MAX_CHAPTER_SUBPAGES:
            visited.add(next_url)
            hops += 1

            more_html = self.fetch_raw(next_url)
            if not more_html:
                break

            more_paragraphs = self._extract_paragraphs(more_html, next_url)
            if not more_paragraphs:
                break

            paragraphs.extend(more_paragraphs)
            next_url = self._find_chapter_next_page(more_html, next_url)

        return "\n\n".join(paragraphs)

    @staticmethod
    def _extract_paragraphs(html: str, source_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")

        content = None
        selectors = [
            ("div", "chapter-content"),
            ("div", "txt"),
            ("div", "content"),
            ("div", "chr-c"),
            ("div", "reading-content"),
            ("div", "chapter-c"),
            ("article", None),
        ]

        for tag, cls in selectors:
            content = soup.find(tag, class_=cls) if cls else soup.find(tag)
            if content:
                break

        if not content:
            print(f"No content: {source_url}")
            return []

        paragraph_tags = content.find_all("p")
        substantial_tags = [
            tag for tag in paragraph_tags if len(tag.get_text(strip=True)) > 20
        ]

        if len(substantial_tags) >= 3:
            # The site DOES use real <p> tags per paragraph - use them directly.
            return [tag.get_text(" ", strip=True) for tag in substantial_tags]

        # Fall back to <br>-based splitting. Crucially, a <br><br> (or
        # <br> with only whitespace between) is a REAL paragraph break,
        # but a single <br> is usually just a soft line-wrap - e.g. a
        # dialogue line split from its "... he said" attribution on the
        # site. Treating every single <br> as a new paragraph (the
        # previous behavior) produced a choppy, disjointed-looking PDF
        # with one sentence fragment per "paragraph". So: collapse
        # doubled <br> into a paragraph-boundary marker FIRST, then turn
        # any remaining single <br> into a plain space so it merges back
        # into flowing prose instead of starting a new block.
        html_str = str(content)
        html_str = re.sub(r"(<br\s*/?>\s*){2,}", "\u2029", html_str, flags=re.IGNORECASE)
        html_str = re.sub(r"<br\s*/?>", " ", html_str, flags=re.IGNORECASE)

        merged_soup = BeautifulSoup(html_str, "html.parser")
        raw_text = merged_soup.get_text()

        paragraphs = [
            re.sub(r"\s+", " ", piece).strip()
            for piece in raw_text.split("\u2029")
        ]

        return [p for p in paragraphs if len(p) > 20]

    @staticmethod
    def _find_chapter_next_page(html: str, current_url: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")

        # Prefer an explicit rel="next" link if the site provides one.
        rel_next = soup.find("a", rel="next", href=True)
        if rel_next:
            return urljoin(current_url, rel_next["href"])

        # Otherwise look for a link whose visible text is just "Next"/">>"
        # AND whose URL looks like a same-chapter continuation
        # (…-2.html, …_2, ?page=2) rather than the *next chapter* entirely -
        # we only want to combine true sub-pages of one chapter, never
        # accidentally jump chapters.
        current_base = current_url.split("?")[0].rsplit("_", 1)[0].rsplit("-", 1)[0]

        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True).lower()
            if text not in ("next", "next page", ">>", "»"):
                continue

            href = urljoin(current_url, link["href"])
            href_base = href.split("?")[0].rsplit("_", 1)[0].rsplit("-", 1)[0]

            looks_paginated = bool(re.search(r"([_-]\d+(\.html)?/?$|[?&]page=\d+$)", href))

            if looks_paginated and href_base == current_base:
                return href

        return None
