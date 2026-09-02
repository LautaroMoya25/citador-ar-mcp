"""Parsing and normalisation of Argentine citation formats.

The same CSJN precedent is written in at least four ways in the wild::

    Fallos: 332:1963          the canonical collection cite (tomo:pagina)
    CSJN, "Arriola"           the short name a ruling is known by
    A. 891. XLIV. RHE         the docket number (numero de expediente)
    CSJ 001086/2022/CS001     the modern docket number

Only the first is a stable, parseable identifier, which is why the citator is
scoped to the Court (see FASE-0-legal.md, section 4). The other three resolve
through the ``aliases`` table; this module only normalises them into stable
lookup keys.

Nothing here reaches the network or the database. Everything is a pure function
over strings, which is what makes it the most testable piece of the project.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# The collection currently runs to tomo 349 (year 2026), verified against
# https://sj.csjn.gov.ar/homeSJ/totalTomos/ during Fase 0. The ceiling is
# deliberately loose so the parser does not need a release to accept new tomos,
# but it still rejects the four-digit numbers that appear when a page number is
# misread as a volume.
MIN_VOLUME: Final = 1
MAX_VOLUME: Final = 500
MIN_PAGE: Final = 1
MAX_PAGE: Final = 9999


class CitationForm(StrEnum):
    """How a reference was written in the source text."""

    FALLOS = "fallos"
    """Canonical collection cite, ``Fallos: 332:1963``."""

    EXPEDIENTE = "expediente"
    """Docket number, ``A. 891. XLIV`` or ``CSJ 001086/2022/CS001``."""

    SHORT_NAME = "short_name"
    """The name a ruling is known by, ``Arriola``."""

    CAPTION = "caption"
    """A full or partial caratula."""


class RulingId(BaseModel):
    """The canonical identifier of a CSJN ruling: a position in the Fallos collection.

    Rendered as ``fallos:<tomo>:<pagina>``, which is the primary key of the
    ``rulings`` table.
    """

    model_config = ConfigDict(frozen=True)

    volume: int = Field(ge=MIN_VOLUME, le=MAX_VOLUME, description="tomo")
    page: int = Field(ge=MIN_PAGE, le=MAX_PAGE, description="pagina")

    def __str__(self) -> str:
        return f"fallos:{self.volume}:{self.page}"

    @property
    def human(self) -> str:
        """The way a lawyer would write it."""
        return f"Fallos: {self.volume}:{self.page}"

    @classmethod
    def parse(cls, raw: str) -> RulingId | None:
        """Parse a single reference. Returns ``None`` rather than raising.

        Accepts ``fallos:332:1963``, ``Fallos: 332:1963``, ``Fallos 332:1963``
        and the bare ``332:1963``. Refuses anything it is not sure about; a
        wrong node is worse than a missing one.
        """
        m = _SINGLE_REF.fullmatch(raw.strip())
        if m is None:
            return None
        return cls.build(int(m.group("volume")), int(m.group("page")))

    @classmethod
    def build(cls, volume: int, page: int) -> RulingId | None:
        """Construct if the pair is in range, otherwise ``None``."""
        if not (MIN_VOLUME <= volume <= MAX_VOLUME and MIN_PAGE <= page <= MAX_PAGE):
            return None
        return cls(volume=volume, page=page)


class RawCitation(BaseModel):
    """A reference as it was found in a text, with the span that produced it.

    ``start``/``end`` are offsets into the text the citation was found in. They
    exist so that every edge in the graph can be traced back to the characters
    that justify it: no quote, no row (CLAUDE.md, section 5).
    """

    model_config = ConfigDict(frozen=True)

    raw: str
    form: CitationForm
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    ruling_id: RulingId | None = None
    """Resolved only for :attr:`CitationForm.FALLOS`. The other forms need the
    ``aliases`` table, which lives in ``store/``, not here."""


# ---------------------------------------------------------------------------
# Fallos references
# ---------------------------------------------------------------------------

_SINGLE_REF = re.compile(
    r"(?:fallos\s*:?\s*)?(?P<volume>\d{1,3})\s*:\s*(?P<page>\d{1,4})",
    re.IGNORECASE,
)

# A "Fallos:" marker followed by a run of references. The run is captured whole
# and split afterwards, because a comma means two different things depending on
# what follows it -- see _split_run.
_FALLOS_RUN = re.compile(
    r"Fallos\s*:?\s*"
    r"(?P<run>\d{1,3}\s*:\s*\d{1,4}"
    # The continuation must allow four digits: a pinpoint page (`, 2699`) is as
    # wide as a page number, and capping it at three silently truncates the run.
    r"(?:\s*(?:,|;|\sy\s|\se\s)\s*\d{1,4}(?:\s*:\s*\d{1,4})?)*)",
    re.IGNORECASE,
)

# Inside a run: either a full `volume:page` or a bare number (a pinpoint page).
_RUN_ITEM = re.compile(r"(?P<volume>\d{1,3})\s*:\s*(?P<page>\d{1,4})|(?P<pinpoint>\d{1,4})")


def find_fallos_citations(text: str) -> list[RawCitation]:
    """Find every canonical ``Fallos`` reference in ``text``.

    Handles the two shapes that trip up naive regexes:

    * **pinpoints** -- ``Fallos: 331:2691, 2699`` is *one* ruling cited at a
      specific page, not two rulings.
    * **runs** -- ``Fallos: 301:341; 302:1284 y 303:1029`` is three rulings
      under a single ``Fallos:`` marker.

    Returns citations in order of appearance. A run yields one citation per
    ``volume:page`` pair; bare pinpoint pages are dropped.
    """
    out: list[RawCitation] = []
    for m in _FALLOS_RUN.finditer(text):
        run = m.group("run")
        base = m.start("run")
        for raw, start, end, volume, page in _split_run(run, base):
            rid = RulingId.build(volume, page)
            if rid is None:
                continue
            out.append(
                RawCitation(
                    raw=raw,
                    form=CitationForm.FALLOS,
                    start=start,
                    end=end,
                    ruling_id=rid,
                )
            )
    return _dedupe_spans(out)


def _split_run(run: str, base: int) -> list[tuple[str, int, int, int, int]]:
    """Split a reference run into ``(raw, start, end, volume, page)`` tuples.

    A bare number inside a run is a pinpoint page and is dropped: it refers to a
    location inside the ruling already named, not to a new one.
    """
    items: list[tuple[str, int, int, int, int]] = []
    for m in _RUN_ITEM.finditer(run):
        if m.group("pinpoint") is not None:
            continue  # pinpoint page of the preceding ruling
        volume = int(m.group("volume"))
        page = int(m.group("page"))
        items.append((m.group(0), base + m.start(), base + m.end(), volume, page))
    return items


def _dedupe_spans(cites: list[RawCitation]) -> list[RawCitation]:
    """Drop repeated spans, keeping order of appearance."""
    out: list[RawCitation] = []
    seen: set[tuple[int, int]] = set()
    for c in cites:
        key = (c.start, c.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Expedientes
# ---------------------------------------------------------------------------

# Classic form, used until the mid-2010s: `A. 891. XLIV` plus an optional
# recurso suffix (RHE, REX, RHF...). The letter is the appellant's initial.
_EXPEDIENTE_CLASSIC = re.compile(
    r"(?P<letter>[A-Z])\.?\s*(?P<number>\d{1,4})\.?\s*"
    r"(?P<roman>[IVXLCDM]{1,8})\.?(?:\s*(?P<suffix>[A-Z]{2,4})\.?)?"
)

# Modern form, post acordada 4/2013: `CSJ 001086/2022/CS001`, `CNE 006781/2017/CS001`.
_EXPEDIENTE_MODERN = re.compile(
    r"(?P<court>[A-Z]{2,4})\s*(?P<number>\d{3,7})\s*/\s*(?P<year>\d{4})"
    r"(?:\s*/\s*(?P<sub>[A-Z0-9]{2,8}))?"
)

# The API returns the tomo in roman numerals in `numeroExpediente` for old
# rulings that never had a docket number -- `CCC.` for tomo 300. That is not an
# expediente and must never be stored as an alias.
_ROMAN_ONLY = re.compile(r"^[IVXLCDM]{1,8}\.?$")


def normalize_expediente(raw: str) -> str | None:
    """Normalise a docket number into a stable alias key.

    ``A. 891. XLIV. RHE`` and ``A.891.XLIV`` both become ``A.891.XLIV``; the
    recurso suffix is dropped because it is not part of the identity of the
    case. ``CSJ 001086/2022/CS001`` becomes ``CSJ.1086/2022/CS001``.

    Returns ``None`` for input that is not a docket number, including the
    roman-numeral placeholder the CSJN API returns for pre-1990 rulings.
    """
    s = " ".join(raw.strip().split())
    if not s or _ROMAN_ONLY.match(s):
        return None

    m = _EXPEDIENTE_MODERN.fullmatch(s)
    if m:
        key = f"{m.group('court')}.{int(m.group('number'))}/{m.group('year')}"
        if m.group("sub"):
            key += f"/{m.group('sub')}"
        return key

    m = _EXPEDIENTE_CLASSIC.fullmatch(s)
    if m:
        return f"{m.group('letter')}.{int(m.group('number'))}.{m.group('roman')}"

    return None


# ---------------------------------------------------------------------------
# Caratulas and short names
# ---------------------------------------------------------------------------

# The slash forms carry no trailing \b on purpose. `s/CAUSA` has a boundary
# after the slash but `s/ causa` does not, and requiring one drops the `s` from
# half the transcriptions -- which is exactly the variation this is meant to
# absorb.
_NOISE = re.compile(
    r"(?:\bs/|\bc/|\by\s+otros?\b|\by\s+otras?\b|\bs\.?a\.?\b|\bs\.?r\.?l\.?\b|"
    r"\bcausa\s+n[°º]?\s*\d+\b|\brecurso\s+de\s+hecho\b|\bexpte\.?)",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def strip_accents(s: str) -> str:
    """Fold accents, so that ``Asociacion`` and ``Asociación`` are one key."""
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def normalize_caption(raw: str) -> str:
    """Normalise a caratula into an alias lookup key.

    Uppercases, folds accents, drops punctuation and the boilerplate that varies
    between transcriptions (``s/``, ``c/``, ``y otros``, ``causa n 9080``), so
    that the CSJN's ``ARRIOLA SEBASTIAN Y OTROS s/CAUSA N 9080`` and a lawyer's
    ``Arriola, Sebastian y otros s/ causa n 9080.`` collapse to one key.
    """
    s = strip_accents(raw).upper()
    s = _NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


def short_name_key(raw: str) -> str:
    """Normalise the name a ruling is known by, ``"Arriola"`` -> ``ARRIOLA``."""
    return normalize_caption(raw.strip().strip("\"'“”"))
