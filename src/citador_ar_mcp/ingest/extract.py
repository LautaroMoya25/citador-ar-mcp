"""PDF text extraction, and the quality gate that decides whether to trust it.

Fase 0 turned up something the plan did not anticipate, and it is the reason
this module exists in the shape it does.

The CSJN's older rulings are **not** scans. They carry a real text layer, so a
naive extractor reports 5.000 characters a page and everything looks fine. But
the font subsets have no usable ``ToUnicode`` map, so what comes out is a
monoalphabetic substitution of the actual text::

    plaintext   que en consecuencia
    extracted   !B)V )7V %87@)%B)7%.#V

The substitution is not even stable within a document -- different font subsets
on different pages use different mappings -- so it cannot be undone with a
single table. Character counts, and even Spanish stop-word rates, do **not**
detect this: the cipher preserves letter frequency, so a garbled page scores
like Spanish on any frequency-based measure.

:func:`text_quality` is therefore token-shaped rather than frequency-shaped. It
asks what share of whitespace-separated tokens look like Spanish words at all,
which collapses to near zero on ciphered text while staying above 0.8 on real
text. Measured during Fase 0:

===============================  ======  ========
Ruling                           tokens  quality
===============================  ======  ========
Arriola      (Fallos 332:1963)    16072     0.84
Montalvo     (Fallos 313:1333)    13609     0.00
Bazterrica   (Fallos 308:1392)    26722     0.00
Colavini     (Fallos 300:254)      5922     0.00
===============================  ======  ========

Anything that fails the gate is stored as ``text_status='garbled'`` and is never
fed to the citation finder. A ciphered page cannot produce a real quote, and
without a quote there is no row.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

# The CSJN PDFs mark a hyphenated line break with U+FFFE (a noncharacter),
# not with a hyphen: `juris<U+FFFE>prudencia`. Dropping it rejoins the word.
# 389 occurrences in Arriola alone; leaving them in breaks every citation regex
# that happens to straddle a line break.
HYPHEN_BREAK: Final = "￾"

#: Below this, extracted text is treated as unusable.
#:
#: Set from the Fase 0 measurements, which separate cleanly once page furniture
#: is included in the score: fourteen known-good rulings scored 0.77-0.89, and
#: the three known-garbled ones scored 0.42, 0.42 and 0.61. The floor sits in
#: the gap, closer to the garbled side, because the asymmetry matters -- letting
#: ciphered text through produces unreadable quotes attached to real-looking
#: edges, which is precisely the failure a citator cannot afford.
#:
#: Note that Montalvo scores 0.61 rather than ~0.0: digits and punctuation often
#: survive the broken font encoding even when letters do not, so a garbled
#: document still yields readable `Fallos: N:M` strings. The edges are findable;
#: the passages that would justify them are not. No quote, no row.
QUALITY_FLOOR: Final = 0.70

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"^[A-Za-zÁÉÍÓÚÑÜáéíóúñü][A-Za-zÁÉÍÓÚÑÜáéíóúñü.,;:()«»\"'\-]*$")
_VOWEL = re.compile(r"[aeiouáéíóúAEIOUÁÉÍÓÚ]")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


class TextStatus(StrEnum):
    """What we know about the body text of a ruling."""

    EXTRACTED = "extracted"
    """Real text, straight from the PDF."""

    OCR = "ocr"
    """Recovered by OCR."""

    GARBLED = "garbled"
    """A text layer exists but its encoding is unusable. Needs OCR."""

    UNAVAILABLE = "unavailable"
    """No document published, or the download failed."""


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """The result of reading one PDF."""

    text: str
    pages: int
    quality: float
    status: TextStatus

    @property
    def usable(self) -> bool:
        return self.status in (TextStatus.EXTRACTED, TextStatus.OCR)


def clean_pdf_text(raw: str) -> str:
    """Normalise raw PDF text without changing what it says.

    Rejoins hyphenated line breaks, collapses runs of spaces and tabs, and
    trims trailing whitespace. Paragraph breaks are preserved because the
    citation finder uses them to bound a quote.
    """
    text = raw.replace(HYPHEN_BREAK, "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def text_quality(text: str) -> float:
    """Share of tokens that look like Spanish words, in ``0.0..1.0``.

    Deliberately not frequency-based. See the module docstring: the failure mode
    this has to catch preserves letter frequencies exactly.
    """
    tokens = text.split()
    if not tokens:
        return 0.0
    ok = sum(1 for t in tokens if len(t) >= 2 and _TOKEN.match(t) and _VOWEL.search(t))
    return ok / len(tokens)


def classify(text: str, *, floor: float = QUALITY_FLOOR) -> tuple[float, TextStatus]:
    """Score ``text`` and decide whether it can be trusted."""
    if not text.strip():
        return 0.0, TextStatus.UNAVAILABLE
    q = text_quality(text)
    return q, TextStatus.EXTRACTED if q >= floor else TextStatus.GARBLED


def extract_pdf(
    path: Path | str,
    *,
    floor: float = QUALITY_FLOOR,
    ocr_fallback: bool = False,
    ocr_cache: Path | None = None,
) -> ExtractedText:
    """Extract and grade the text of a PDF, optionally recovering it by OCR.

    ``pypdfium2`` is an optional dependency (``pip install citador-ar-mcp[ingest]``)
    because the MCP server itself never touches a PDF: the ingest pipeline runs
    offline and ships a SQLite file. See CLAUDE.md, section 2.

    With ``ocr_fallback``, a document that fails the quality gate is rasterised
    and read back with Tesseract. The OCR output has to clear the *same* gate:
    OCR can fail too, and a citator that quotes garbage is worse than one that
    admits it has no text. When it does not clear it, the original verdict stands.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise RuntimeError("extract_pdf needs the 'ingest' extra: uv sync --extra ingest") from exc

    doc = pdfium.PdfDocument(str(path))
    try:
        pages = len(doc)
        raw = "\n\n".join(doc[i].get_textpage().get_text_range() for i in range(pages))
    finally:
        doc.close()

    text = clean_pdf_text(raw)
    quality, status = classify(text, floor=floor)
    original = ExtractedText(text=text, pages=pages, quality=round(quality, 4), status=status)

    if status is TextStatus.EXTRACTED or not ocr_fallback:
        return original

    from citador_ar_mcp.ingest import ocr

    if not ocr.available():
        log.warning("%s es '%s' y no hay tesseract para recuperarlo", Path(path).name, status.value)
        return original

    result = ocr.ocr_pdf(path, cache_dir=ocr_cache)
    ocr_text = clean_pdf_text(result.text)
    ocr_quality = text_quality(ocr_text)
    if ocr_quality < floor:
        log.warning(
            "OCR de %s quedó en %.3f, bajo el umbral %.2f: se mantiene '%s'",
            Path(path).name,
            ocr_quality,
            floor,
            status.value,
        )
        return original

    log.info(
        "OCR de %s recuperó el texto: %.3f -> %.3f%s",
        Path(path).name,
        quality,
        ocr_quality,
        " (cacheado)" if result.from_cache else "",
    )
    return ExtractedText(
        text=ocr_text,
        pages=result.pages or pages,
        quality=round(ocr_quality, 4),
        status=TextStatus.OCR,
    )
