"""The LLM stage of treatment classification.

The rules in ``ingest/treatment.py`` read fixed formulas. What they cannot read
is the case that needs judgement across clauses -- and the golden fixture has
one. Considerando 12 of Arriola says::

    ... y en 1990, en "Montalvo" vuelve nuevamente sobre sus pasos a favor de la
    criminalización ... (Fallos: 313:1333), y como lo adelantáramos en las
    consideraciones previas, hoy el Tribunal decide volver a "Bazterrica".

Departing from Montalvo is stated nowhere. It follows from returning to the
precedent Montalvo had displaced, which is an inference over the whole
paragraph. A regex that caught it would catch a hundred things that are not it.

**This stage never runs on its own.** ``classify_passage`` consults it only when
the rules fall through, so the expensive path is reserved for the passages that
actually need judgement, and a rule match is never overridden by a model.

**Two guards, because a confident wrong label is the failure mode that matters.**

* *Grounding.* The model must return the exact phrase it relied on, and that
  phrase has to appear in the passage. An invented one is discarded and the
  edge falls back to ``mentioned``. This is the same rule the rest of the
  project runs on -- no quote, no row -- applied to the model's own output.
* *Ceiling.* A model-assigned confidence is capped at :data:`MAX_CONFIDENCE`,
  below the floor a negative treatment needs to turn a light red on its own.
  A model can put ``abandoned`` in the detail with its passage; it cannot, by
  itself, tell a lawyer to discard a precedent.

Credentials are optional. :func:`available` reports whether the stage can run,
and the pipeline degrades to rules alone when it cannot: no key, no crash.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field

from citador_ar_mcp.domain.treatment import FALLBACK, Method, Polarity, Treatment
from citador_ar_mcp.ingest.treatment import UNCLASSIFIED_CONFIDENCE, Classification

log = logging.getLogger(__name__)

MODEL: Final = "claude-opus-5"

#: A model's judgement is evidence, not a verdict. This sits below
#: ``NEGATIVE_CONFIDENCE_FLOOR`` so an LLM-assigned ``abandoned`` shows up in the
#: detail with its passage but cannot turn the light red without a rule or a
#: human agreeing. Raising it above that floor hands the red light to the model.
MAX_CONFIDENCE: Final = 0.70

#: Legal judgement over adversarial prose, where the cost of a confident error
#: is a lawyer discarding good authority. Worth the effort budget.
EFFORT: Final = "high"

SYSTEM: Final = """\
Sos un asistente de investigación jurídica que clasifica cómo un fallo de la \
Corte Suprema de Justicia de la Nación argentina trató a un precedente que cita.

Se te da un pasaje de un fallo y la cita canónica del precedente en cuestión. \
Devolvés una sola etiqueta del vocabulario:

- applied: lo aplica como fundamento de la decisión
- followed: lo sigue expresamente
- distinguished: lo reconoce pero lo aparta por los hechos del caso
- limited: restringe su alcance
- criticized: lo cuestiona sin abandonarlo
- abandoned: el tribunal se aparta de la doctrina de ese precedente
- mentioned: lo cita sin tomar postura

Reglas, en orden de prioridad:

1. Ante cualquier duda, la respuesta es "mentioned" con confianza baja. Nunca \
inventes precisión. Una clasificación equivocada que dice "abandonado" hace que \
un abogado descarte doctrina vigente, y ese es el error caro.

2. "abandoned" exige que el pasaje muestre que ESTE tribunal se aparta de ESE \
precedente. No alcanza con que el pasaje contenga la palabra "apartarse".

3. Cuidado con el relato histórico. Si el pasaje narra lo que hizo un tribunal \
anterior ("en 'Bazterrica' se apartó de tal doctrina"), el precedente citado es \
el LUGAR de ese apartamiento, no su objeto: eso es "mentioned", no "abandoned".

4. Cuidado con la atribución. Si el pasaje reproduce el argumento de una parte, \
del recurrente o del dictamen del Procurador, no es la postura del tribunal: \
"mentioned".

5. Un pasaje puede citar varios precedentes con tratamientos distintos. \
Clasificá únicamente el que se te indica.

