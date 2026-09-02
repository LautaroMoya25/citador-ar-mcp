"""Aggregation: N treatments into one signal, without ever losing the detail.

The rule that shapes this module is in CLAUDE.md, section 5: *a ruling can be
abandoned on one point and still good law on another*, so the aggregate can
never be a bare value. :class:`SignalReport` therefore always carries the
per-treatment breakdown and the caveats that qualify it, and the tools are
expected to render both.

Three guards keep the red light honest:

* only treatments written in the **majority** opinion can drive the signal;
* a negative treatment below :data:`~citador_ar_mcp.domain.treatment.NEGATIVE_CONFIDENCE_FLOOR`
  is reported but does not turn the light red by itself;
* when positive and negative treatments coexist, the report says so instead of
  picking a winner.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from citador_ar_mcp.domain.citation import RulingId
from citador_ar_mcp.domain.treatment import (
    NEGATIVE_CONFIDENCE_FLOOR,
    Method,
    Opinion,
    Polarity,
    Treatment,
)


class Signal(StrEnum):
    """The traffic light. ``RED`` means: do not cite this without reading why."""

    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    GRAY = "gray"

    @property
    def label_es(self) -> str:
        return _SIGNAL_LABEL_ES[self]

    @property
    def glyph(self) -> str:
        return _SIGNAL_GLYPH[self]


_SIGNAL_LABEL_ES: Final[dict[Signal, str]] = {
    Signal.RED: "doctrina abandonada",
    Signal.YELLOW: "vigente con reservas",
    Signal.GREEN: "vigente",
    Signal.GRAY: "sin tratamiento registrado",
}

_SIGNAL_GLYPH: Final[dict[Signal, str]] = {
    Signal.RED: "🔴",
    Signal.YELLOW: "🟡",
    Signal.GREEN: "🟢",
    Signal.GRAY: "⚪",
}


class TreatmentRecord(BaseModel):
    """One edge of the graph: how ``citing`` treated the ruling under review.

    ``quote`` is not decorative. Every claim the citator makes has to be
    auditable against the text that produced it, so a record without a passage
    is not a record (CLAUDE.md, section 5).
    """

    model_config = ConfigDict(frozen=True)

    citing: RulingId
    treatment: Treatment
    opinion: Opinion = Opinion.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0)
    quote: str = Field(min_length=1)
    method: Method = Method.RULE
    decided_year: int | None = None

    @property
    def counts_toward_signal(self) -> bool:
        """Only the majority opinion states the doctrine of the Court."""
        return self.opinion.is_binding


class SignalReport(BaseModel):
    """The answer to "is this still good law", with everything needed to check it."""

    model_config = ConfigDict(frozen=True)

    subject: RulingId
    signal: Signal
    confidence: float = Field(ge=0.0, le=1.0)
    total_citing: int = Field(ge=0)
    counts: dict[Treatment, int] = Field(default_factory=dict)
    """Breakdown over every treatment, binding or not."""
    binding_counts: dict[Treatment, int] = Field(default_factory=dict)
    """Breakdown restricted to majority opinions. This is what drove the signal."""
    non_binding_by_opinion: dict[Opinion, int] = Field(default_factory=dict)
    """Why the rest does not count, split by opinion.

    Not decorative. "Written in a dissent" and "we could not tell which vote
    this came from" are different claims, and collapsing them tells the reader
    something about the Court that the data does not support -- which is exactly
    what happens on a freshly crawled corpus, where every edge is unattributed.
    """
    caveats: list[str] = Field(default_factory=list)
    """Plain-Spanish qualifications. Rendered next to the signal, never dropped."""
    evidence: list[TreatmentRecord] = Field(default_factory=list)
    """The records that justify the signal, worst treatment first."""

    @property
    def is_uniform(self) -> bool:
        """False when the ruling was both departed from and relied upon."""
        polarities = {t.polarity for t, n in self.binding_counts.items() if n}
        return not (Polarity.NEGATIVE in polarities and Polarity.POSITIVE in polarities)


def aggregate(
    subject: RulingId,
    records: list[TreatmentRecord],
    *,
    negative_floor: float = NEGATIVE_CONFIDENCE_FLOOR,
    max_evidence: int = 10,
) -> SignalReport:
    """Reduce the treatments a ruling received into a single signal plus its detail.

    ``negative_floor`` is the confidence a negative treatment needs before it can
    turn the light red on its own. Lowering it makes the citator noisier and more
    dangerous; it exists as a parameter so the evals can measure the trade-off,
    not so callers can tune it away.
    """
    counts = Counter(r.treatment for r in records)
    binding = [r for r in records if r.counts_toward_signal]
    binding_counts = Counter(r.treatment for r in binding)

    confident_negative = [
        r
        for r in binding
        if r.treatment.polarity is Polarity.NEGATIVE and r.confidence >= negative_floor
    ]
    cautionary = [r for r in binding if r.treatment.polarity is Polarity.CAUTIONARY]
    positive = [r for r in binding if r.treatment.polarity is Polarity.POSITIVE]

    restoring = _restoring(confident_negative, positive)

    if confident_negative and restoring:
        # Departed from, then restored. See _restoring for why this is yellow
        # rather than red or green.
        signal = Signal.YELLOW
        drivers = restoring
    elif confident_negative:
        signal = Signal.RED
        drivers = confident_negative
    elif cautionary:
        signal = Signal.YELLOW
        drivers = cautionary
    elif positive:
        signal = Signal.GREEN
        drivers = positive
    else:
        signal = Signal.GRAY
        drivers = binding

    caveats = _build_caveats(
        records=records,
        binding=binding,
        binding_counts=binding_counts,
        negative_floor=negative_floor,
        signal=signal,
    )

    confidence = max((r.confidence for r in drivers), default=0.0)
    if signal is Signal.GRAY:
        # "No negative treatment found" is a weak claim when nothing cites it.
        confidence = 0.0 if not records else min(confidence, 0.5)

    evidence = sorted(
        records,
        key=lambda r: (-r.treatment.severity, -r.confidence, -(r.decided_year or 0)),
    )[:max_evidence]

    non_binding = Counter(r.opinion for r in records if not r.counts_toward_signal)

    return SignalReport(
        subject=subject,
        signal=signal,
        confidence=round(confidence, 3),
        total_citing=len({r.citing for r in records}),
        counts=dict(counts),
        binding_counts=dict(binding_counts),
        non_binding_by_opinion=dict(non_binding),
        caveats=caveats,
        evidence=evidence,
    )


def _restoring(
    negative: list[TreatmentRecord], positive: list[TreatmentRecord]
) -> list[TreatmentRecord]:
    """Positive treatments that postdate every confident departure.

    Precedent moves in both directions, and a citator that ignores time gets the
    most interesting cases exactly backwards. Bazterrica was departed from by
    Montalvo in 1990 and returned to by Arriola in 2009: reporting it as dead law
    in 2026 because a 1990 ruling once departed from it would be a false red, and
    a false red costs a lawyer good authority.

    The result is deliberately **yellow, not green**. A precedent that has been
    overturned and restored is live law with a history the reader needs to see,
    and the caveats spell that history out.

    Requires known years on both sides. Where the year is missing we cannot order
    the events, so nothing is restored and the red light stands -- the
    conservative reading.
    """
    if not negative or not positive:
        return []
    negative_years = [r.decided_year for r in negative if r.decided_year]
    if len(negative_years) != len(negative):
        return []
    latest_departure = max(negative_years)
    return [r for r in positive if r.decided_year and r.decided_year > latest_departure]


def _build_caveats(
    *,
    records: list[TreatmentRecord],
    binding: list[TreatmentRecord],
    binding_counts: Counter[Treatment],
    negative_floor: float,
    signal: Signal,
) -> list[str]:
    """Everything the signal alone would hide. Order is stable for the tests."""
    caveats: list[str] = []

    if not records:
        caveats.append(
            "No hay fallos posteriores que lo citen en el corpus. "
            "Ausencia de tratamiento negativo no equivale a vigencia confirmada."
        )
        return caveats

    restored = _restoring(
        [
            r
            for r in binding
            if r.treatment.polarity is Polarity.NEGATIVE and r.confidence >= negative_floor
        ],
        [r for r in binding if r.treatment.polarity is Polarity.POSITIVE],
    )
    if restored:
        newest = max(restored, key=lambda r: r.decided_year or 0)
        caveats.append(
            "El precedente fue abandonado y después retomado: "
            f"{newest.citing.human} ({newest.decided_year}) vuelve a él. "
            "La señal no es roja por eso, pero la historia importa para citarlo: "
            "revisar el detalle antes de apoyarse en él."
        )

    non_binding_negative = [
        r
        for r in records
        if not r.counts_toward_signal and r.treatment.polarity is Polarity.NEGATIVE
    ]
    if non_binding_negative:
        caveats.append(
            f"{len(non_binding_negative)} tratamiento(s) negativo(s) provienen de "
            "disidencias o votos propios. No son doctrina del tribunal y no "
            "determinan la señal."
        )

    weak_negative = [
        r
        for r in binding
        if r.treatment.polarity is Polarity.NEGATIVE and r.confidence < negative_floor
    ]
    if weak_negative:
        caveats.append(
            f"{len(weak_negative)} tratamiento(s) clasificado(s) como negativo(s) "
            f"por debajo del umbral de confianza ({negative_floor:.2f}). "
            "Figuran en el detalle pero no encienden la señal roja."
        )

    polarities = {t.polarity for t, n in binding_counts.items() if n}
    if Polarity.NEGATIVE in polarities and Polarity.POSITIVE in polarities:
        caveats.append(
            "El fallo fue abandonado en algún punto y aplicado en otro. "
            "La vigencia no es uniforme: revisar el detalle por tratamiento."
        )

    if records and not binding:
        caveats.append(
            "Ninguno de los tratamientos pudo atribuirse al voto de la mayoría. "
            "La señal es indeterminada, no positiva."
        )

    if signal is Signal.RED:
        caveats.append(
            "Señal roja: verificar el pasaje citado antes de descartar el precedente. "
            "Esta es una herramienta de investigación, no un dictamen."
        )

    return caveats
