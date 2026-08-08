"""
PDF creation module.

Produces a "real book" layout for every PDF part:
  page 1 - cover image
  page 2 - novel info (title, author, site, status, chapter range, etc.)
  page 3 - table of contents for the chapters in THIS PDF (title + page #)
  page 4+ - chapters, justified book-style typography, page numbers in
            the footer of every page.

Also supports an optional Persian ("fa") layout: right-to-left alignment,
reshaped Arabic-script text (see core/rtl.py), a Persian font if one is
installed (see config.PERSIAN_FONT_FILE), and Persian labels for the
fixed page headings (Title/Author/Status/Table of Contents/etc).
"""

import io
import re
from datetime import datetime
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image as RLImage
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config import (
    ANDROID_STORAGE,
    ANDROID_STORAGE_AVAILABLE,
    DEFAULT_CHAPTERS_PER_PDF,
    OUTPUT_DIR,
    PERSIAN_FONT_FILE,
    PERSIAN_FONT_NAME,
)
from core.logger import get_logger
from core.rtl import shape_rtl
from core.session import download_binary
from core.utils import progress_bar, safe_filename

log = get_logger(__name__)

# Standard cover box on the PDF's opening page. Sized to fill nearly the
# whole printable page area (A4 minus the document's 20mm margins on each
# side, minus reportlab's own default ~6pt frame padding) rather than a
# small centered thumbnail, while still preserving the source image's own
# aspect ratio (never stretched).
COVER_MAX_WIDTH_MM = 160
COVER_MAX_HEIGHT_MM = 248

# Covers are re-encoded to this max pixel width before embedding, so a
# huge source image doesn't bloat every single PDF part with a copy of
# a multi-megabyte photo - this keeps output "standard quality" and
# consistent regardless of what the source site served.
COVER_MAX_PIXEL_WIDTH = 900
COVER_JPEG_QUALITY = 85

# Fixed page-scaffold text (headings/labels), in English and Persian.
LABELS = {
    "en": {
        "title": "Title:",
        "author": "Author:",
        "source": "Source:",
        "status": "Status:",
        "chapters_on_site": "Chapters available on site:",
        "chapters_in_file": "Chapters in this file:",
        "processed_with": "Translated/proofread with:",
        "generated": "Generated:",
        "toc_heading": "Table of Contents",
        "chapter": "Chapter",
        "completed": "Completed",
        "ongoing": "Ongoing",
        "unknown": "Unknown",
    },
    "fa": {
        "title": "عنوان:",
        "author": "نویسنده:",
        "source": "منبع:",
        "status": "وضعیت:",
        "chapters_on_site": "تعداد فصل موجود در سایت:",
        "chapters_in_file": "فصل‌های این فایل:",
        "processed_with": "ترجمه/ویرایش با:",
        "generated": "تاریخ ساخت:",
        "toc_heading": "فهرست مطالب",
        "chapter": "فصل",
        "completed": "تکمیل شده",
        "ongoing": "در حال انتشار",
        "unknown": "نامشخص",
    },
}

_persian_font_ready = None  # tri-state: None = not checked yet


def _ensure_persian_font() -> str:
    """
    Registers the Persian TTF (if present at config.PERSIAN_FONT_FILE) with
    reportlab. Returns the font name to use for Persian text - the real
    Persian font if available, otherwise falls back to Helvetica (which has
    NO Persian glyphs, so text will render as boxes/blanks - a warning is
    logged so this is never a silent failure).
    """
    global _persian_font_ready

    if _persian_font_ready is not None:
        return PERSIAN_FONT_NAME if _persian_font_ready else "Helvetica"

    if not PERSIAN_FONT_FILE.exists():
        log.warning(
            f"Persian font not found at {PERSIAN_FONT_FILE} - Persian PDFs "
            "will use a fallback font and Persian text will NOT display "
            "correctly. Download a Persian TTF (e.g. Vazirmatn) and save "
            "it at that exact path."
        )
        _persian_font_ready = False
        return "Helvetica"

    try:
        pdfmetrics.registerFont(TTFont(PERSIAN_FONT_NAME, str(PERSIAN_FONT_FILE)))
        pdfmetrics.registerFont(TTFont(f"{PERSIAN_FONT_NAME}-Bold", str(PERSIAN_FONT_FILE)))
        _persian_font_ready = True
        return PERSIAN_FONT_NAME
    except Exception as error:
        log.warning(f"Could not register Persian font: {error}")
        _persian_font_ready = False
        return "Helvetica"


