"""
Universal Parser: best-effort fallback for any site that doesn't have a
dedicated parser (request "Parser هوشمند" / ParserFactory auto-detect ->
Universal Parser).

It cannot know a new site's exact markup, so it uses generic heuristics:
  - Chapter links: any <a href> whose URL contains "chapter" followed by a
    number.
  - Chapter list pagination: follows ?page=2, ?page=3, ... until a page
    yields no new chapter links (handles sites that paginate their chapter
    index without exposing a "total pages" counter anywhere).
  - Chapter content: tries the same common container class names the
    site-specific parsers use, then falls back to "the element on the page
    with the most paragraph text", which is a decent generic heuristic for
    article/reader-style pages.

This will not work perfectly on every site - sites that render chapter
content via JavaScript, or that use very unusual markup, will need a
dedicated parser (see parsers/freewebnovel.py for the pattern to copy).
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.utils import slugify
from models import Chapter, Novel
from parsers.base import BaseParser

MAX_PAGINATION_PAGES = 50


class UniversalParser(BaseParser):

    def __init__(self, base_url: str):
        self.base_url = base_url

    @property
    def name(self):
        return "Universal (experimental)"

    def get_novel(self, url):
        html = self.fetch(url)

        if not html:
            raise RuntimeError("Cannot load novel page")

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Novel"

        cover_tag = soup.find("meta", property="og:image")
        cover_url = cover_tag["content"] if cover_tag and cover_tag.get("content") else None

        return Novel(
            title=title,
            url=url,
            slug=slugify(url.rstrip("/").split("/")[-1]) or slugify(title),
            site_name="Universal Parser",
            cover_url=cover_url,
        )

    def get_chapters(self, novel):
        chapters = []
        exists = set()
        seen_numbers = set()

        page = 1
        empty_pages = 0

        while page <= MAX_PAGINATION_PAGES and empty_pages < 2:
            url = novel.url if page == 1 else self._page_url(novel.url, page)
            html = self.fetch(url)

            if not html:
                empty_pages += 1
                page += 1
                continue

            soup = BeautifulSoup(html, "html.parser")
            found_this_page = 0

            for link in soup.find_all("a", href=True):
                href = link["href"]

                match = re.search(r"chapter[-_ /]?(\d+)", href.lower())
                if not match:
                    continue

                number = int(match.group(1))
                full_url = urljoin(self.base_url, href)

                if full_url in exists:
                    continue
                exists.add(full_url)

                if number in seen_numbers:
                    continue
                seen_numbers.add(number)

                title = link.get_text(strip=True) or f"Chapter {number}"
                chapters.append(Chapter(number=number, title=title, url=full_url))
                found_this_page += 1

            empty_pages = empty_pages + 1 if found_this_page == 0 else 0
            page += 1

        chapters.sort(key=lambda x: x.number)
        novel.chapters = chapters

        return chapters

    def get_chapter_content(self, chapter):
        html = self.fetch_raw(chapter.url)

        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")

        candidates = [
            ("div", "chapter-content"), ("div", "txt"), ("div", "content"),
            ("div", "chr-c"), ("div", "reading-content"), ("div", "chapter-c"),
            ("div", "entry-content"), ("div", "post-content"), ("article", None),
        ]

        content = None
        for tag, cls in candidates:
            content = soup.find(tag, class_=cls) if cls else soup.find(tag)
            if content:
                break

        if not content:
            content = self._largest_text_block(soup)

        if not content:
            return ""

        paragraph_tags = content.find_all("p")
        substantial_tags = [
            tag for tag in paragraph_tags if len(tag.get_text(strip=True)) > 20
        ]

        if len(substantial_tags) >= 3:
            paragraphs = [tag.get_text(" ", strip=True) for tag in substantial_tags]
        else:
            # Same fix as FreeWebNovelParser: a doubled <br><br> is a real
            # paragraph break, but a single <br> is usually just a soft
            # line-wrap (e.g. a dialogue line split from its attribution)
            # - merge single breaks into flowing prose instead of starting
            # a new choppy paragraph for every one of them.
            html_str = str(content)
            html_str = re.sub(r"(<br\s*/?>\s*){2,}", "\u2029", html_str, flags=re.IGNORECASE)
            html_str = re.sub(r"<br\s*/?>", " ", html_str, flags=re.IGNORECASE)

            merged_soup = BeautifulSoup(html_str, "html.parser")
            raw_text = merged_soup.get_text()

            paragraphs = [
                re.sub(r"\s+", " ", piece).strip() for piece in raw_text.split("\u2029")
            ]
            paragraphs = [p for p in paragraphs if len(p) > 20]

        return "\n\n".join(paragraphs)

    @staticmethod
    def _largest_text_block(soup):
        """
        Generic heuristic: the container with the most cumulative <p> text
        is very likely the article/chapter body, on almost any reader-style
        page layout.
        """
        best_container = None
        best_length = 0

        for container in soup.find_all(["div", "article", "section"]):
            text_length = sum(
                len(p.get_text(strip=True)) for p in container.find_all("p", recursive=False)
            )
            if text_length > best_length:
                best_length = text_length
                best_container = container

        return best_container if best_length > 200 else None

    @staticmethod
    def _page_url(base_url: str, page: int) -> str:
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}page={page}"
