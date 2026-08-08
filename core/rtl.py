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

IMPORTANT LIMITATION of that two-step fix: get_display() only produces a
correct result for text that renders on ONE line. If the resulting
reordered string is longer than the available width, reportlab wraps it
onto multiple lines using plain left-to-right word-wrap logic - which has
no idea the string was already bidi-reordered, so it breaks the line at
the wrong point and scrambles word order between lines (this is exactly
the "single lines are fine, multi-line paragraphs get jumbled" bug).

The fix is shape_rtl_wrapped(): decide the line breaks OURSELVES (using
the original word order, before reordering - word order and per-word
pixel width are unaffected by bidi reordering, only their final sequence
is), reorder each already-determined line independently, then join the
lines with an explicit reportlab "<br/>" tag so reportlab draws each
line's content as-is instead of re-wrapping the (already reordered)
text itself.
"""

from core.logger import get_logger

log = get_logger(__name__)

_warned_once = False


def _rtl_libs_available() -> bool:
    global _warned_once

    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401

        return True
    except ImportError:
        if not _warned_once:
            log.warning(
                "arabic_reshaper / python-bidi not installed - Persian text "
                "will render as disconnected/reversed letters. Install them "
                "with: pip install arabic-reshaper python-bidi"
            )
            _warned_once = True
        return False


def shape_rtl(text: str) -> str:
    """
    Reshape + reorder Persian/Arabic text for correct rendering in
    reportlab. Only safe for text guaranteed to render on a SINGLE line
    (short labels, table headers) - for anything that might wrap across
    multiple lines (chapter body text, long titles), use
    shape_rtl_wrapped() instead, or this will produce scrambled word
    order between lines. Returns the text unchanged if the required
    packages aren't installed, rather than crashing.
    """
    if not text or not _rtl_libs_available():
        return text

    import arabic_reshaper
    from bidi.algorithm import get_display

    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def shape_rtl_wrapped(text: str, font_name: str, font_size: float, max_width_pt: float) -> str:
    """
    Like shape_rtl(), but line-wraps correctly for multi-line Persian
    paragraphs (see module docstring for why shape_rtl() alone breaks on
    wrapped text). Returns reportlab mini-markup with explicit <br/> tags
    between lines, meant to be passed straight into a Paragraph(). Falls
    back to shape_rtl() (single-line-safe only) if reportlab's own text
    measurement isn't available for some reason, and to the original text
    if the RTL packages aren't installed.
    """
    if not text or not _rtl_libs_available():
        return text

    import arabic_reshaper
    from bidi.algorithm import get_display

    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
    except ImportError:
        return shape_rtl(text)

    # Reshape (letter-joining) BEFORE any wrapping/reordering decisions -
    # this only changes each character's glyph, never word order or count.
    reshaped = arabic_reshaper.reshape(text)
    words = reshaped.split(" ")

    space_width = stringWidth(" ", font_name, font_size)
    lines: list[str] = []
    current_words: list[str] = []
    current_width = 0.0

    for word in words:
        word_width = stringWidth(word, font_name, font_size)
        extra = word_width if not current_words else word_width + space_width

        if current_words and current_width + extra > max_width_pt:
            lines.append(" ".join(current_words))
            current_words = [word]
            current_width = word_width
        else:
            current_words.append(word)
            current_width += extra

    if current_words:
        lines.append(" ".join(current_words))

    # Each line is now a correct, independently-wrappable chunk in
    # LOGICAL (reading) word order - reorder each one for visual RTL
    # display, then join with an explicit line break so reportlab draws
    # them as-is instead of trying to re-wrap the (already reordered)
    # combined string itself.
    reordered_lines = [get_display(line) for line in lines]
    return "<br/>".join(reordered_lines)
