"""OCR recovery for rulings whose PDF text layer is unusable.

Needed because of the finding in ``ingest/extract.py``: the CSJN's older PDFs
carry a text layer encoded through font subsets with no usable ``ToUnicode``
map, so extraction yields a monoalphabetic substitution of the real text. The
pixels are fine; only the character codes are wrong. Rasterising the page and
reading it back with Tesseract sidesteps the broken mapping entirely.

**Why not ocrmypdf.** The contract names it, and it is the right tool for its
actual job -- producing a searchable PDF by overlaying an OCR layer. That is not
this job: we want text, not a new PDF, and we want it page by page so a
87-page ruling can be OCR'd only where a citation actually lands. It also pulls
in Ghostscript, a second heavy system dependency that Windows package managers
do not carry. Rendering with ``pypdfium2`` -- already a dependency -- and piping
to Tesseract needs neither. ``ocrmypdf`` remains available under the ``ocr``
extra for anyone who wants searchable PDFs out of the pipeline.

**Setup.** Tesseract plus the Spanish language data::

    winget install UB-Mannheim.TesseractOCR
    # spa.traineddata from github.com/tesseract-ocr/tessdata_best
    # into the tessdata directory, or anywhere pointed at by TESSDATA_PREFIX

Set ``CITADOR_TESSERACT`` if the binary is not on ``PATH``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

#: 300 dpi is the resolution Tesseract is tuned for. Below roughly 200 the
#: accuracy on 1980s print falls off sharply; above 400 it costs time and buys
#: nothing.
DEFAULT_DPI: Final = 300

#: `spa` needs tessdata_best rather than the default pack: these are typewriter
#: and hot-metal prints from the 1970s and 1980s, not clean modern type.
DEFAULT_LANG: Final = "spa"

#: Page segmentation mode 6, "assume a single uniform block of text". Court
#: pages are a single justified column; the default mode 3 tries to find columns
#: that are not there and shreds the reading order.
DEFAULT_PSM: Final = "6"

_WINDOWS_DEFAULT: Final = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


class TesseractUnavailableError(RuntimeError):
    """Tesseract is not installed, or not where we looked."""

    def __init__(self) -> None:
        super().__init__(
            "No encontré tesseract. Instalalo "
            "(`winget install UB-Mannheim.TesseractOCR`, `apt install tesseract-ocr "
            "tesseract-ocr-spa`, `brew install tesseract tesseract-lang`) o definí "
            "CITADOR_TESSERACT con la ruta al binario. Hace falta el paquete de "
            "español: spa.traineddata en el directorio tessdata o en TESSDATA_PREFIX."
        )


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Recovered text plus what it cost, so callers can report honestly."""

    text: str
    pages: int
    from_cache: bool


def find_tesseract() -> Path | None:
    """Locate the Tesseract binary. ``CITADOR_TESSERACT`` wins, then ``PATH``."""
    override = os.environ.get("CITADOR_TESSERACT")
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None

    found = shutil.which("tesseract")
    if found:
        return Path(found)

    return _WINDOWS_DEFAULT if _WINDOWS_DEFAULT.exists() else None


def available() -> bool:
    """Whether OCR can run at all. Lets callers degrade instead of crashing."""
    return find_tesseract() is not None


def languages() -> list[str]:
    """Language packs Tesseract can see. Empty when it cannot run."""
    binary = find_tesseract()
    if binary is None:
        return []
    proc = subprocess.run(
        [str(binary), "--list-langs"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = proc.stdout.splitlines()
    return [line.strip() for line in lines[1:] if line.strip()]


def _cache_key(pdf: Path, lang: str, dpi: int) -> str:
    """Content-addressed, so a re-downloaded PDF does not reuse stale text."""
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
    return f"{pdf.stem}-{lang}-{dpi}-{digest}"


def ocr_pdf(
    pdf: Path | str,
    *,
    lang: str = DEFAULT_LANG,
    dpi: int = DEFAULT_DPI,
    psm: str = DEFAULT_PSM,
    cache_dir: Path | None = None,
) -> OcrResult:
    """OCR an entire PDF and return its text.

    Roughly two seconds a page at 300 dpi, so an 87-page ruling is a couple of
    minutes. Results are cached under ``cache_dir`` keyed on the file's hash;
    the ingest pipeline is meant to be re-runnable and re-OCRing a corpus
    because a later stage crashed would make that painful.
    """
    pdf = Path(pdf)
    binary = find_tesseract()
    if binary is None:
        raise TesseractUnavailableError

    cached: Path | None = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{_cache_key(pdf, lang, dpi)}.txt"
        if cached.exists():
            text = cached.read_text(encoding="utf-8")
            return OcrResult(text=text, pages=text.count("\f") + 1, from_cache=True)

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise RuntimeError("ocr_pdf needs the 'ingest' extra: uv sync --extra ingest") from exc

    doc = pdfium.PdfDocument(str(pdf))
    pages: list[str] = []
    try:
        total = len(doc)
        with tempfile.TemporaryDirectory(prefix="citador-ocr-") as tmp:
            image_path = Path(tmp) / "page.png"
            for index in range(total):
                doc[index].render(scale=dpi / 72).to_pil().save(image_path)
                pages.append(_run_tesseract(binary, image_path, lang=lang, psm=psm))
                if (index + 1) % 20 == 0:
                    log.info("OCR %s: %s/%s páginas", pdf.name, index + 1, total)
    finally:
        doc.close()

    # Form feed between pages, matching what a PDF text extractor would emit.
    text = "\f".join(pages)
    if cached is not None:
        cached.write_text(text, encoding="utf-8")
    return OcrResult(text=text, pages=len(pages), from_cache=False)


def _run_tesseract(binary: Path, image: Path, *, lang: str, psm: str) -> str:
    proc = subprocess.run(
        [str(binary), str(image), "stdout", "-l", lang, "--psm", psm],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        log.warning("tesseract falló en %s: %s", image.name, proc.stderr.strip()[:200])
        return ""
    return proc.stdout
