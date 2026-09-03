"""Tests for the LLM classification stage.

None of these call the API. What is worth testing here is not that Claude
classifies well -- that is what ``scripts/eval_treatment.py`` measures against
the golden -- but that a wrong or unavailable model cannot damage the graph:
the grounding check, the confidence ceiling, and the degradation path.
"""

from __future__ import annotations

import pytest

from citador_ar_mcp.domain.treatment import (
    NEGATIVE_CONFIDENCE_FLOOR,
    Method,
    Treatment,
)
from citador_ar_mcp.ingest import llm
from citador_ar_mcp.ingest.treatment import (
    UNCLASSIFIED_CONFIDENCE,
    Classification,
    classify_passage,
)

PASSAGE = (
    "Que las cuestiones centrales en debate han sido resueltas acertadamente "
    "en el precedente de Fallos: 308:1392, cuya doctrina el Tribunal reitera."
)


class FakeVerdict(llm._Verdict):
    pass


def ground(treatment: Treatment, evidence: str, confidence: float = 0.95) -> Classification:
    """Run the grounding check without touching the network."""
    classifier = llm.ClaudeTreatmentClassifier.__new__(llm.ClaudeTreatmentClassifier)
    classifier._max_confidence = llm.MAX_CONFIDENCE
    verdict = FakeVerdict(
        treatment=treatment,
        confidence=confidence,
        evidence=evidence,
        reasoning="prueba",
    )
    return classifier._ground(verdict, PASSAGE)


class TestGrounding:
    def test_a_quoted_phrase_that_exists_is_accepted(self) -> None:
        result = ground(Treatment.FOLLOWED, "han sido resueltas acertadamente")
        assert result.treatment is Treatment.FOLLOWED
        assert result.method is Method.LLM

    def test_whitespace_and_case_do_not_break_grounding(self) -> None:
        result = ground(Treatment.FOLLOWED, "HAN  SIDO\n  RESUELTAS   ACERTADAMENTE")
        assert result.treatment is Treatment.FOLLOWED

    def test_an_invented_phrase_is_discarded(self) -> None:
        """No quote, no row -- applied to the model's own output.

        A model that cites a phrase the passage does not contain is not reading
        the passage, and whatever it concluded cannot be trusted.
        """
        result = ground(Treatment.ABANDONED, "corresponde apartarse de la doctrina")
        assert result.treatment is Treatment.MENTIONED
        assert result.confidence == UNCLASSIFIED_CONFIDENCE
        assert result.rule == "sin-anclaje"

    def test_an_invented_phrase_cannot_smuggle_in_a_red_light(self) -> None:
        assert ground(Treatment.ABANDONED, "inventado", confidence=1.0).treatment is not (
            Treatment.ABANDONED
        )


class TestConfidenceCeiling:
    def test_the_model_cannot_claim_certainty(self) -> None:
        result = ground(Treatment.FOLLOWED, "cuya doctrina el Tribunal reitera", 1.0)
        assert result.confidence <= llm.MAX_CONFIDENCE

    def test_an_llm_abandoned_cannot_turn_a_light_red_on_its_own(self) -> None:
        """The ceiling sits below the floor a negative treatment needs.

        The model can put `abandoned` in the detail with its passage; it cannot
        by itself tell a lawyer to discard a precedent.
        """
        assert llm.MAX_CONFIDENCE < NEGATIVE_CONFIDENCE_FLOOR
        result = ground(Treatment.ABANDONED, "Fallos: 308:1392", confidence=1.0)
        assert result.treatment is Treatment.ABANDONED
        assert result.confidence < NEGATIVE_CONFIDENCE_FLOOR


class TestDegradation:
    def test_a_rule_match_never_consults_the_model(self) -> None:
        """The expensive path is only for what the rules cannot read."""
        calls: list[str] = []

        class Spy:
            def classify(self, quote: str, *, cited: str | None = None) -> None:
                calls.append(quote)
                return None

        result = classify_passage(PASSAGE, llm=Spy())
        assert result.treatment is Treatment.FOLLOWED
        assert result.method is Method.RULE
        assert calls == []

    def test_the_model_is_consulted_when_the_rules_fall_through(self) -> None:
        quote = "conforme surge de lo actuado (Fallos: 328:4343)"
        seen: list[str] = []

        class Spy:
            def classify(self, q: str, *, cited: str | None = None) -> Classification:
                seen.append(q)
                return Classification(Treatment.APPLIED, 0.6, Method.LLM, rule="llm")

        result = classify_passage(quote, llm=Spy())
        assert seen == [quote]
        assert result.treatment is Treatment.APPLIED

    def test_a_model_failure_leaves_the_conservative_fallback(self) -> None:
        class Broken:
            def classify(self, quote: str, *, cited: str | None = None) -> None:
                return None

        result = classify_passage("conforme surge de lo actuado (Fallos: 328:4343)", llm=Broken())
        assert result.treatment is Treatment.MENTIONED
        assert result.confidence == UNCLASSIFIED_CONFIDENCE

    def test_build_returns_none_instead_of_raising_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm, "available", lambda: False)
        assert llm.build() is None


class TestOptIn:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "si", "SÍ", "True"])
    def test_recognised_opt_ins(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CITADOR_LLM", value)
        assert llm.enabled_by_env()

    @pytest.mark.parametrize("value", ["", "0", "no", "false"])
    def test_off_by_default(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CITADOR_LLM", value)
        assert not llm.enabled_by_env()

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CITADOR_LLM", raising=False)
        assert not llm.enabled_by_env()


class TestModelChoice:
    def test_uses_the_current_opus_model_id(self) -> None:
        assert llm.MODEL == "claude-opus-5"


class TestAvailability:
    """Constructing the client is not a credential check.

    The SDK resolves credentials lazily, so `Anthropic()` succeeds with none at
    all and fails only on the first real request. An earlier version of
    `available()` returned True in exactly that state, which would have reported
    the stage as active and then classified nothing.
    """

    def test_a_credential_failure_is_reported_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic

        def explode(*_a: object, **_k: object) -> object:
            raise TypeError("Could not resolve authentication method")

        monkeypatch.setattr(anthropic.Anthropic, "__init__", explode)
        assert not llm.available()

    def test_the_probe_does_not_use_the_messages_endpoint(self) -> None:
        """It has to authenticate without billing anything."""
        import inspect

        source = inspect.getsource(llm.available)
        assert "count_tokens" in source
        assert "messages.create" not in source
        assert "messages.parse" not in source
