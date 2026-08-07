"""
Library: tracks which chapters have actually been downloaded.

Design fix (was the #1 reported bug): the OLD implementation trusted a
`downloaded` list inside library.json, "confirmed" only by checking whether
matching files still existed in the *cache* folder. That meant:

  - Clearing the cache made the app think nothing was downloaded, even
    though finished PDFs were sitting right there.
  - A corrupted/lost library.json meant losing all progress tracking, even
    though the PDFs were fine.

NEW design: the PDF files themselves are the single source of truth.
Every PDF created by PDFMaker embeds its exact chapter list in the PDF's
metadata (Keywords field: "chapters=1,2,3,...")). Library.scan_downloaded()
reads that metadata straight out of the PDFs on disk. Consequences:

  - Deleting the cache: no effect on what's considered "downloaded".
  - Deleting a PDF: those chapters become "missing" again (correct).
  - Deleting/corrupting library.json: no effect - it's not used for this
    anymore, only for small user preferences (last batch size, etc).

For PDFs created by the old version of the app (no metadata), we fall back
to parsing the "Ch_start-end" range out of the filename, so upgrading
doesn't lose recognition of previously-downloaded material.
"""

import re
from pathlib import Path

from config import DATA_DIR
from core.logger import get_logger
from core.utils import load_json, safe_filename, save_json

log = get_logger(__name__)

SETTINGS_FILE = DATA_DIR / "settings.json"
FAILED_FILE = DATA_DIR / "failed_chapters.json"

_FILENAME_RANGE_RE = re.compile(r"_Ch_(\d+)-(\d+)\.pdf$", re.IGNORECASE)


class Library:
    """
    Reads download status from PDFs on disk, and stores small user
    preferences + the "failed chapters" list (so a future run can offer to
    retry just those) in data/*.json.
    """

    def __init__(self):
        self.settings = load_json(SETTINGS_FILE, default={}) or {}
        self.failed = load_json(FAILED_FILE, default={}) or {}

    # ---------------------------------------------------------------
    # Settings (batch size, worker count, etc. - NOT download progress)
    # ---------------------------------------------------------------

    def save_settings(self, updates: dict) -> None:
        self.settings.update(updates)
        save_json(SETTINGS_FILE, self.settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    # ---------------------------------------------------------------
    # Failed chapters (per novel), so they can be retried later
    # ---------------------------------------------------------------

    def set_failed(self, novel_key: str, chapter_numbers: list[int]) -> None:
        if chapter_numbers:
            self.failed[novel_key] = sorted(set(chapter_numbers))
        else:
            self.failed.pop(novel_key, None)

        save_json(FAILED_FILE, self.failed)

    def get_failed(self, novel_key: str) -> list[int]:
        return sorted(self.failed.get(novel_key, []))

    # ---------------------------------------------------------------
    # Download status - derived straight from the PDF folder
    # ---------------------------------------------------------------

    def novel_folder(self, output_dir: Path, novel_title: str) -> Path:
        return output_dir / safe_filename(novel_title)

    def scan_downloaded(
        self, output_dir: Path, novel_title: str
    ) -> tuple[set[int], list[Path]]:
        """
        Returns (downloaded_chapter_numbers, corrupted_pdf_paths).

        A PDF that can't be opened/parsed at all is reported as corrupted
        instead of silently counted as "downloaded" (request #13: PDF
        health check) - its chapters are treated as missing so the user
        can rebuild that PDF (from cache, if still present) or re-download.
        """
        folder = self.novel_folder(output_dir, novel_title)

        downloaded: set[int] = set()
        corrupted: list[Path] = []

        if not folder.exists():
            return downloaded, corrupted

        for pdf_path in sorted(folder.glob("*.pdf")):
            chapters = self._read_pdf_chapters(pdf_path)

            if chapters is None:
                corrupted.append(pdf_path)
                continue

            downloaded.update(chapters)

        return downloaded, corrupted

    def _read_pdf_chapters(self, pdf_path: Path) -> set[int] | None:
        """
        Returns the set of chapter numbers embedded in a PDF, or None if
        the PDF is unreadable/corrupted.
        """
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)  # forces a real parse, not just header read

            if page_count == 0:
                return None

            metadata = reader.metadata or {}
            keywords = (metadata.get("/Keywords") or "").strip()

            match = re.search(r"chapters=([\d,]+)", keywords)
            if match:
                return {int(n) for n in match.group(1).split(",") if n}

        except ImportError:
            log.warning("pypdf not installed - falling back to filename parsing")
        except Exception as error:
            log.warning(f"Corrupted/unreadable PDF {pdf_path.name}: {error}")
            return None

        # Fall back to filename-encoded range (old PDFs, or metadata missing)
        range_match = _FILENAME_RANGE_RE.search(pdf_path.name)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            return set(range(start, end + 1))

        # PDF opened fine but we truly can't tell which chapters it holds.
        return set()

    @staticmethod
    def missing_chapters(all_numbers, downloaded: set[int]) -> list[int]:
        return sorted(set(all_numbers) - downloaded)

    @staticmethod
    def detect_gaps(all_numbers: list[int]) -> list[int]:
        """
        Chapters missing from the *site's own* chapter list, i.e. numbers
        between min and max that never appeared at all (request: detect
        removed/skipped chapters on the source site).
        """
        if not all_numbers:
            return []

        full_range = set(range(min(all_numbers), max(all_numbers) + 1))
        return sorted(full_range - set(all_numbers))
