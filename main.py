"""
Novel Downloader main application.
"""

import gc

from config import (
    DEFAULT_CHAPTERS_PER_PDF,
    DEFAULT_DOWNLOAD_BATCH,
    DEFAULT_MAX_WORKERS,
    PROXY_URL,
    VERSION,
)
from core.collection import Collection
from core.downloader import ChapterDownloader
from core.library import Library
from core.logger import get_logger
from core.pdfmaker import PDFMaker
from core.translator import proofread_text_labeled, translate_text, translate_text_labeled
from core.utils import format_ranges, parse_chapter_selection, progress_bar
from parsers.factory import ParserFactory

log = get_logger(__name__)


def get_number(message: str, default: int | None = None) -> int:
    while True:
        value = input(message).strip()

        if not value and default is not None:
            return default

        try:
            return int(value)
        except ValueError:
            print("Please enter a number.")


def get_yes_no(message: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{message} ({suffix}): ").strip().lower()

    if not value:
        return default

    return value == "y"


def print_status(all_chapters, downloaded, corrupted, failed):
    total = len(all_chapters)
    print(f"\nTotal chapters on site: {total}")
    print(f"Already downloaded:     {len(downloaded)}  [{format_ranges(downloaded)}]")

    missing = Library.missing_chapters([c.number for c in all_chapters], downloaded)
    if missing:
        print(f"Missing:                {len(missing)}  [{format_ranges(missing)}]")

    gaps = Library.detect_gaps([c.number for c in all_chapters])
    if gaps:
        print(f"Gaps on the site itself (never listed): [{format_ranges(gaps)}]")

    if corrupted:
        print(f"Corrupted PDFs found: {[p.name for p in corrupted]}")
        print("  -> use option 6 to rebuild them from cache (if still cached).")

    if failed:
        print(f"Previously failed chapters: [{format_ranges(failed)}]  (option 7 to retry)")

    print()


def translate_chapters(chapters, target_lang="fa", proofread=False):
    """
    Machine-translates each chapter's title and body, and optionally runs
    an AI proofreading pass (grammar/punctuation cleanup) on the result.
    Best-effort throughout - a chapter that fails to translate/proofread
    keeps its best-available text rather than blocking the whole batch.

    Also returns a per-chapter label of which backend actually produced
    the translated/proofread text (e.g. "Gemini", or "Gemini, partially
    fell back to Google Translate" if the configured backend failed
    partway through a chapter) - shown on the PDF's info page so a silent
    fallback is never hidden from you.
    """
    titles = {}
    bodies = {}
    backend_labels = {}

    action = "Translating and proofreading" if proofread else "Translating"
    print(f"\n{action} {len(chapters)} chapters...")

    with progress_bar(total=len(chapters), desc=action, unit="chapter") as progress:
        for chapter in chapters:
            title, title_label = translate_text_labeled(chapter.title, target_lang=target_lang)
            body, body_label = translate_text_labeled(chapter.content, target_lang=target_lang)
            label = body_label  # the body is representative of the chapter as a whole

            if proofread:
                title, _ = proofread_text_labeled(title)
                body, proofread_label = proofread_text_labeled(body)
                label = f"{label} + proofread: {proofread_label}"

            titles[chapter.number] = title
            bodies[chapter.number] = body
            backend_labels[chapter.number] = label
            progress.update(1)

    return titles, bodies, backend_labels


def proofread_chapters(chapters):
    """
    AI proofreading pass with NO translation - used when the person wants
    the original English cleaned up but doesn't want a Persian PDF.
    """
    titles = {}
    bodies = {}
    backend_labels = {}

    print(f"\nProofreading {len(chapters)} chapters...")

    with progress_bar(total=len(chapters), desc="Proofreading", unit="chapter") as progress:
        for chapter in chapters:
            title, _ = proofread_text_labeled(chapter.title)
            body, label = proofread_text_labeled(chapter.content)
            titles[chapter.number] = title
            bodies[chapter.number] = body
            backend_labels[chapter.number] = label
            progress.update(1)

    return titles, bodies, backend_labels


def run_batched_download(
    downloader: ChapterDownloader,
    maker: PDFMaker,
    library: Library,
    novel,
    chapters,
    start,
    target,
    create_pdf,
    download_batch,
    per_pdf,
    only_numbers=None,
    translate=False,
    display_title=None,
    proofread=False,
):
    current = start
    pdf_buffer = []
    total_known_chapters = len(chapters)

    def flush_pdf_buffer(batch):
        if translate:
            titles, bodies, backend_labels = translate_chapters(batch, target_lang="fa", proofread=proofread)
            maker.split_and_create(
                novel.title,
                batch,
                per_pdf,
                novel=novel,
                total_known_chapters=total_known_chapters,
                language="fa",
                display_title=display_title,
                chapter_titles=titles,
                chapter_bodies=bodies,
                chapter_backend_labels=backend_labels,
            )
        elif proofread:
            titles, bodies, backend_labels = proofread_chapters(batch)
            maker.split_and_create(
                novel.title,
                batch,
                per_pdf,
                novel=novel,
                total_known_chapters=total_known_chapters,
                chapter_titles=titles,
                chapter_bodies=bodies,
                chapter_backend_labels=backend_labels,
            )
        else:
            maker.split_and_create(
                novel.title, batch, per_pdf, novel=novel, total_known_chapters=total_known_chapters
            )

    while current <= target:
        batch_end = min(current + download_batch - 1, target)

        print(f"\nDownloading chapters {current} to {batch_end}")

        downloaded = downloader.download_range(
            chapters, current, batch_end, batch_size=download_batch, only_numbers=only_numbers
        )

        if downloaded:
            if create_pdf:
                pdf_buffer.extend(downloaded)

                while len(pdf_buffer) >= per_pdf:
                    flush_pdf_buffer(pdf_buffer[:per_pdf])
                    del pdf_buffer[:per_pdf]
                    gc.collect()  # release finished chapters' text before the next batch

            downloaded.clear()

        current = batch_end + 1

    if create_pdf and pdf_buffer:
        flush_pdf_buffer(pdf_buffer)
        pdf_buffer.clear()
        gc.collect()

    novel_key = novel.slug
    library.set_failed(novel_key, downloader.failed_chapters)

    print("\nFinished!")

    if downloader.failed_chapters:
        print(f"Failed chapters this run: {format_ranges(downloader.failed_chapters)}")
        print("(saved - use option 7 next time to retry just these)")


def rebuild_from_cache(downloader: ChapterDownloader, maker: PDFMaker, library, novel, all_chapters, downloaded_numbers):
    """
    Request #14: if chapters are still sitting in cache but their PDF is
    missing/corrupted, rebuild the PDF WITHOUT re-downloading anything.
    """
    cached_numbers = downloader.cache.cached_numbers()
    rebuildable = sorted(cached_numbers - downloaded_numbers)

    if not rebuildable:
        print("Nothing to rebuild - no cached chapters are missing from a PDF.")
        return

    print(f"Rebuilding PDFs for {len(rebuildable)} cached chapters: {format_ranges(rebuildable)}")

    lookup = {c.number: c for c in all_chapters}
    chapters_to_build = []

    for number in rebuildable:
        chapter = lookup.get(number)
        if chapter is None:
            continue
        content = downloader.cache.load(number)
        if not content:
            continue
        chapter.content = content
        chapters_to_build.append(chapter)

    if not chapters_to_build:
        print("Cached chapters no longer match the current chapter list - nothing rebuilt.")
        return

    per_pdf = library.get_setting("chapters_per_pdf", DEFAULT_CHAPTERS_PER_PDF)
    maker.split_and_create(
        novel.title, chapters_to_build, per_pdf, novel=novel, total_known_chapters=len(all_chapters)
    )
    print("Rebuild complete.")


def open_novel(url: str, library: Library, collection: Collection):
    """
    The full "novel detail" workflow: load chapters, show status, let the
    user pick an action (download range / missing / rebuild / retry /
    manual chapter selection), and save the novel into the persistent
    "My Library" collection so it can be reopened later without the URL.
    """
    parser = ParserFactory.create(url)

    novel = parser.get_novel(url)

    print(f"\nNovel: {novel.title}  [{parser.name}]")
    if novel.is_completed:
        print("Status: Completed")

    print("Finding chapters...")

    chapters = parser.get_chapters(novel)

    if not chapters:
        print("No chapters found.")
        return

    print(f"Found {len(chapters)} chapters")

    collection.add(novel)
    collection.update_known_count(novel.slug, len(chapters))

    maker = PDFMaker()

    downloaded_numbers, corrupted_pdfs = library.scan_downloaded(maker.output_dir, novel.title)
    failed_before = library.get_failed(novel.slug)

    print_status(chapters, downloaded_numbers, corrupted_pdfs, failed_before)

    print("1. Continue Download")
    print("2. Download Custom Range")
    print("3. Download Latest")
    print("4. Download All (Smart Batch)")
    print("5. Download Only Missing Chapters")
    print("6. Rebuild PDFs From Cache (no re-download)")
    print("7. Retry Failed Chapters")
    print("8. Select Specific Chapters (e.g. 1-10,15,22)")
    print("9. Remove From My Library")
    print("0. Back")
    print()

    option = input("Select option: ").strip()

    last_downloaded = max(downloaded_numbers) if downloaded_numbers else 0
    only_numbers = None

    if option == "1":
        start = last_downloaded + 1
        target = get_number("Download until chapter: ")

    elif option == "2":
        start = get_number("Start chapter: ")
        target = get_number("End chapter: ")

    elif option in ("3", "4"):
        start = last_downloaded + 1
        target = len(chapters)
        if option == "4":
            print("\nSmart Download All")

    elif option == "5":
        all_numbers = [c.number for c in chapters]
        missing = Library.missing_chapters(all_numbers, downloaded_numbers)

        if not missing:
            print("Nothing missing - everything is already downloaded.")
            return

        start, target = min(missing), max(missing)
        only_numbers = set(missing)
        print(f"Downloading {len(missing)} missing chapters: {format_ranges(missing)}")

    elif option == "6":
        downloader = ChapterDownloader(parser, novel)
        rebuild_from_cache(downloader, maker, library, novel, chapters, downloaded_numbers)
        return

    elif option == "7":
        if not failed_before:
            print("No previously failed chapters recorded.")
            return

        start, target = min(failed_before), max(failed_before)
        only_numbers = set(failed_before)
        print(f"Retrying {len(failed_before)} failed chapters: {format_ranges(failed_before)}")

    elif option == "8":
        all_numbers = [c.number for c in chapters]

        while True:
            text = input("Chapters (e.g. 1-10,15,22): ").strip()
            try:
                selected = parse_chapter_selection(text, valid_numbers=all_numbers)
                break
            except ValueError:
                print("Couldn't parse that - use numbers and ranges like 1-10,15,22")

        if not selected:
            print("No valid chapters in that selection.")
            return

        start, target = min(selected), max(selected)
        only_numbers = set(selected)
        print(f"Downloading {len(selected)} selected chapters: {format_ranges(selected)}")

    elif option == "9":
        if get_yes_no(f"Remove '{novel.title}' from My Library? (downloaded PDFs are kept)"):
            collection.remove(novel.slug)
            print("Removed.")
        return

    elif option == "0":
        return

    else:
        print("Invalid option.")
        return

    target = min(target, len(chapters))
    start = max(start, 1)

    if start > target and only_numbers is None:
        print("Nothing new to download.")
        return

    create_pdf = get_yes_no("Create PDF?", default=True)

    download_batch = library.get_setting("download_batch", DEFAULT_DOWNLOAD_BATCH)
    per_pdf = library.get_setting("chapters_per_pdf", DEFAULT_CHAPTERS_PER_PDF)
    max_workers = library.get_setting("max_workers", DEFAULT_MAX_WORKERS)

    if create_pdf:
        value = input(f"Download Batch Size (Default: {download_batch}): ").strip()
        if value:
            download_batch = int(value)

        value = input(f"Chapters per PDF (Default: {per_pdf}): ").strip()
        if value:
            per_pdf = int(value)

        value = input(f"Parallel Workers (Default: {max_workers}): ").strip()
        if value:
            max_workers = int(value)

        library.save_settings(
            {
                "download_batch": download_batch,
                "chapters_per_pdf": per_pdf,
                "max_workers": max_workers,
            }
        )

    translate = False
    display_title = None
    proofread = False

    if create_pdf:
        translate = get_yes_no(
            "Translate to Persian before creating PDF? (machine translation, best-effort)"
        )
        if translate:
            print("Translating novel title...")
            display_title = translate_text(novel.title, target_lang="fa")

        proofread = get_yes_no(
            "AI-proofread the text (grammar/punctuation) before creating PDF? "
            "(needs gemini or anthropic backend configured)"
        )

    downloader = ChapterDownloader(parser, novel, max_workers=max_workers)

    run_batched_download(
        downloader,
        maker,
        library,
        novel,
        chapters,
        start,
        target,
        create_pdf,
        download_batch,
        per_pdf,
        only_numbers=only_numbers,
        translate=translate,
        display_title=display_title,
        proofread=proofread,
    )


def update_all(collection: Collection):
    """
    Tachiyomi-style "Library update": quickly check every saved novel for
    new chapters without downloading anything.
    """
    saved = collection.list_all()

    if not saved:
        print("My Library is empty.")
        return

    print(f"\nChecking {len(saved)} novels for new chapters...\n")

    for entry in saved:
        try:
            parser = ParserFactory.create(entry.url)
            novel = parser.get_novel(entry.url)
            chapters = parser.get_chapters(novel)
        except Exception as error:
            print(f"  {entry.title}: could not check ({error})")
            log.warning(f"Update check failed for {entry.title}: {error}")
            continue

        new_count = len(chapters) - entry.known_chapter_count

        if new_count > 0:
            print(f"  {entry.title}: {new_count} new chapter(s) (now {len(chapters)} total)")
        else:
            print(f"  {entry.title}: up to date ({len(chapters)} chapters)")

        collection.update_known_count(entry.slug, len(chapters))


def show_my_library(collection: Collection):
    saved = collection.list_all()

    if not saved:
        print("\nMy Library is empty. Add a novel by URL first.")
        return None

    print("\n=== My Library ===")
    for index, entry in enumerate(saved, start=1):
        known = f"{entry.known_chapter_count} chapters known" if entry.known_chapter_count else "not yet scanned"
        print(f"{index}. {entry.title}  [{entry.site_name}] - {known}")
    print()

    return saved


def search_novels():
    """
    Search novels by name (currently FreeWebNovel only - see
    parsers/freewebnovel.py:search for how, and why it's sitemap-based
    rather than using the site's own JS-driven search page).
    """
    from parsers.freewebnovel import FreeWebNovelParser

    query = input("Search for: ").strip()

    if not query:
        return None

    print("Searching (FreeWebNovel)...")

    parser = FreeWebNovelParser()
    results = parser.search(query)

    if not results:
        print("No results. Try a shorter or different search term.")
        return None

    print(f"\n=== Search Results ({len(results)}) ===")
    for index, (title_guess, url) in enumerate(results, start=1):
        print(f"{index}. {title_guess}")
    print()

    choice = input("Pick a number (or Enter to cancel): ").strip()

    if not choice.isdigit() or not (1 <= int(choice) <= len(results)):
        return None

    return results[int(choice) - 1][1]


def main():
    print(f"=== Novel Downloader v{VERSION} ===")

    if PROXY_URL:
        print(f"(using proxy: {PROXY_URL})")

    library = Library()
    collection = Collection()

    while True:
        saved = show_my_library(collection)

        print("A. Add New Novel (by URL)")
        print("S. Search Novel by Name (FreeWebNovel)")
        if saved:
            print("U. Update All (check for new chapters)")
        print("0. Exit")
        print()

        choice = input("Select a novel number, or an option: ").strip()

        try:
            if choice == "0" or choice.lower() == "exit":
                print("Bye!")
                return

            elif choice.lower() == "a":
                url = input("Novel URL: ").strip()
                if url:
                    open_novel(url, library, collection)

            elif choice.lower() == "s":
                url = search_novels()
                if url:
                    open_novel(url, library, collection)

            elif choice.lower() == "u" and saved:
                update_all(collection)

            elif choice.isdigit() and saved and 1 <= int(choice) <= len(saved):
                entry = saved[int(choice) - 1]
                open_novel(entry.url, library, collection)

            else:
                print("Invalid option.")

        except Exception as error:
            log.exception("Unhandled error")
            print(f"Error: {error}")
            print("(details written to logs/download.log)")

        input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
