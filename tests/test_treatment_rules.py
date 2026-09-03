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


class TestFormulasFromTheRealCorpus:
    """Rules added after measuring which formulas actually occur.

    A sample of 120 real passages from tomo 332 classified at 0%: the rule set
    had been built from the golden chain, which is four rulings written across
    thirty years and not representative of how the Court cites day to day. Most
    citations really are neutral -- strings of supporting cites joined by "v.,
    entre otros" and "y su cita" -- but two carrying formulas were being missed.
    """

    def test_doctrina_de_invokes_the_precedent_as_governing(self) -> None:
        """`(doctrina de Fallos: 320:1272)` applies that ruling's holding."""
        quote = (
            "los que se consideran afectados deben demostrar que quien emitió la "
            "expresión obró con conocimiento de que eran falsas (doctrina de "
            "Fallos: 320:1272; 327:943)"
        )
        assert treatment_of(quote, at=quote.find("320:1272")) is Treatment.APPLIED

    def test_as_the_court_held_in_is_following(self) -> None:
        quote = (
            "tal como expuso el Tribunal en la causa Patitó (Fallos: 331:1530), "
            "las reglas de la responsabilidad civil ceden"
        )
        assert treatment_of(quote, at=quote.find("331:1530")) is Treatment.FOLLOWED

    def test_a_string_of_supporting_cites_stays_neutral(self) -> None:
        """The common shape, and it genuinely takes no position on any of them."""
        quote = (
            "que interesan a vastos sectores de la población y que se originan en "
            "una relación que supone una desigualdad entre las partes (Fallos: "
            "181:209, 213/214; 239:80, 83 y 306:1059, 1064)"
        )
        assert treatment_of(quote, at=quote.find("181:209")) is Treatment.MENTIONED

    def test_doctrina_de_a_named_doctrine_is_not_a_treatment(self) -> None:
        """ "la doctrina de la real malicia" names a doctrine, not a precedent."""
        quote = (
            "en los términos de la doctrina de la real malicia, esta Corte "
            "resolvió el caso (Fallos: 331:1530)"
        )
        assert treatment_of(quote, at=quote.find("331:1530")) is not Treatment.APPLIED


class TestFormulasMinedFromTheCorpus:
    """Rules derived by reading 9.168 real passages, not by inventing patterns.

    scripts/mine_formulas.py ranks the phrases that recur around a citation,
    anchored on verb stems because a treatment in Spanish is carried by a verb.
    Most of what it surfaced was a false friend and was rejected; these are the
    ones that survived reading the passages they came from.
    """

    def test_a_precedent_invoked_for_its_holding_is_applied(self) -> None:
        quote = (
            "Que cabe recordar que en el precedente de Fallos: 308:789, este "
            "Tribunal sostuvo que cuando un órgano periodístico difunde una "
            "información que podría tener entidad difamatoria"
        )
        assert treatment_of(quote, at=quote.find("308:789")) is Treatment.APPLIED

    def test_the_courts_own_settled_doctrine_is_applied(self) -> None:
        quote = (
            "el a quo construyó una nulidad en abierta contradicción a la "
            "doctrina sentada por esta Corte (Fallos: 295:961)"
        )
        assert treatment_of(quote, at=quote.find("295:961")) is Treatment.APPLIED

    def test_a_binding_doctrine_is_applied(self) -> None:
        quote = "obliga la doctrina precedentemente citada (Fallos: 332:2813)"
        assert treatment_of(quote, at=quote.find("332:2813")) is Treatment.APPLIED

    def test_narrowing_the_reach_of_a_criterion_is_limited(self) -> None:
        quote = (
            "se ha manifestado que dicho criterio no resulta aplicable cuando la "
            "información no se refiere a funcionarios o figuras públicas "
            "(Fallos: 330:3685)"
        )
        assert treatment_of(quote, at=quote.find("330:3685")) is Treatment.LIMITED


class TestFalseFriendsTheMiningExposed:
    """Frequent phrases that look like treatment and are not.

    Each of these ranked high in the mining and was rejected after reading the
    passages. They are kept as tests because the next person to extend the rule
    set will find them just as tempting.
    """

    def test_shared_powers_are_not_shared_reasoning(self) -> None:
        """ "compartidas" here is about federal competences, not about agreeing."""
        quote = (
            "debe evitarse que los estados abusen en el ejercicio de esas "
            "competencias, tanto si son propias como si son compartidas o "
            "concurrentes (Fallos: 340:1695)"
        )
        assert treatment_of(quote, at=quote.find("340:1695")) is Treatment.MENTIONED

    def test_a_case_that_must_be_decided_is_not_a_case_that_was_followed(self) -> None:
        quote = (
            "constituye un presupuesto necesario que exista un caso o "
            "controversia que deba ser resuelto por el Tribunal (Fallos: 323:4098)"
        )
        assert treatment_of(quote, at=quote.find("323:4098")) is Treatment.MENTIONED

    def test_the_rule_about_departing_is_not_a_departure(self) -> None:
        """The Court stating when departure is permissible, citing in support."""
        quote = (
            "se estableció que el Tribunal no podría apartarse de su doctrina "
            "sino sobre la base de causas suficientemente graves (Fallos: 183:409)"
        )
        assert treatment_of(quote, at=quote.find("183:409")) is not Treatment.ABANDONED

    def test_not_departing_from_a_statutes_text_is_not_departing_from_a_precedent(
        self,
    ) -> None:
        quote = (
            "Cuando la letra de una norma es clara no cabe apartarse de su texto (Fallos: 327:5614)"
        )
        assert treatment_of(quote, at=quote.find("327:5614")) is not Treatment.ABANDONED
