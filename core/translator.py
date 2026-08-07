"""
Optional machine translation AND AI proofreading of chapters before PDF
creation.

TRANSLATION - three backends, picked via NOVEL_DOWNLOADER_TRANSLATOR:

  - "google" (default, zero setup): the free Google Translate endpoint via
    `deep-translator`. No API key needed, but this is an UNOFFICIAL free
    endpoint - it can be rate-limited, and translation quality is
    classic-NMT grade: serviceable, but literal/mechanical, especially for
    dialogue and tone.

  - "gemini" (optional, free tier with your own API key): translates each
    chunk with Google's Gemini model. Meaningfully better than classic
    machine translation for fiction, and has a genuinely free tier (no
    credit card) at ai.google.dev, just with daily rate limits.
        export GEMINI_API_KEY="..."
        export NOVEL_DOWNLOADER_TRANSLATOR="gemini"

  - "anthropic" (optional, needs your own PAID API key): translates each
    chunk with Claude - generally the highest quality of the three.
        pip install anthropic
        export ANTHROPIC_API_KEY="sk-ant-..."
        export NOVEL_DOWNLOADER_TRANSLATOR="anthropic"

PROOFREADING (grammar/punctuation cleanup, in either English or Persian)
uses the SAME backend selection, but only "gemini" and "anthropic" can
actually proofread - "google" is a translation-only endpoint with no
proofreading ability, so proofread_text() is a no-op (with a warning) if
the backend is "google" or unconfigured. This never blocks PDF creation -
proofreading is purely an optional polish step.

Every LLM call falls back gracefully (to Google Translate for translation,
or to leaving text unchanged for proofreading) if its backend's
requirements (package/API key) aren't met, so switching backends is always
just changing the env var - nothing else in the app needs to change.

⚠️ None of this could be executed/tested in the sandbox this was written
in (no internet access there to reach any of these endpoints). The
chunking/retry logic follows each backend's documented API and known
limits, but you should run a small test yourself before relying on it for
a long novel - see CHANGELOG_FA.md.
"""

import os
import time

import requests

from core.logger import get_logger

log = get_logger(__name__)

# The free Google Translate endpoint used by deep-translator rejects
# requests above roughly 5000 characters - stay safely under that.
MAX_CHUNK_CHARS = 4500

# LLM backends (Gemini/Claude) have much larger context windows, but
# keeping chunks a similar size keeps output consistent in style/pacing
# between chunks and keeps any single request small enough to retry
# cheaply on failure.
MAX_CHUNK_CHARS_LLM = 8000

# Small delay between chunk requests to stay polite to free endpoints
# and reduce the chance of getting temporarily rate-limited.
DELAY_BETWEEN_CHUNKS = 0.6

RETRY_DELAYS = [3, 8, 20]

ANTHROPIC_MODEL = "claude-sonnet-4-5"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

TRANSLATION_SYSTEM_PROMPT = (
    "You are a professional literary translator. Translate the given "
    "English web-novel chapter text into natural, fluent Persian (Farsi) "
    "suitable for a printed book. Preserve paragraph breaks exactly "
    "(one blank line between paragraphs). Preserve dialogue as dialogue - "
    "natural spoken Persian, not stiff literal phrasing. Keep character "
    "names transliterated consistently. Output ONLY the translated Persian "
    "text - no notes, no explanations, no English, no markdown formatting."
)

EDIT_SYSTEM_PROMPT = (
    "You are a copy editor. The text given to you may be in English or in "
    "Persian (Farsi) - detect which, and proofread it IN THAT SAME "
    "LANGUAGE. Do not translate it. Fix only: grammar (correct "
    "subject-verb agreement, verb tense/conjugation consistency), "
    "punctuation (quotation marks, commas, sentence-ending marks), typos, "
    "and spacing. Preserve the exact paragraph structure (one blank line "
    "between paragraphs), all dialogue, all plot content, and the "
    "author's voice/style - do not summarize, shorten, expand, censor, or "
    "rewrite sentences beyond what's needed to fix grammar/punctuation. "
    "Output ONLY the corrected text - no notes, no explanations, no "
    "markdown formatting."
)


def _backend_name() -> str:
    return os.environ.get("NOVEL_DOWNLOADER_TRANSLATOR", "google").strip().lower()


def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    """
    Split text into pieces under max_chars, breaking on paragraph
    boundaries ("\\n\\n") wherever possible so a chunk boundary never lands
    in the middle of a sentence unless a single paragraph itself is too
    long (rare, but handled by a hard split as a last resort).
    """
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph

        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= max_chars:
            current = paragraph
        else:
            for i in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[i : i + max_chars])

    if current:
        chunks.append(current)

    return chunks


def _translate_google(text: str, target_lang: str, source_lang: str) -> str:
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        log.warning("deep-translator not installed - returning original text untranslated")
        return text

    chunks = _split_into_chunks(text, MAX_CHUNK_CHARS)
    translated_chunks = []

    for chunk in chunks:
        result = None

        for attempt, delay in enumerate([0.0] + RETRY_DELAYS):
            if delay:
                time.sleep(delay)

            try:
                result = GoogleTranslator(source=source_lang, target=target_lang).translate(chunk)
                if result:
                    break
            except Exception as error:
                log.warning(f"Google translation attempt {attempt + 1} failed: {error}")

        if not result:
            log.warning("Translation failed after all retries - keeping original text for this chunk")

        translated_chunks.append(result or chunk)
        time.sleep(DELAY_BETWEEN_CHUNKS)

    return "\n\n".join(translated_chunks)


