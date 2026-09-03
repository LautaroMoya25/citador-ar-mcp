"""The treatment vocabulary: how a later ruling treated an earlier one.

Adapted from citator practice (Shepard's, KeyCite, BCite) to Argentine terms.
The vocabulary is deliberately small. Every extra label is another way for the
classifier to be confidently wrong, and a citator that says "still good law"
about an abandoned precedent is worse than no citator at all.

Two rules govern this module and are enforced by the tests:

1. **``ABANDONED`` is the red light and must be the most conservative label.**
   When in doubt the answer is ``MENTIONED`` with low confidence, never
   ``ABANDONED``.
2. **A treatment written in a dissent is not the doctrine of the Court.**
   Every treatment therefore carries the :class:`Opinion` it was written in, and
   anything that is not the majority is excluded from the aggregate signal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Treatment(StrEnum):
    """How the citing ruling treated the cited one."""

    APPLIED = "applied"
    FOLLOWED = "followed"
    DISTINGUISHED = "distinguished"
    LIMITED = "limited"
    CRITICIZED = "criticized"
    ABANDONED = "abandoned"
    MENTIONED = "mentioned"

    @property
    def polarity(self) -> Polarity:
        """Which direction this treatment pushes the aggregate signal."""
        return _POLARITY[self]

    @property
    def label_es(self) -> str:
        """Spanish label, for markdown output. The domain language is Spanish."""
        return _LABEL_ES[self]

    @property
    def description_es(self) -> str:
        return _DESCRIPTION_ES[self]

    @property
    def severity(self) -> int:
        """Ordering for "worst treatment first". Higher is worse for the precedent."""
        return _SEVERITY[self]


class Polarity(StrEnum):
    """The effect of a treatment on the standing of the cited ruling."""

    POSITIVE = "positive"
    """The precedent was used as good law."""

    NEUTRAL = "neutral"
    """Cited without taking a position."""

    CAUTIONARY = "cautionary"
    """Still good law, but narrowed or questioned."""

    NEGATIVE = "negative"
    """The Court departed from it."""


class Opinion(StrEnum):
    """Which part of the ruling the treatment was written in.

    The CSJN publishes majority, concurring, dissenting and partially dissenting
    opinions in one document. Only :attr:`MAJORITY` binds.
    """

    MAJORITY = "majority"
    """Voto de la mayoria."""

    CONCURRENCE = "concurrence"
    """Voto propio o concurrente."""

    DISSENT = "dissent"
    """Disidencia."""

    PARTIAL_DISSENT = "partial_dissent"
    """Disidencia parcial."""

    DICTAMEN = "dictamen"
    """Dictamen del Procurador General.

    Printed inside the Fallos volume ahead of the ruling, which makes it easy to
    mistake for the Court's own text. It is an opinion offered *to* the Court,
    which is free to ignore it -- and in Bazterrica did exactly that. Anything
    cited here is non-binding for the same reason a dissent is.
    """

    UNKNOWN = "unknown"
    """Could not be determined. Treated as non-binding, like a dissent."""

    @property
    def is_binding(self) -> bool:
        """Whether a treatment in this opinion states the doctrine of the Court."""
        return self is Opinion.MAJORITY

    @property
    def label_es(self) -> str:
        return _OPINION_LABEL_ES[self]


class Method(StrEnum):
    """How a treatment was determined. Stored so results stay auditable."""

    RULE = "rule"
    """Matched a fixed formula used by the Court."""

    LLM = "llm"
    """Classified by a model over the surrounding passage."""

    MANUAL = "manual"
    """Annotated by hand. Used by the golden fixture."""


_POLARITY: Final[dict[Treatment, Polarity]] = {
    Treatment.APPLIED: Polarity.POSITIVE,
    Treatment.FOLLOWED: Polarity.POSITIVE,
    Treatment.MENTIONED: Polarity.NEUTRAL,
    Treatment.DISTINGUISHED: Polarity.CAUTIONARY,
    Treatment.LIMITED: Polarity.CAUTIONARY,
    Treatment.CRITICIZED: Polarity.CAUTIONARY,
    Treatment.ABANDONED: Polarity.NEGATIVE,
}

_SEVERITY: Final[dict[Treatment, int]] = {
    Treatment.APPLIED: 0,
    Treatment.FOLLOWED: 0,
    Treatment.MENTIONED: 1,
    Treatment.DISTINGUISHED: 2,
    Treatment.LIMITED: 3,
    Treatment.CRITICIZED: 4,
    Treatment.ABANDONED: 5,
}

_LABEL_ES: Final[dict[Treatment, str]] = {
    Treatment.APPLIED: "aplicado",
    Treatment.FOLLOWED: "seguido",
    Treatment.DISTINGUISHED: "distinguido",
    Treatment.LIMITED: "limitado",
    Treatment.CRITICIZED: "criticado",
    Treatment.ABANDONED: "abandonado",
    Treatment.MENTIONED: "mencionado",
}

_DESCRIPTION_ES: Final[dict[Treatment, str]] = {
    Treatment.APPLIED: "Lo aplica como fundamento de la decision.",
    Treatment.FOLLOWED: "Lo sigue expresamente.",
    Treatment.DISTINGUISHED: "Lo reconoce pero lo aparta por los hechos del caso.",
    Treatment.LIMITED: "Restringe su alcance.",
    Treatment.CRITICIZED: "Lo cuestiona sin abandonarlo.",
    Treatment.ABANDONED: "Se aparta de la doctrina anterior.",
    Treatment.MENTIONED: "Lo cita sin tomar postura.",
}

_OPINION_LABEL_ES: Final[dict[Opinion, str]] = {
    Opinion.MAJORITY: "mayoria",
    Opinion.CONCURRENCE: "voto propio",
    Opinion.DISSENT: "disidencia",
    Opinion.PARTIAL_DISSENT: "disidencia parcial",
    Opinion.DICTAMEN: "dictamen del Procurador",
    Opinion.UNKNOWN: "indeterminado",
}

#: The label to fall back to when the evidence does not support anything
#: stronger. Paired with a low confidence by the classifier.
FALLBACK: Final = Treatment.MENTIONED

#: Minimum confidence required before a negative treatment is allowed to drive
#: the aggregate signal. Below this, ``ABANDONED`` still appears in the detail
#: but does not turn the light red on its own. Deliberately high: the cost of a
#: false red is a lawyer discarding good law.
NEGATIVE_CONFIDENCE_FLOOR: Final = 0.75
