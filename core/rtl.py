"""
Right-to-left text shaping for Persian/Arabic script.

reportlab has NO built-in support for Arabic-script text: it doesn't join
letters into their correct contextual forms, and it doesn't reorder
characters for right-to-left display - passed raw, Persian text renders as
disconnected, visually-reversed letters. This module fixes both problems
using the standard fix (documented pattern for reportlab + Arabic script):

  1. `arabic_reshaper` rewrites each letter into its correct joined
     presentation form based on its neighbors.
  2. `python-bidi`'s get_display() then reorders the string into the
     order it should visually appear left-to-right on the page - which is
     what reportlab actually needs, since it always lays out strings
     left-to-right internally regardless of the language.

⚠️ Not executable/testable in the sandbox this was written in (no
internet there to install these two packages). The approach itself is the
standard documented technique for this exact problem, but please render
one test page yourself first - see CHANGELOG_FA.md.
"""

from core.logger import get_logger

log = get_logger(__name__)

_warned_once = False


def shape_rtl(text: str) -> str:
    """
    Reshape + reorder Persian/Arabic text for correct rendering in
    reportlab. Returns the text unchanged (with a one-time warning) if the
    required packages aren't installed, rather than crashing.
    """
    global _warned_once

    if not text:
        return text

    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)

    except ImportError:
        if not _warned_once:
            log.warning(
                "arabic_reshaper / python-bidi not installed - Persian text "
                "will render as disconnected/reversed letters. Install them "
                "with: pip install arabic-reshaper python-bidi"
            )
            _warned_once = True
        return text
