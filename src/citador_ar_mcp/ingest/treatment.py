"""Classifying *how* a ruling treated the precedent it cited.

This is the part that makes the project a citator rather than a twelfth search
wrapper. The CSJN publishes that A cites B; nobody publishes whether A applied,
distinguished or abandoned B.

Two things shape the design.

**The unit is the clause, not the passage.** Considerando 12 of Arriola cites
three precedents in one sentence, with three different treatments::

    Así en "Colavini" (Fallos: 300:254) se pronunció a favor de la
    criminalización; en "Bazterrica" y "Capalbo", se apartó de tal doctrina
    (Fallos: 308:1392); y en 1990, en "Montalvo" vuelve nuevamente sobre sus
    pasos ... (Fallos: 313:1333)

A classifier that reads the whole passage sees "se apartó de tal doctrina" and
labels all three as abandoned, which is wrong for two of them. So the rules run
against the clause the citation actually sits in, and only fall back to the
wider passage at reduced confidence.

**Two guards, because Spanish legal prose is adversarial by construction.**
A ruling recites the parties' arguments before rejecting them, and it says "no
corresponde apartarse" as often as "corresponde apartarse":

* *negation* -- a negator in front of a formula inverts it, so the rule is
  dropped rather than applied backwards;
* *attribution* -- a clause carrying "el recurrente sostiene", "a juicio del
  Procurador" and the like is somebody else's position, not the Court's, and is
  capped at :data:`ATTRIBUTED_CONFIDENCE`.

Where no rule fires, the answer is ``mentioned`` at low confidence. That is the
instruction, and it is also the only safe default: an
unclassified edge is visible in the detail and cannot move a signal, while a
guessed one silently poisons the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from citador_ar_mcp.domain.treatment import FALLBACK, Method, Polarity, Treatment

#: Confidence attached to the fallback. Below every floor in the project, on
#: purpose: "we found a citation and could not read it" must never move a light.
UNCLASSIFIED_CONFIDENCE: Final = 0.20

#: Ceiling for a formula found in someone else's argument rather than the
#: Court's own reasoning.
ATTRIBUTED_CONFIDENCE: Final = 0.40

#: Multiplier for a formula found outside the citation's own clause. The signal
#: is real but its connection to *this* citation is an inference.
DISTANT_PENALTY: Final = 0.65

#: How far around the citation the local clause may reach before we treat the
#: match as distant.
CLAUSE_RADIUS: Final = 260


@dataclass(frozen=True, slots=True)
class Classification:
    """A treatment, what backs it, and how sure we are."""

    treatment: Treatment
    confidence: float
    method: Method
    rule: str | None = None
    """Name of the formula that fired, for auditing. ``None`` for the fallback."""
    scope: str = "clause"
    """``clause``, ``passage`` or ``fallback`` -- where the evidence was found."""

    @property
    def is_fallback(self) -> bool:
        return self.rule is None


class TreatmentClassifier(Protocol):
    """Anything that can classify a passage. Lets the LLM stage plug in later.

    The rule engine runs first and an implementation is only consulted when the
    rules fall through, so the expensive path is reserved for the passages that
    actually need judgement.
    """

    def classify(self, quote: str, *, cited: str | None = None) -> Classification | None: ...


@dataclass(frozen=True, slots=True)
class Rule:
    """One formula the Court uses, and what it means."""

    treatment: Treatment
    name: str
    pattern: re.Pattern[str]
    confidence: float


def _rule(treatment: Treatment, name: str, source: str, confidence: float) -> Rule:
    return Rule(treatment, name, re.compile(source, re.IGNORECASE), confidence)


# The order matters only for reporting; scoring picks the highest-confidence
# match. Confidences are set so that a departure needs an unambiguous formula:
# ABANDONED is the red light and must be the hardest label to earn.
RULES: Final[tuple[Rule, ...]] = (
    # --- abandoned -------------------------------------------------------
    _rule(
        Treatment.ABANDONED,
        "apartarse-de-la-doctrina",
        r"(?:corresponde|cabe|procede)\s+apartarse\s+de(?:\s+la\s+doctrina)?",
        0.90,
    ),
    _rule(Treatment.ABANDONED, "se-aparto-de-tal-doctrina", r"se\s+apart[óo]\s+de\s+tal", 0.88),
    _rule(
        Treatment.ABANDONED,
        "abandonar-la-doctrina",
        r"(?:abandonar|dejar\s+sin\s+efecto|revisar)\s+(?:la\s+)?"
        r"(?:doctrina|criterio|jurisprudencia)",
        0.85,
    ),
    _rule(
        Treatment.ABANDONED,
        "no-es-compatible-con-el-criterio",
        r"no\s+(?:es|resulta)\s+compatible[^.;]{0,60}(?:criterio|doctrina|voto)",
        0.82,
    ),
    _rule(
        Treatment.ABANDONED,
        "modificar-la-doctrina",
        r"(?:modificar|rectificar|variar)\s+(?:el\s+criterio|la\s+doctrina)",
        0.82,
    ),
    _rule(
        Treatment.ABANDONED,
        "no-comparte-la-doctrina",
        r"no\s+(?:comparte|comparten)[^.;]{0,50}(?:doctrina|criterio|conclusi[óo]n)",
        0.78,
    ),
    # --- followed --------------------------------------------------------
    _rule(
        Treatment.FOLLOWED,
        "resueltas-acertadamente-en",
        r"(?:han\s+sido|fueron)\s+resueltas?\s+acertadamente\s+en",
        0.88,
    ),
    _rule(
        Treatment.FOLLOWED,
        "remision-a-lo-resuelto",
        r"(?:cabe|corresponde)\s+(?:remitirse|estar)\s+a\s+lo\s+(?:resuelto|decidido)",
        0.85,
    ),
    _rule(
        Treatment.FOLLOWED,
        "doctrina-que-el-tribunal-comparte",
        r"doctrina\s+que\s+(?:el\s+Tribunal|esta\s+Corte)\s+(?:comparte|reitera)",
        0.85,
    ),
    _rule(
        Treatment.FOLLOWED,
        "conforme-la-doctrina-de",
        r"(?:de\s+conformidad\s+con|conforme|con\s+arreglo\s+a)\s+(?:la\s+|el\s+)?"
        r"(?:doctrina|criterio|precedente)",
        0.78,
    ),
    _rule(Treatment.FOLLOWED, "en-igual-sentido", r"en\s+(?:igual|el\s+mismo)\s+sentido", 0.72),
    _rule(
        Treatment.FOLLOWED,
        "tal-como-resolvio-el-tribunal",
        r"(?:tal\s+como|conforme|según)\s+(?:lo\s+)?"
        r"(?:expuso|sostuvo|señaló|resolvió|decidió|estableció)\s+"
        r"(?:el\s+Tribunal|esta\s+Corte|la\s+Corte)",
        0.80,
    ),
    # --- applied ---------------------------------------------------------
    _rule(
        Treatment.APPLIED,
        "doctrina-de",
        # `(doctrina de Fallos: 320:1272)` invokes that ruling's holding as
        # governing, which is what APPLIED means in this vocabulary. Kept at
        # moderate confidence: it is a real signal but a generic one, and it
        # appears in about 4% of passages sampled from tomo 332.
        r"doctrina\s+(?:de|del)\b(?!\s*(?:la\s+)?(?:arbitrariedad|real\s+malicia))",
        0.70,
    ),
    # --- applied ---------------------------------------------------------
    _rule(
        Treatment.APPLIED,
        "corresponde-aplicar-el-criterio",
        r"(?:corresponde|cabe)\s+aplicar\s+(?:el\s+criterio|la\s+doctrina|lo\s+resuelto)",
        0.85,
    ),
    _rule(
        Treatment.APPLIED,
        "resulta-aplicable-la-doctrina",
        r"(?:resulta|es)\s+(?:de\s+)?aplicaci[óo]n|resulta\s+aplicable\s+(?:la\s+doctrina|el)",
        0.75,
    ),
    _rule(
        Treatment.APPLIED,
        "precedente-invocado",
        # `en el precedente de Fallos: 308:789, este Tribunal sostuvo que...`
        # The commonest way the Court reaches for a holding and uses it, 83
        # occurrences in the mined sample. The optional adjectives ("tradicional",
        # "citado") and the publication wording ("publicado en", "registrado en")
        # are variants of the same move.
        r"(?:en\s+el|el|del|al)\s+(?:tradicional\s+|citado\s+|conocido\s+|mencionado\s+)?"
        r"precedentes?\s+(?:publicado\s+en\s+|registrado\s+en\s+|de\s+)?",
        0.70,
    ),
    _rule(
        Treatment.APPLIED,
        "doctrina-sentada-por-la-corte",
        # `en abierta contradicción a la doctrina sentada por esta Corte
        # (Fallos: ...)` -- the Court affirming its own line against a lower
        # court that departed from it.
        r"doctrina\s+sentada\s+por\s+(?:esta\s+Corte|el\s+Tribunal|este\s+Tribunal)",
        0.75,
    ),
    _rule(
        Treatment.APPLIED,
        "obliga-la-doctrina",
        r"obliga\s+la\s+doctrina",
        0.78,
    ),
    # --- distinguished ---------------------------------------------------
    _rule(
        Treatment.DISTINGUISHED,
        "no-resulta-aplicable-al-caso",
        r"no\s+(?:resulta|es)\s+aplicable[^.;]{0,60}(?:al\s+caso|sub\s+lite|autos)",
        0.80,
    ),
    _rule(
        Treatment.DISTINGUISHED,
        "circunstancias-distintas",
        r"(?:a\s+diferencia\s+de|no\s+guarda\s+analog[íi]a|"
        r"(?:las\s+)?circunstancias[^.;]{0,40}(?:difieren|distintas))",
        0.75,
    ),
    # --- limited ---------------------------------------------------------
    _rule(
        Treatment.LIMITED,
        "limitar-el-alcance",
        r"(?:limitar|circunscribir|restringir|acotar)\s+(?:el\s+alcance|los\s+alcances)",
        0.80,
    ),
    _rule(
        Treatment.LIMITED,
        "no-resulta-aplicable-cuando",
        # `dicho criterio no resulta aplicable cuando la información no se
        # refiere a funcionarios públicos` -- narrowing the precedent's reach
        # rather than departing from it. The one genuine narrowing found in
        # 9.168 mined passages.
        r"no\s+(?:resulta|es)\s+aplicable\s+cuando",
        0.75,
    ),
    _rule(
        Treatment.LIMITED,
        "solo-resulta-aplicable",
        r"s[óo]lo\s+(?:resulta|es)\s+aplicable",
        0.72,
    ),
    # --- criticized ------------------------------------------------------
    _rule(
        Treatment.CRITICIZED,
        "objetable",
        r"(?:resulta|es)\s+(?:objetable|criticable)|ha\s+sido\s+objeto\s+de\s+cr[íi]ticas",
        0.72,
    ),
)

#: A negator in front of a formula inverts it. `no corresponde apartarse de la
#: doctrina` is the opposite of a departure, and a rule that fires on it is
#: worse than no rule at all.
_NEGATORS: Final = re.compile(
    r"\b(?:no|nunca|jam[áa]s|tampoco|sin\s+que\s+corresponda)\s+(?:\w+\s+){0,3}$",
    re.IGNORECASE,
)

#: Rules whose own wording already contains the negation. Excluded from the
#: negation guard or it would cancel them out.
_INHERENTLY_NEGATIVE: Final = frozenset(
    {
        "no-es-compatible-con-el-criterio",
        "no-comparte-la-doctrina",
        "no-resulta-aplicable-al-caso",
        "circunstancias-distintas",
    }
)

#: Historical recital: the clause reports what an *earlier* Court did, rather
#: than what this one is doing.
#:
#: The discriminator is grammatical. In a real departure the precedent is the
#: **object** of the verb -- "corresponde apartarse *de la doctrina de* Fallos:
#: 308:1392". In a recital it is the **locus** -- "*en* 'Bazterrica' y 'Capalbo',
#: se apartó de tal doctrina", where the thing being departed from is Colavini
#: and the departing court is the one sitting in 1986.
#:
#: Without this guard the pipeline reads considerando 12 of Arriola as Arriola
#: abandoning Bazterrica, which is the exact inversion of what happened: Arriola
#: restored Bazterrica. It produced a confident false red on the first real run.
_RECITAL: Final = re.compile(
    r"^\W{0,4}(?:as[íi]\s+|y\s+|luego\s+|despu[ée]s\s+|posteriormente\s+){0,2}"
    r"(?:en\s+\d{4}\s*,?\s+)?"
    r"en\s+(?:el\s+)?(?:caso\s+|precedente\s+|autos\s+|re\s+|la\s+causa\s+)?"
    r"[\"“'«]",
    re.IGNORECASE,
)

#: Marks that the clause states somebody else's position: a party's argument, or
#: the Procurador's opinion, not the Court's holding.
_ATTRIBUTION: Final = re.compile(
    r"\b(?:el\s+)?(?:recurrente|apelante|actor|demandad[oa]|quejoso|"
    r"a\s+juicio\s+del\s+procurador|el\s+procurador|"
    r"(?:sostiene|alega|invoca|afirma|aduce|argumenta)\s+(?:el|la|que\s+el))\b",
    re.IGNORECASE,
)

#: Clause boundaries in Spanish legal prose. Semicolons separate the parallel
#: propositions the Court strings together; the connectives introduce a change
#: of subject that usually means a different precedent is being discussed.
#:
#: A full stop only counts when a capital or digit follows it. Legal Spanish is
#: full of mid-sentence periods -- "art.", "consid.", "inc.", "in re" citations
#: -- and OCR adds more of them by reading commas as points. Breaking a clause at
#: every dot would shred the sentence the rules are meant to read.
_CLAUSE_SPLIT: Final = re.compile(
    r"(?:;|\.(?=\s*[A-ZÁÉÍÓÚÑ0-9])"
    r"|\sy\s+en\s|\spero\s|\ssin\s+embargo\s|\sno\s+obstante\s|\smientras\s+que\s)",
    re.IGNORECASE,
)

#: Length-preserving repair of periods that are not sentence ends, so a rule can
#: match across them. Applied only to the text the rules read; the stored quote
#: stays exactly as the source printed it, because the quote is the evidence.
#: Length preservation matters: ``citation_position`` indexes into the original.
_SOFT_PERIOD: Final = re.compile(r"\.(?=\s*[a-záéíóúñü])")


def _normalize_for_matching(text: str) -> str:
    """Turn non-sentence periods into commas. Same length, same offsets."""
    return _SOFT_PERIOD.sub(",", text)


def local_clause(text: str, position: int) -> str:
    """The clause containing ``position``.

    Bounded by :data:`CLAUSE_RADIUS` so a passage with no punctuation cannot
    quietly turn into a whole-passage match.
    """
    lo = max(0, position - CLAUSE_RADIUS)
    hi = min(len(text), position + CLAUSE_RADIUS)

    start = lo
    for before in _CLAUSE_SPLIT.finditer(text, lo, position):
        start = before.end()

    end = hi
    after = _CLAUSE_SPLIT.search(text, position, hi)
    if after is not None:
        end = after.start()
    return text[start:end].strip()


def _best_match(text: str) -> tuple[Rule, re.Match[str]] | None:
    """Highest-confidence rule that fires on ``text``, negation guard applied."""
    best: tuple[Rule, re.Match[str]] | None = None
    for rule in RULES:
        m = rule.pattern.search(text)
        if m is None:
            continue
        if rule.name not in _INHERENTLY_NEGATIVE and _NEGATORS.search(text[: m.start()]):
            continue
        if best is None or rule.confidence > best[0].confidence:
            best = (rule, m)
    return best


def classify_passage(
    quote: str,
    *,
    citation_position: int | None = None,
    llm: TreatmentClassifier | None = None,
    cited: str | None = None,
) -> Classification:
    """Classify how the citing ruling treated the precedent cited in ``quote``.

    ``citation_position`` is the offset of the citation *within the quote*. Pass
    it whenever it is known: without it the whole passage is treated as one
    clause, which is exactly the failure mode described in the module docstring.

    ``llm`` is consulted only when the rules fall through, so the expensive path
    runs on the passages that actually need judgement.
    """
    if not quote.strip():
        return Classification(FALLBACK, UNCLASSIFIED_CONFIDENCE, Method.RULE, scope="fallback")

    normalized = _normalize_for_matching(quote)

    # `whole` and `passage` both mean "matched against the entire quote", but
    # they are not the same claim. `whole` is the caller saying "this quote is
    # about this citation, read all of it"; `passage` is us having failed to find
    # anything in the clause and widened on our own. Only the second is an
    # inference, and only the second is penalised.
    if citation_position is None:
        haystack, scope, widened = normalized, "whole", False
    else:
        haystack, scope, widened = local_clause(normalized, citation_position), "clause", False

    found = _best_match(haystack)
    if found is None and scope == "clause":
        found = _best_match(normalized)
        haystack, scope, widened = normalized, "passage", True

    if found is not None and widened and _is_negative(found[0].treatment):
        # A departure has to be stated where the citation is, not somewhere else
        # in the paragraph. Considerando 12 of Arriola is the case in point: it
        # says "se apartó de tal doctrina" about Bazterrica while merely
        # narrating Colavini two clauses earlier, and a passage-wide match paints
        # both red. ABANDONED is the red light and never earns itself by
        # proximity.
        found = None

    if found is None:
        if llm is not None:
            guess = llm.classify(quote, cited=cited)
            if guess is not None:
                return guess
        return Classification(FALLBACK, UNCLASSIFIED_CONFIDENCE, Method.RULE, scope="fallback")

    rule, _match = found
    confidence = rule.confidence
    if widened:
        confidence *= DISTANT_PENALTY

    if _RECITAL.match(haystack):
        # The clause narrates what an earlier Court did. Whatever verb it
        # contains belongs to that court, not to this one.
        return Classification(
            FALLBACK,
            UNCLASSIFIED_CONFIDENCE,
            Method.RULE,
            rule=f"{rule.name}(relato-historico)",
            scope=scope,
        )

    if _ATTRIBUTION.search(haystack):
        # Somebody else's argument. Keep the finding visible but strip its force.
        return Classification(
            FALLBACK,
            min(confidence, ATTRIBUTED_CONFIDENCE),
            Method.RULE,
            rule=f"{rule.name}(atribuido)",
            scope=scope,
        )

    return Classification(rule.treatment, round(confidence, 3), Method.RULE, rule.name, scope)


def _is_negative(treatment: Treatment) -> bool:
    """Whether a label would move the signal towards red."""
    return treatment.polarity is Polarity.NEGATIVE
