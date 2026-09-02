"""Tests for the rule-based treatment classifier.

Most of these are about the guards. The rules themselves are easy; what is hard
is not firing them on prose that only looks like a departure -- a party's
argument, a negated formula, or the Court recounting what an earlier Court did.
"""

from __future__ import annotations

import pytest

from citador_ar_mcp.domain.treatment import Treatment
from citador_ar_mcp.ingest.treatment import (
    ATTRIBUTED_CONFIDENCE,
    UNCLASSIFIED_CONFIDENCE,
    classify_passage,
    local_clause,
)


def treatment_of(quote: str, *, at: int | None = None) -> Treatment:
    return classify_passage(quote, citation_position=at).treatment


class TestRules:
    @pytest.mark.parametrize(
        ("quote", "expected"),
        [
            (
                "corresponde apartarse de la doctrina de Fallos: 300:254",
                Treatment.ABANDONED,
            ),
            (
                "cabe abandonar la doctrina sentada en Fallos: 308:1392",
                Treatment.ABANDONED,
            ),
            (
                "no es compatible el criterio expuesto en Fallos: 308:1392",
                Treatment.ABANDONED,
            ),
            (
                "las cuestiones han sido resueltas acertadamente en Fallos: 308:1392",
                Treatment.FOLLOWED,
            ),
            (
                "corresponde aplicar el criterio de Fallos: 332:1963",
                Treatment.APPLIED,
            ),
            (
                "el precedente de Fallos: 300:254 no resulta aplicable al caso",
                Treatment.DISTINGUISHED,
            ),
            (
                "corresponde limitar el alcance de la doctrina de Fallos: 313:1333",
                Treatment.LIMITED,
            ),
        ],
    )
    def test_each_formula_maps_to_its_treatment(self, quote: str, expected: Treatment) -> None:
        assert treatment_of(quote) is expected

    def test_an_unremarkable_citation_is_only_mentioned(self) -> None:
        quote = "conforme surge de las constancias de la causa (Fallos: 328:4343)"
        result = classify_passage(quote)
        assert result.treatment is Treatment.MENTIONED
        assert result.confidence == UNCLASSIFIED_CONFIDENCE
        assert result.is_fallback


class TestNegationGuard:
    def test_a_negated_departure_is_not_a_departure(self) -> None:
        """`no corresponde apartarse` is the opposite of `corresponde apartarse`."""
        quote = "no corresponde apartarse de la doctrina de Fallos: 300:254"
        assert treatment_of(quote) is not Treatment.ABANDONED

    def test_the_positive_form_still_fires(self) -> None:
        assert treatment_of("corresponde apartarse de la doctrina") is Treatment.ABANDONED


class TestAttributionGuard:
    def test_a_partys_argument_is_not_the_courts_holding(self) -> None:
        quote = (
            "El recurrente sostiene que corresponde apartarse de la doctrina de "
            "Fallos: 300:254 por resultar contraria al artículo 19"
        )
        result = classify_passage(quote)
        assert result.treatment is Treatment.MENTIONED
        assert result.confidence <= ATTRIBUTED_CONFIDENCE
        assert result.rule is not None and "atribuido" in result.rule

    def test_the_finding_stays_visible_for_auditing(self) -> None:
        """Downgraded, not discarded: the rule name records what was seen."""
        quote = "el apelante alega que cabe abandonar la doctrina de Fallos: 308:1392"
        assert classify_passage(quote).rule is not None


class TestRecitalGuard:
    def test_narrating_an_earlier_departure_is_not_departing(self) -> None:
        """The inversion the pipeline produced on its first real run.

        Verbatim from considerando 12 of Arriola. The subject of "se apartó" is
        the Court sitting in Bazterrica in 1986, and what it departed from was
        Colavini. Read as a treatment of Bazterrica it says the opposite of what
        happened: Arriola restored Bazterrica.
        """
        clause = 'en "Bazterrica" y "Capalbo", se apartó de tal doctrina (Fallos: 308:1392)'
        result = classify_passage(clause, citation_position=clause.find("308"))
        assert result.treatment is Treatment.MENTIONED
        assert result.rule is not None and "relato-historico" in result.rule

    def test_a_dated_recital_is_also_narration(self) -> None:
        clause = 'y en 1990, en "Montalvo" se apartó de tal doctrina (Fallos: 313:1333)'
        assert treatment_of(clause, at=clause.find("313")) is Treatment.MENTIONED

    def test_a_real_departure_is_not_swallowed_by_the_guard(self) -> None:
        """The precedent is the object of the verb here, not its location."""
        quote = "No es compatible, pues, el criterio expuesto en Fallos: 308:1392"
        assert treatment_of(quote) is Treatment.ABANDONED


class TestClauseScope:
    ARRIOLA_C12 = (
        'Así en "Colavini" (Fallos: 300:254) se pronunció a favor de la '
        'criminalización; en "Bazterrica" y "Capalbo", se apartó de tal doctrina '
        '(Fallos: 308:1392); y en 1990, en "Montalvo" vuelve nuevamente sobre sus '
        "pasos a favor de la criminalización (Fallos: 313:1333)"
    )

    def test_one_sentence_three_precedents_is_not_three_departures(self) -> None:
        """The passage carries `se apartó de tal doctrina` once, about one of them."""
        for needle in ("300:254", "308:1392", "313:1333"):
            at = self.ARRIOLA_C12.find(needle)
            assert treatment_of(self.ARRIOLA_C12, at=at) is Treatment.MENTIONED

    def test_local_clause_stops_at_the_semicolon(self) -> None:
        clause = local_clause(self.ARRIOLA_C12, self.ARRIOLA_C12.find("300:254"))
        assert "Colavini" in clause
        assert "Bazterrica" not in clause

    def test_a_distant_formula_cannot_light_the_red(self) -> None:
        """ABANDONED is the red light and never earns itself by proximity."""
        quote = (
            "corresponde apartarse de la doctrina anterior. "
            "Por lo demás, conviene recordar lo dicho en Fallos: 328:4343 sobre "
            "las condiciones carcelarias, que nada tiene que ver con el punto."
        )
        assert treatment_of(quote, at=quote.find("328:4343")) is not Treatment.ABANDONED


class TestOcrTolerance:
    def test_periods_that_are_really_commas_do_not_break_a_rule(self) -> None:
        """OCR of 1990 print reads commas as points; the formula still has to match.

        Verbatim shape from the OCR of Montalvo, where the source reads
        "No es compatible, pues, el criterio expuesto...".
        """
        quote = "No es compatible, pues. cl criterio expuesto en Fallos 308: 1392"
        assert treatment_of(quote) is Treatment.ABANDONED

    def test_a_real_sentence_break_still_separates_clauses(self) -> None:
        quote = (
            "corresponde apartarse de la doctrina. Por otro lado, Fallos: 328:4343 dice otra cosa"
        )
        assert treatment_of(quote, at=quote.find("328:4343")) is not Treatment.ABANDONED