class NumberedCanvas(Canvas):
    """
    Draws "page N / total" centered in the footer of every page. Standard
    reportlab two-pass-per-page recipe: pages are buffered as they're
    drawn, then re-visited once the true page count is known (at save()
    time) to stamp each one with "N / total" instead of just "N".
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)

        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(total_pages)
            super().showPage()

        super().save()

    def _draw_page_number(self, total_pages):
        # No footer on the cover page - it's a full-bleed image page.
        if self._pageNumber == 1:
            return

        self.setFont("Helvetica", 9)
        self.drawCentredString(
            A4[0] / 2, 12 * mm, f"{self._pageNumber} / {total_pages}"
        )


class NovelDocTemplate(SimpleDocTemplate):
    """
    A SimpleDocTemplate that records which page each chapter heading lands
    on, so a table of contents can be rendered with real page numbers.
    Flowables built by PDFMaker tag themselves with `_chapter_number` (see
    _chapter_heading()) so afterFlowable() can pick them up automatically.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chapter_page_numbers: dict[int, int] = {}

    def afterFlowable(self, flowable):
        chapter_number = getattr(flowable, "_chapter_number", None)
        if chapter_number is not None:
            self.chapter_page_numbers[chapter_number] = self.page


class PDFMaker:

    def __init__(self, use_android_storage: bool = True):
        if use_android_storage and ANDROID_STORAGE_AVAILABLE:
            self.output_dir = ANDROID_STORAGE
        else:
            self.output_dir = OUTPUT_DIR

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def novel_folder(self, novel_title: str) -> Path:
        return self.output_dir / safe_filename(novel_title)

    # -----------------------------------------------------------------
    # Language-aware styles
    # -----------------------------------------------------------------

    def _build_styles(self, language: str) -> dict:
        styles = getSampleStyleSheet()
        is_persian = language == "fa"

        if is_persian:
            body_font = _ensure_persian_font()
            bold_font = f"{body_font}-Bold" if body_font == PERSIAN_FONT_NAME else body_font
            align = TA_RIGHT
        else:
            body_font = "Times-Roman"
            bold_font = "Times-Bold"
            align = TA_JUSTIFY

        return {
            "title": ParagraphStyle(
                "NovelTitle", parent=styles["Title"], fontName=bold_font,
                alignment=TA_CENTER, fontSize=20, spaceAfter=20,
            ),
            "info_label": ParagraphStyle(
                "InfoLabel", parent=styles["Normal"], fontName=bold_font,
                fontSize=11, alignment=(TA_RIGHT if is_persian else TA_LEFT),
            ),
            "info_value": ParagraphStyle(
                "InfoValue", parent=styles["Normal"], fontName=body_font,
                fontSize=11, alignment=(TA_RIGHT if is_persian else TA_LEFT),
            ),
            "toc_heading": ParagraphStyle(
                "TOCHeading", parent=styles["Heading1"], fontName=bold_font,
                alignment=TA_CENTER, fontSize=16, spaceAfter=18,
            ),
            "toc_entry": ParagraphStyle(
                "TOCEntry", parent=styles["Normal"], fontName=body_font,
                fontSize=11, alignment=(TA_RIGHT if is_persian else TA_LEFT),
            ),
            "chapter": ParagraphStyle(
                "ChapterTitle", parent=styles["Heading2"], fontName=bold_font,
                fontSize=15, spaceBefore=6, spaceAfter=16,
                alignment=(TA_RIGHT if is_persian else TA_LEFT),
            ),
            "body": ParagraphStyle(
                "ChapterBody", parent=styles["BodyText"], fontName=body_font,
                fontSize=11.5, leading=18 if is_persian else 17,
                alignment=align,
                # A true right-side first-line indent isn't supported by
                # reportlab's LTR-only paragraph model, so Persian body
                # text relies on spaceAfter between paragraphs instead of
                # an indent (English keeps the book-style indent).
                firstLineIndent=(0 if is_persian else 16),
                spaceAfter=10 if is_persian else 8,
            ),
        }

    @staticmethod
    def _text(value: str, language: str) -> str:
        """Applies RTL shaping to a piece of text when language is Persian."""
        return shape_rtl(value) if language == "fa" else value

    # -----------------------------------------------------------------
    # Cover image
    # -----------------------------------------------------------------

    def _ensure_cover_cached(self, novel) -> Path | None:
        """
        Downloads the novel's cover image once and caches it as
        cover.jpg inside the novel's output folder, resized/re-encoded to
        a standard max resolution/quality. Returns the cached path, or
        None if there's no cover URL or the download/processing failed
        (never raises - a missing cover should never break PDF creation).
        """
        if not novel or not getattr(novel, "cover_url", None):
            return None

        folder = self.novel_folder(novel.title)
        folder.mkdir(parents=True, exist_ok=True)
        cover_path = folder / "cover.jpg"

        if cover_path.exists():
            return cover_path

        raw = download_binary(novel.cover_url)

        if not raw:
            log.warning(f"Could not download cover image for {novel.title}")
            return None

        try:
            from PIL import Image

            image = Image.open(io.BytesIO(raw))
            image = image.convert("RGB")

            if image.width > COVER_MAX_PIXEL_WIDTH:
                ratio = COVER_MAX_PIXEL_WIDTH / image.width
                image = image.resize(
                    (COVER_MAX_PIXEL_WIDTH, int(image.height * ratio)),
                    Image.LANCZOS,
                )

            image.save(cover_path, "JPEG", quality=COVER_JPEG_QUALITY)
            return cover_path

        except ImportError:
            log.warning("Pillow not installed - saving cover image unprocessed")
            cover_path.write_bytes(raw)
            return cover_path
        except Exception as error:
            log.warning(f"Could not process cover image for {novel.title}: {error}")
            return None

    def _cover_flowable(self, cover_path: Path):
        """
        Returns a reportlab Image flowable sized to fit within the
        standard cover box, preserving aspect ratio (never stretched).
        """
        try:
            from PIL import Image as PILImage

            with PILImage.open(cover_path) as image:
                width_px, height_px = image.size
        except Exception:
            width_px, height_px = COVER_MAX_WIDTH_MM, COVER_MAX_HEIGHT_MM

        max_width = COVER_MAX_WIDTH_MM * mm
        max_height = COVER_MAX_HEIGHT_MM * mm

        scale = min(max_width / width_px, max_height / height_px)
        display_width = width_px * scale
        display_height = height_px * scale

        return RLImage(str(cover_path), width=display_width, height=display_height)

    # -----------------------------------------------------------------
    # Info page (page 2)
    # -----------------------------------------------------------------

    def _info_page_flowables(
        self, display_title, novel, chapters, total_known_chapters, language, styles, chapter_backend_labels=None
    ):
        labels = LABELS[language]
        t = lambda s: self._text(s, language)  # noqa: E731
        rows = []

        def add_row(label, value):
            rows.append(
                [
                    Paragraph(t(label), styles["info_label"]),
                    Paragraph(t(str(value)) if value else t("-"), styles["info_value"]),
                ]
            )

        add_row(labels["title"], display_title)
        add_row(labels["author"], getattr(novel, "author", None) if novel else None)
        add_row(labels["source"], getattr(novel, "site_name", None) if novel else None)

        status = labels["unknown"]
        if novel is not None:
            if novel.is_completed is True:
                status = labels["completed"]
            elif novel.is_completed is False:
                status = labels["ongoing"]
        add_row(labels["status"], status)

        if total_known_chapters:
            add_row(labels["chapters_on_site"], total_known_chapters)

        chapter_range = f"{chapters[0].number} - {chapters[-1].number}  ({len(chapters)})"
        add_row(labels["chapters_in_file"], chapter_range)

        if chapter_backend_labels:
            # Summarize which backend actually produced each chapter's text
            # (e.g. "Gemini (18 chapters), Gemini, partially fell back to
            # Google Translate (2 chapters)") - so a silent mid-run fallback
            # from your configured AI backend to plain Google Translate is
            # always visible in the PDF itself, not hidden.
            from collections import Counter

            counts = Counter(
                chapter_backend_labels.get(c.number, "-") for c in chapters
            )
            summary = ", ".join(f"{label} ({count})" for label, count in counts.most_common())
            add_row(labels["processed_with"], summary)

        add_row(labels["generated"], datetime.now().strftime("%Y-%m-%d %H:%M"))

        # Wide, generously-padded rows so the table reads as a real page of
        # content rather than a small block of text huddled at the top of
        # an otherwise-empty page.
        col_widths = [55 * mm, 115 * mm]
        if language == "fa":
            rows = [[value, label] for label, value in rows]  # label column on the right visually
            col_widths = [115 * mm, 55 * mm]

        table = Table(rows, colWidths=col_widths)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
                    ("TOPPADDING", (0, 0), (-1, -1), 16),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.75, "#bbbbbb"),
                    ("LINEABOVE", (0, 0), (-1, 0), 1, "#333333"),
                    ("LINEBELOW", (0, -1), (-1, -1), 1, "#333333"),
                ]
            )
        )

        # Vertically balance the page: push the title+table down from the
        # very top so the whole block sits roughly centered on the page
        # instead of clinging to the top margin with a large empty gap
        # below it.
        return [
            Spacer(1, 60),
            Paragraph(t(display_title), styles["title"]),
            Spacer(1, 50),
            table,
        ]

    # -----------------------------------------------------------------
    # Table of contents (page 3)
    # -----------------------------------------------------------------

    @staticmethod
    def _is_generic_title(chapter) -> bool:
        """
        True if the chapter's ORIGINAL (untranslated) title is just a
        generic "Chapter N" placeholder rather than a real, distinct
        title - which is the case for sites (like FreeWebNovel) that
        don't expose per-chapter titles at all. In that case, a
        translated version of "Chapter 51" (e.g. Gemini sometimes
        spelling it out as "فصل پنجاه و یکم") should NOT be appended
        after our own "{label} {number}:" prefix - that just duplicates
        the chapter number/word in two different forms.
        """
        return chapter.title.strip().lower() == f"chapter {chapter.number}".lower()

    def _toc_flowables(self, chapters, chapter_titles, page_numbers, language, styles):
        labels = LABELS[language]
        t = lambda s: self._text(s, language)  # noqa: E731
        rows = []

        for chapter in chapters:
            page = page_numbers.get(chapter.number, "-")

            if self._is_generic_title(chapter):
                label = t(f"{labels['chapter']} {chapter.number}")
            else:
                title = chapter_titles.get(chapter.number, chapter.title)
                label = t(f"{labels['chapter']} {chapter.number}: {title}")

            rows.append([Paragraph(label, styles["toc_entry"]), Paragraph(str(page), styles["toc_entry"])])

        col_widths = [135 * mm, 15 * mm]
        if language == "fa":
            rows = [[page_cell, title_cell] for title_cell, page_cell in rows]
            col_widths = [15 * mm, 135 * mm]

        table = Table(rows, colWidths=col_widths)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0 if language == "fa" else 1, 0), (0 if language == "fa" else 1, -1), "RIGHT" if language != "fa" else "LEFT"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, "#e0e0e0"),
                ]
            )
        )

        return [Paragraph(t(labels["toc_heading"]), styles["toc_heading"]), table]

    def _chapter_heading(self, chapter, title, language, styles):
        """
        A chapter's heading Paragraph, tagged with the chapter number so
        NovelDocTemplate.afterFlowable() can record which page it landed
        on for the table of contents.
        """
        labels = LABELS[language]

        if self._is_generic_title(chapter):
            text = self._text(f"{labels['chapter']} {chapter.number}", language)
        else:
            text = self._text(f"{labels['chapter']} {chapter.number}: {title}", language)

        heading = Paragraph(text, styles["chapter"])
        heading._chapter_number = chapter.number
        return heading

    # -----------------------------------------------------------------
    # Building
    # -----------------------------------------------------------------

    def _build_content(
        self,
        display_title,
        chapters,
        novel,
        total_known_chapters,
        page_numbers,
        language,
        chapter_titles,
        chapter_bodies,
        styles,
        chapter_backend_labels=None,
    ):
        content = []

        cover_path = self._ensure_cover_cached(novel) if novel else None

        if cover_path and cover_path.exists():
            content.append(self._cover_flowable(cover_path))
        else:
            content.append(Spacer(1, 60))
            content.append(Paragraph(self._text(display_title, language), styles["title"]))

        content.append(PageBreak())

        content.extend(
        