Toda respuesta lleva `evidence`: la frase EXACTA y textual del pasaje que \
sostiene tu clasificación, copiada carácter por carácter. Si no podés señalar \
una frase textual que la sostenga, la clasificación es "mentioned"."""


class _Verdict(BaseModel):
    """The schema the model is constrained to."""

    treatment: Treatment
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(description="Frase textual del pasaje que sostiene la clasificación")
    reasoning: str = Field(description="Una o dos oraciones, en español")


#: Claude Opus 5 list prices, US dollars per million tokens. Cache reads are a
#: tenth of the input rate; writes are a quarter more than it.
PRICE_INPUT: Final = 5.00
PRICE_OUTPUT: Final = 25.00
PRICE_CACHE_READ: Final = 0.50
PRICE_CACHE_WRITE: Final = 6.25


@dataclass
class Budget:
    """A hard ceiling on what a classification run may spend.

    Accounted from the ``usage`` the API returns, not from an estimate of it.
    An estimate is what you check afterwards; this is what stops the run.
    """

    limit_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def record(self, usage: object) -> float:
        """Add one response's usage. Returns what that call cost.

        Reads the fields defensively: a field the SDK stops reporting must cost
        the run accuracy, not raise in the middle of a paid batch.
        """

        def field(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        inp = field("input_tokens")
        out = field("output_tokens")
        cr = field("cache_read_input_tokens")
        cw = field("cache_creation_input_tokens")
        cost = (
            inp * PRICE_INPUT + out * PRICE_OUTPUT + cr * PRICE_CACHE_READ + cw * PRICE_CACHE_WRITE
        ) / 1_000_000
        self.spent_usd += cost
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        return cost

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd

    @property
    def per_call(self) -> float:
        return self.spent_usd / self.calls if self.calls else 0.0

    def affords(self, calls: int = 1) -> bool:
        """Whether the measured per-call cost leaves room for ``calls`` more.

        Uses the observed average rather than a guess, and refuses once the
        projection would cross the limit -- so the ceiling holds even if the
        first calls happened to be cheap.
        """
        if self.exhausted:
            return False
        if not self.calls:
            return True
        return self.spent_usd + self.per_call * calls <= self.limit_usd

    def summary(self) -> str:
        return (
            f"{self.calls} llamadas, ${self.spent_usd:.2f} de ${self.limit_usd:.2f} "
            f"(${self.per_call:.4f} por llamada; "
            f"{self.input_tokens:,} in, {self.output_tokens:,} out, "
            f"{self.cache_read_tokens:,} de caché)"
        )


def available() -> bool:
    """Whether the LLM stage can actually run.

    Probes with ``count_tokens``, which authenticates but bills nothing.

    Constructing the client is not a check: the SDK resolves credentials lazily,
    so ``Anthropic()`` succeeds with no credential at all and fails only on the
    first real request. An earlier version of this function returned ``True`` in
    exactly that state, which would have reported "etapa LLM activa" and then
    silently classified nothing.

    An unset ``ANTHROPIC_API_KEY`` does not on its own mean there are no
    credentials -- the SDK also resolves ``ANTHROPIC_AUTH_TOKEN`` and an
    ``ant auth login`` profile -- which is why this asks the API rather than
    reading an environment variable.
    """
    try:
        import anthropic
    except ImportError:
        return False
    try:
        client = anthropic.Anthropic(max_retries=0, timeout=15.0)
        client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": "ping"}])
    except Exception as exc:
        log.info("etapa LLM no disponible: %s", type(exc).__name__)
        return False
    return True


def _normalize(text: str) -> str:
    """Loose comparison key for grounding: whitespace and case only."""
    return " ".join(text.split()).casefold()


class ClaudeTreatmentClassifier:
    """Classifies a passage with Claude. Satisfies ``TreatmentClassifier``.

    Construct it once and reuse it: the client holds a connection pool, and the
    system prompt is identical on every call, so it caches.
    """

    def __init__(
        self,
        *,
        model: str = MODEL,
        effort: str = EFFORT,
        max_confidence: float = MAX_CONFIDENCE,
        budget: Budget | None = None,
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._model = model
        self._effort = effort
        self._max_confidence = max_confidence
        self.budget = budget

    def classify(self, quote: str, *, cited: str | None = None) -> Classification | None:
        """Classify ``quote``. Returns ``None`` on any failure, never raises.

        ``None`` means "the rules' fallback stands", which is the conservative
        outcome. An outage in this stage degrades coverage, never correctness.
        """
        if self.budget is not None and not self.budget.affords():
            log.warning("presupuesto agotado (%s); no se llama más", self.budget.summary())
            return None

        target = cited or "el precedente citado en el pasaje"
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Precedente a clasificar: {target}\n\n"
                            f"Pasaje del fallo que lo cita:\n\n{quote}"
                        ),
                    }
                ],
                output_format=_Verdict,
            )
        except self._anthropic.APIStatusError as exc:
            log.warning("clasificación LLM falló (%s): %s", exc.status_code, exc.message)
            return None
        except self._anthropic.APIConnectionError:
            log.warning("clasificación LLM sin conexión; quedan las reglas")
            return None
        except Exception:
            log.exception("clasificación LLM falló de forma inesperada")
            return None

        if self.budget is not None:
            self.budget.record(getattr(response, "usage", None))

        verdict = response.parsed_output
        if verdict is None:
            return None
        return self._ground(verdict, quote)

    def _ground(self, verdict: _Verdict, quote: str) -> Classification:
        """Check the model's evidence against the passage and cap its confidence."""
        if _normalize(verdict.evidence) not in _normalize(quote):
            # The model cited a phrase the passage does not contain. Whatever it
            # concluded, it is not reading this text.
            log.warning(
                "descarto clasificación LLM '%s': la frase citada no está en el pasaje",
                verdict.treatment.value,
            )
            return Classification(
                FALLBACK,
                UNCLASSIFIED_CONFIDENCE,
                Method.LLM,
                rule="sin-anclaje",
                scope="fallback",
            )

        confidence = min(verdict.confidence, self._max_confidence)
        if verdict.treatment.polarity is Polarity.NEGATIVE:
            log.info(
                "LLM propone 'abandoned' con confianza %.2f (tope %.2f): "
                "figura en el detalle, no enciende la señal sola",
                verdict.confidence,
                self._max_confidence,
            )
        return Classification(
            verdict.treatment,
            round(confidence, 3),
            Method.LLM,
            rule="llm",
            scope="clause",
        )


def build(**kwargs: object) -> ClaudeTreatmentClassifier | None:
    """The classifier, or ``None`` when it cannot run. Never raises."""
    if not available():
        log.info(
            "etapa LLM no disponible: falta el paquete 'anthropic' "
            "(uv sync --extra llm) o no hay credencial. Siguen solo las reglas."
        )
        return None
    try:
        return ClaudeTreatmentClassifier(**kwargs)  # type: ignore[arg-type]
    except Exception:
        log.exception("no pude construir el clasificador LLM; siguen solo las reglas")
        return None


def enabled_by_env() -> bool:
    """Whether the pipeline should try the LLM stage. Off unless asked for.

    Opt-in because it costs money per passage and a corpus crawl is a lot of
    passages.
    """
    return os.environ.get("CITADOR_LLM", "").strip().lower() in {"1", "true", "yes", "si", "sí"}