def _call_anthropic(chunk: str, system_prompt: str) -> str | None:
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    result = None

    for attempt, delay in enumerate([0.0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay)

        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=8000,
                system=system_prompt,
                messages=[{"role": "user", "content": chunk}],
            )
            result = "".join(
                block.text for block in response.content if hasattr(block, "text")
            ).strip()
            if result:
                return result
        except Exception as error:
            log.warning(f"Anthropic request attempt {attempt + 1} failed: {error}")

    return None


def _call_gemini(chunk: str, system_prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log.warning("GEMINI_API_KEY not set")
        return None

    for attempt, delay in enumerate([0.0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay)

        try:
            response = requests.post(
                GEMINI_API_URL,
                params={"key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": chunk}]}],
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                result = "".join(part.get("text", "") for part in parts).strip()
                if result:
                    return result
        except Exception as error:
            log.warning(f"Gemini request attempt {attempt + 1} failed: {error}")

        time.sleep(DELAY_BETWEEN_CHUNKS)

    return None


def _translate_anthropic(text: str, target_lang: str) -> tuple[str, str]:
    chunks = _split_into_chunks(text, MAX_CHUNK_CHARS_LLM)
    translated_chunks = []
    used_fallback = False

    for chunk in chunks:
        result = _call_anthropic(chunk, TRANSLATION_SYSTEM_PROMPT)
        if not result:
            log.warning("Anthropic translation failed - falling back to Google for this chunk")
            result = _translate_google(chunk, target_lang, "auto")
            used_fallback = True
        translated_chunks.append(result)

    label = "Anthropic (Claude), partially fell back to Google Translate" if used_fallback else "Anthropic (Claude)"
    return "\n\n".join(translated_chunks), label


def _translate_gemini(text: str, target_lang: str) -> tuple[str, str]:
    chunks = _split_into_chunks(text, MAX_CHUNK_CHARS_LLM)
    translated_chunks = []
    used_fallback = False

    for chunk in chunks:
        result = _call_gemini(chunk, TRANSLATION_SYSTEM_PROMPT)
        if not result:
            log.warning("Gemini translation failed - falling back to Google for this chunk")
            result = _translate_google(chunk, target_lang, "auto")
            used_fallback = True
        translated_chunks.append(result)

    label = "Gemini, partially fell back to Google Translate" if used_fallback else "Gemini"
    return "\n\n".join(translated_chunks), label


def translate_text_labeled(text: str, target_lang: str = "fa", source_lang: str = "auto") -> tuple[str, str]:
    """
    Same as translate_text(), but also returns a human-readable label
    naming which backend actually produced the result - including when a
    configured backend (Gemini/Claude) failed partway through and some
    chunks silently fell back to Google Translate, so that fallback is
    visible instead of hidden.
    """
    if not text or not text.strip():
        return text, "-"

    backend = _backend_name()

    if backend == "anthropic":
        return _translate_anthropic(text, target_lang)

    if backend == "gemini":
        return _translate_gemini(text, target_lang)

    return _translate_google(text, target_lang, source_lang), "Google Translate"


def translate_text(text: str, target_lang: str = "fa", source_lang: str = "auto") -> str:
    """
    Translate `text` using whichever backend NOVEL_DOWNLOADER_TRANSLATOR
    selects (default: google). Best-effort: falls back rather than raising
    or dropping content - a partially-translated chapter is much better
    than a crash or a blank page.
    """
    translated, _label = translate_text_labeled(text, target_lang, source_lang)
    return translated


def proofread_text_labeled(text: str) -> tuple[str, str]:
    """
    Same as proofread_text(), but also returns a human-readable label
    naming which backend actually proofread the text (or "Not applied" if
    skipped because no LLM backend is configured).
    """
    if not text or not text.strip():
        return text, "-"

    backend = _backend_name()

    if backend not in ("gemini", "anthropic"):
        log.warning(
            "AI proofreading needs NOVEL_DOWNLOADER_TRANSLATOR set to "
            "'gemini' or 'anthropic' - skipping proofreading, text left "
            "unchanged."
        )
        return text, "Not applied (no gemini/anthropic backend configured)"

    caller = _call_gemini if backend == "gemini" else _call_anthropic
    backend_label = "Gemini" if backend == "gemini" else "Anthropic (Claude)"
    chunks = _split_into_chunks(text, MAX_CHUNK_CHARS_LLM)
    edited_chunks = []
    any_failed = False

    for chunk in chunks:
        result = caller(chunk, EDIT_SYSTEM_PROMPT)
        if not result:
            log.warning(f"Proofreading failed for a chunk via {backend} - keeping original text for it")
            any_failed = True
        edited_chunks.append(result or chunk)
        time.sleep(DELAY_BETWEEN_CHUNKS)

    label = f"{backend_label}, partially failed (some text left unedited)" if any_failed else backend_label
    return "\n\n".join(edited_chunks), label


def proofread_text(text: str) -> str:
    """
    AI proofreading pass: fixes grammar (subject-verb agreement, verb
    conjugation), punctuation, typos and spacing - WITHOUT translating,
    summarizing, or rewriting content. Works on English or Persian text
    (the model detects which and corrects it in the same language).

    Only "gemini" and "anthropic" backends can actually proofread - the
    free Google Translate endpoint has no proofreading capability, so if
    NOVEL_DOWNLOADER_TRANSLATOR is "google" (the default) or unset, this
    is a no-op: the original text is returned unchanged, with a warning
    logged, rather than silently failing or crashing PDF creation.
    """
    edited, _label = proofread_text_labeled(text)
    return edited
