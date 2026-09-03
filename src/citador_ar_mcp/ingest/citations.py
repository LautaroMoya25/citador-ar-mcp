"""Finding citations in the body of a ruling, and attributing each one to a vote.

Two jobs, in order.

**Page furniture.** CSJN PDFs break pages in the middle of words using a ``-//-``
continuation marker, with the page footer, docket number, caption and next page
number sitting between the two halves::

    ... ES COPIA VO-//- -42- A. 891. XLIV. RECURSO DE HECHO Arriola,
    Sebastián y otros s/ causa n 9080. -43- -//-TO DEL SEÑOR MINISTRO ...

Read literally that is ``VO`` and ``TO``; joined, it is the heading ``VOTO DEL
SEÑOR MINISTRO``. Every heading in a multi-vote ruling lands on a page boundary,
so a parser that does not rejoin them finds no headings at all and silently
attributes the whole document to the majority. :func:`join_page_breaks` fixes
this before anything else runs.

**Vote attribution.** *A citation in a dissent is not the
doctrine of the tribunal; mark it or the citator lies.* The CSJN prints the
majority first and then each separate opinion under its own heading, so the
opinion a citation belongs to is the heading that most recently preceded it.
Where no heading precedes it, it is the majority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from citador_ar_mcp.domain.citation import RawCitation, RulingId, find_fallos_citations
from citador_ar_mcp.domain.treatment import Opinion

#: Page furniture between two `-//-` markers: footer, docket, caption, page number.
#: Bounded so a missing closing marker cannot swallow the rest of the document.
_PAGE_BREAK: Final = re.compile(r"-//-.{0,400}?-//-", re.DOTALL)

#: Leftover single markers, once the paired ones are gone.
_STRAY_BREAK: Final = re.compile(r"-//-")

#: `-NN-` page numbers left behind on their own.
_PAGE_NUMBER: Final = re.compile(r"(?<!\w)-\d{1,4}-(?!\w)")

_HEADING: Final = re.compile(
    r"(?P<kind>VOTO|DISIDENCIA\s+PARCIAL|DISIDENCIA)\s+"
    r"(?:DE\s+L[AO]S?|DEL|DE)\s+"
    r"(?:SEÑOR(?:A|ES)?|SEÑORAS?)\s+"
    r"(?:PRESIDENTE|VICEPRESIDENTE|MINISTR[OA]S?|JUE[CZ]E?S?)"
    r"(?P<author>.{0,180}?)"
    r"Considerando",
    re.IGNORECASE | re.DOTALL,
)

# The printed Fallos volumes -- which is what the old rulings are scans of --
# carry the Procurador General's opinion *before* the Court's, under its own
# heading. It reads exactly like the ruling and sits in the same document, so a
# parser that ignores it attributes the Procurador's citations to the Court.
# In Bazterrica that would be a straight inversion: the Procurador argued for
# upholding Colavini and the Court went the other way.
_DICTAMEN: Final = re.compile(
    r"DICTAMEN\s+DEL\s+PROCURADOR\s+(?:GENERAL|FISCAL)[^\n]{0,120}",
    re.IGNORECASE,
)

#: Where the Court's own text starts again after a dictamen.
_FALLO: Final = re.compile(
    r"(?:FALLO\s+DE\s+LA\s+CORTE\s+SUPREMA|"
    r"Buenos\s+Aires,\s+\d{1,2}\s+de\s+\w+\s+de\s+(?:19|20)\d\d\s*\.?\s*Vistos)",
    re.IGNORECASE,
)

_KIND_TO_OPINION: Final[dict[str, Opinion]] = {
    "voto": Opinion.CONCURRENCE,
    "disidencia": Opinion.DISSENT,
    "disidencia parcial": Opinion.PARTIAL_DISSENT,
}

#: How much text around a citation to keep as the auditable quote. Wide enough
#: to contain the verb that carries the treatment ("se aparta de", "corresponde
#: aplicar"), narrow enough that a reader can check it at a glance.
QUOTE_BEFORE: Final = 320
QUOTE_AFTER: Final = 200

#: How far back from the end of a quote to look for a sentence break.
_TAIL_WINDOW: Final = 160


@dataclass(frozen=True, slots=True)
class OpinionSpan:
    """A contiguous stretch of the ruling written by one set of judges."""

    start: int
    end: int
    opinion: Opinion
    author: str


@dataclass(frozen=True, slots=True)
class FoundCitation:
    """A citation, the passage that contains it, and the vote it belongs to."""

    cited: RulingId
    raw: str
    quote: str
    opinion: Opinion
    author: str
    offset: int


def join_page_breaks(text: str) -> str:
    """Remove page furniture and rejoin words split across page breaks."""
    joined = _PAGE_BREAK.sub("", text)
    joined = _STRAY_BREAK.sub("", joined)
    joined = _PAGE_NUMBER.sub(" ", joined)
    return re.sub(r"[ \t]{2,}", " ", joined)


def split_opinions(text: str) -> list[OpinionSpan]:
    """Segment a ruling into majority and separate opinions.

    Always returns at least one span. Everything before the first heading is the
    majority; a ruling with no headings is entirely majority.
    """
    heads: list[tuple[int, Opinion, str]] = []
    for m in _HEADING.finditer(text):
        kind = " ".join(m.group("kind").lower().split())
        opinion = _KIND_TO_OPINION.get(kind, Opinion.UNKNOWN)
        author = " ".join(m.group("author").split())
        author = re.sub(r"^(?:DOCTOR(?:A|ES|AS)?\s+)?(?:DO[ÑN]A?\s+)?", "", author, flags=re.I)
        heads.append((m.start(), opinion, author.strip(" .,-")))

    heads += [(m.start(), Opinion.DICTAMEN, "Procurador General") for m in _DICTAMEN.finditer(text)]
    heads += [(m.start(), Opinion.MAJORITY, "") for m in _FALLO.finditer(text)]
    heads.sort(key=lambda h: h[0])

    if not heads:
        return [OpinionSpan(0, len(text), Opinion.MAJORITY, "")]

    spans = [OpinionSpan(0, heads[0][0], Opinion.MAJORITY, "")]
    for i, (start, opinion, author) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        spans.append(OpinionSpan(start, end, opinion, author))
    return spans


def opinion_at(spans: list[OpinionSpan], offset: int) -> OpinionSpan:
    """The opinion that contains ``offset``."""
    for span in spans:
        if span.start <= offset < span.end:
            return span
    return spans[-1]


def quote_around(text: str, citation: RawCitation) -> str:
    """The passage that justifies a citation, trimmed to sentence-ish boundaries."""
    lo = max(0, citation.start - QUOTE_BEFORE)
    hi = min(len(text), citation.end + QUOTE_AFTER)
    frag = " ".join(text[lo:hi].split())

    # Start at a sentence boundary when there is one close by, so the quote does
    # not open mid-word.
    if lo > 0:
        opening = re.search(r"(?<=[.;:])\s+(?=[A-ZÁÉÍÓÚÑ0-9])", frag[:180])
        if opening:
            frag = frag[opening.end() :]

    # End at the last sentence break in the tail, for the same reason.
    if hi < len(text):
        tail = frag[-_TAIL_WINDOW:]
        breaks = [m.end() for m in re.finditer(r"[.;]", tail)]
        if breaks:
            frag = frag[: len(frag) - len(tail) + breaks[-1]]
    return frag.strip()


def find_citations(text: str, *, exclude: RulingId | None = None) -> list[FoundCitation]:
    """Find every ``Fallos`` citation in a ruling, with its quote and vote.

    ``exclude`` drops self-references: a ruling's own cite appears in its running
    header on every page, and a self-edge is meaningless in the graph.
    """
    spans = split_opinions(text)
    out: list[FoundCitation] = []
    for cite in find_fallos_citations(text):
        if cite.ruling_id is None or cite.ruling_id == exclude:
            continue
        span = opinion_at(spans, cite.start)
        quote = quote_around(text, cite)
        if not quote:
            continue  # no quote, no row
        out.append(
            FoundCitation(
                cited=cite.ruling_id,
                raw=cite.raw,
                quote=quote,
                opinion=span.opinion,
                author=span.author,
                offset=cite.start,
            )
        )
    return out
