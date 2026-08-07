"""
Common utility functions.
"""

import json
import re
from pathlib import Path


def slugify(text: str) -> str:
    """
    Convert text into a safe folder name.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def safe_filename(text: str) -> str:
    """
    Create a safe filename (strips characters invalid on Windows/Android
    filesystems too, not just POSIX).
    """
    text = re.sub(r'[\\/*?:"<>|]', "", text)
    return text.strip().rstrip(".")


def ensure_directory(path: Path) -> None:
    """
    Create directory if it does not exist.
    """
    path.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, data: dict) -> None:
    """
    Save dictionary as JSON.
    """
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


def load_json(path: Path, default=None):
    """
    Load JSON file. Never raises - a corrupt/missing file just falls back
    to `default`, since a broken settings file should never crash the app.
    """
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return default


def format_ranges(numbers) -> str:
    """
    Compress a list of ints like [1,2,3,5,6,9] into "1-3, 5-6, 9" for
    readable console/log output.
    """
    numbers = sorted(set(numbers))
    if not numbers:
        return "-"

    ranges = []
    start = prev = numbers[0]

    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append((start, prev))
        start = prev = number

    ranges.append((start, prev))

    return ", ".join(
        f"{a}" if a == b else f"{a}-{b}" for a, b in ranges
    )


def parse_chapter_selection(text: str, valid_numbers=None) -> list[int]:
    """
    Parse a Tachiyomi-style manual chapter selection string like
    "1-10, 15, 22-25" into a sorted list of chapter numbers. Raises
    ValueError on malformed input so the caller can ask again.

    If `valid_numbers` is given, the result is intersected with it (so
    picking a chapter number that doesn't actually exist is silently
    dropped rather than crashing the download).
    """
    result = set()

    for part in text.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str.strip()), int(end_str.strip())
            if start > end:
                start, end = end, start
            result.update(range(start, end + 1))
        else:
            result.add(int(part))

    if valid_numbers is not None:
        result &= set(valid_numbers)

    return sorted(result)


class SimpleProgress:
    """
    Minimal drop-in replacement for tqdm's context-manager API, used when
    tqdm isn't installed so the app degrades gracefully instead of crashing
    with ModuleNotFoundError (the original project used tqdm without
    listing it in requirements.txt).
    """

    def __init__(self, total: int, desc: str = "", unit: str = "it"):
        self.total = total
        self.desc = desc
        self.unit = unit
        self.count = 0

    def __enter__(self):
        if self.desc:
            print(f"{self.desc}: 0/{self.total}")
        return self

    def update(self, n: int = 1):
        self.count += n
        print(f"\r{self.desc}: {self.count}/{self.total} {self.unit}", end="")

    def __exit__(self, exc_type, exc_val, exc_tb):
        print()
        return False


def progress_bar(total: int, desc: str = "", unit: str = "it"):
    """
    Returns a tqdm progress bar if tqdm is installed, otherwise falls back
    to SimpleProgress so the app never crashes just because an optional
    dependency is missing.
    """
    try:
        from tqdm import tqdm
        return tqdm(total=total, desc=desc, unit=unit)
    except ImportError:
        return SimpleProgress(total=total, desc=desc, unit=unit)
