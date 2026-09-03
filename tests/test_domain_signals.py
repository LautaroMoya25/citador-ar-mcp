"""Unit tests for signal aggregation.

Most of these exist to hold the conservative guards in place. A citator that
says "still good law" about an abandoned precedent is worse than no citator, and
a citator that cries red over a dissent is not much better.
"""

from __future__ import annotations

import pytest

from citador_ar_mcp.domain.citation import RulingId
from citador_ar_mcp.domain.signals import Signal, TreatmentRecord, aggregate
from citador_ar_mcp.domain.treatment import (
    NEGATIVE_CONFIDENCE_FLOOR,
    Method,
    Opinion,
    Polarity,
    Treatment,
)

SUBJECT = RulingId(volume=308, page=1392)


def rec(
    treatment: Treatment,
    *,
    volume: int = 340,
    page: int = 100,
    opinion: Opinion = Opinion.MAJORITY,
    confidence: float = 0.9,
    year: int = 2017,
) -> TreatmentRecord:
    return TreatmentRecord(
        citing=RulingId(volume=volume, page=page),
        treatment=treatment,
        opinion=opinion,
        confidence=confidence,
        quote="pasaje de prueba que contiene la cita Fallos: 308:1392 y su contexto",
        method=Method.MANUAL,
        decided_year=year,
    )


class TestSignalDirection:
    @pytest.mark.parametrize(
        ("treatment", "expected"),
        [
            (Treatment.ABANDONED, Signal.RED),
            (Treatment.CRITICIZED, Signal.YELLOW),
            (Treatment.LIMITED, Signal.YELLOW),
            (Treatment.DISTINGUISHED, Signal.YELLOW),
            (Treatment.APPLIED, Signal.GREEN),
            (Treatment.FOLLOWED, Signal.GREEN),
            (Treatment.MENTIONED, Signal.GRAY),
        ],
    )
    def test_each_treatment_drives_the_expected_light(
        self, treatment: Treatment, expected: Signal
    ) -> None:
        assert aggregate(SUBJECT, [rec(treatment)]).signal is expected

    def test_the_worst_binding_treatment_wins(self) -> None:
        records = [
            rec(Treatment.APPLIED, page=1),
            rec(Treatment.FOLLOWED, page=2),
            rec(Treatment.ABANDONED, page=3),
        ]
        assert aggregate(SUBJECT, records).signal is Signal.RED

    def test_evidence_is_ordered_worst_first(self) -> None:
        records = [rec(Treatment.MENTIONED, page=1), rec(Treatment.ABANDONED, page=2)]
        report = aggregate(SUBJECT, records)
        assert report.evidence[0].treatment is Treatment.ABANDONED


class TestConservativeGuards:
    def test_a_dissent_cannot_turn_the_light_red(self) -> None:
        """A citation in a dissent is not the Court's doctrine."""
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, opinion=Opinion.DISSENT)])
        assert report.signal is not Signal.RED
        assert report.counts[Treatment.ABANDONED] == 1
        assert Treatment.ABANDONED not in report.binding_counts
        assert any("disidencias" in c for c in report.caveats)

    def test_a_concurrence_cannot_turn_the_light_red_either(self) -> None:
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, opinion=Opinion.CONCURRENCE)])
        assert report.signal is not Signal.RED

    def test_unknown_opinion_is_treated_as_non_binding(self) -> None:
        """When we cannot tell which vote a passage came from, we do not guess."""
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, opinion=Opinion.UNKNOWN)])
        assert report.signal is not Signal.RED
        assert any("mayoría" in c for c in report.caveats)

    def test_low_confidence_negative_does_not_turn_the_light_red(self) -> None:
        low = NEGATIVE_CONFIDENCE_FLOOR - 0.1
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, confidence=low)])
        assert report.signal is not Signal.RED
        assert any("umbral de confianza" in c for c in report.caveats)

    def test_confident_negative_does_turn_the_light_red(self) -> None:
        report = aggregate(
            SUBJECT, [rec(Treatment.ABANDONED, confidence=NEGATIVE_CONFIDENCE_FLOOR)]
        )
        assert report.signal is Signal.RED


class TestRestoration:
    """Precedent moves both ways. A time-blind citator gets those cases backwards."""

    def test_a_later_return_downgrades_red_to_yellow(self) -> None:
        records = [
            rec(Treatment.ABANDONED, page=1, year=1990),
            rec(Treatment.FOLLOWED, page=2, year=2009),
        ]
        report = aggregate(SUBJECT, records)
        assert report.signal is Signal.YELLOW
        assert any("abandonado y después retomado" in c for c in report.caveats)

    def test_restoration_never_reaches_green(self) -> None:
        """It is live law with a history, and the history has to stay visible."""
        records = [
            rec(Treatment.ABANDONED, page=1, year=1990),
            rec(Treatment.FOLLOWED, page=2, year=2009),
            rec(Treatment.APPLIED, page=3, year=2015),
        ]
        assert aggregate(SUBJECT, records).signal is not Signal.GREEN

    def test_an_earlier_positive_does_not_restore_anything(self) -> None:
        """Followed in 1980, abandoned in 1990: still abandoned."""
        records = [
            rec(Treatment.FOLLOWED, page=1, year=1980),
            rec(Treatment.ABANDONED, page=2, year=1990),
        ]
        assert aggregate(SUBJECT, records).signal is Signal.RED

    def test_unknown_years_keep_the_red(self) -> None:
        """Without dates the events cannot be ordered, so the conservative reading wins."""
        records = [
            TreatmentRecord(
                citing=RulingId(volume=313, page=1333),
                treatment=Treatment.ABANDONED,
                opinion=Opinion.MAJORITY,
                confidence=0.9,
                quote="pasaje",
                method=Method.MANUAL,
                decided_year=None,
            ),
            rec(Treatment.FOLLOWED, page=2, year=2009),
        ]
        assert aggregate(SUBJECT, records).signal is Signal.RED

    def test_a_restoring_dissent_does_not_count(self) -> None:
        records = [
            rec(Treatment.ABANDONED, page=1, year=1990),
            rec(Treatment.FOLLOWED, page=2, year=2009, opinion=Opinion.DISSENT),
        ]
        assert aggregate(SUBJECT, records).signal is Signal.RED


class TestDictamen:
    """The Procurador's opinion sits inside the same document as the ruling."""

    def test_the_dictamen_is_not_the_court(self) -> None:
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, opinion=Opinion.DICTAMEN)])
        assert report.signal is not Signal.RED
        assert not Opinion.DICTAMEN.is_binding

    def test_it_still_shows_up_in_the_detail(self) -> None:
        report = aggregate(SUBJECT, [rec(Treatment.ABANDONED, opinion=Opinion.DICTAMEN)])
        assert report.counts[Treatment.ABANDONED] == 1
        assert report.evidence[0].opinion is Opinion.DICTAMEN


class TestNeverABareValue:
    def test_no_citing_rulings_is_gray_not_green(self) -> None:
        """Absence of negative treatment is not evidence of validity."""
        report = aggregate(SUBJECT, [])
        assert report.signal is Signal.GRAY
        assert report.confidence == 0.0
        assert report.total_citing == 0
        assert any("no equivale a vigencia confirmada" in c for c in report.caveats)

    def test_mixed_treatment_is_reported_not_resolved(self) -> None:
        """Abandoned on one point, applied on another. The report must say so."""
        records = [
            rec(Treatment.ABANDONED, page=1),
            rec(Treatment.APPLIED, page=2),
        ]
        report = aggregate(SUBJECT, records)
        assert report.signal is Signal.RED
        assert not report.is_uniform
        assert any("no es uniforme" in c for c in report.caveats)

    def test_the_breakdown_always_accompanies_the_signal(self) -> None:
        records = [rec(Treatment.APPLIED, page=1), rec(Treatment.DISTINGUISHED, page=2)]
        report = aggregate(SUBJECT, records)
        assert report.counts == {Treatment.APPLIED: 1, Treatment.DISTINGUISHED: 1}
        assert report.binding_counts == report.counts

    def test_distinct_citing_rulings_are_counted_once(self) -> None:
        records = [
            rec(Treatment.APPLIED, page=1),
            rec(Treatment.MENTIONED, page=1),
            rec(Treatment.FOLLOWED, page=2),
        ]
        assert aggregate(SUBJECT, records).total_citing == 2


class TestVocabulary:
    def test_abandoned_is_the_only_negative_polarity(self) -> None:
        negatives = [t for t in Treatment if t.polarity is Polarity.NEGATIVE]
        assert negatives == [Treatment.ABANDONED]

    def test_abandoned_is_the_most_severe(self) -> None:
        assert Treatment.ABANDONED.severity == max(t.severity for t in Treatment)

    def test_only_the_majority_binds(self) -> None:
        binding = [o for o in Opinion if o.is_binding]
        assert binding == [Opinion.MAJORITY]

    def test_every_treatment_has_spanish_labels(self) -> None:
        for t in Treatment:
            assert t.label_es and t.description_es


class TestNonBindingBreakdown:
    """ "Written in a dissent" and "we cannot tell" are different claims.

    Found on the first real crawl. Every edge there is unattributed, and lumping
    them under one label reported 156 citations as coming from dissents when the
    truth was that the vote was simply unknown.
    """

    def test_unknown_is_not_reported_as_a_dissent(self) -> None:
        report = aggregate(SUBJECT, [rec(Treatment.MENTIONED, opinion=Opinion.UNKNOWN)])
        assert report.non_binding_by_opinion == {Opinion.UNKNOWN: 1}
        assert Opinion.DISSENT not in report.non_binding_by_opinion

    def test_each_opinion_is_counted_separately(self) -> None:
        records = [
            rec(Treatment.MENTIONED, page=1, opinion=Opinion.DISSENT),
            rec(Treatment.MENTIONED, page=2, opinion=Opinion.DISSENT),
            rec(Treatment.MENTIONED, page=3, opinion=Opinion.CONCURRENCE),
            rec(Treatment.MENTIONED, page=4, opinion=Opinion.DICTAMEN),
            rec(Treatment.MENTIONED, page=5, opinion=Opinion.UNKNOWN),
        ]
        assert aggregate(SUBJECT, records).non_binding_by_opinion == {
            Opinion.DISSENT: 2,
            Opinion.CONCURRENCE: 1,
            Opinion.DICTAMEN: 1,
            Opinion.UNKNOWN: 1,
        }

    def test_majority_citations_are_not_in_the_breakdown(self) -> None:
        report = aggregate(SUBJECT, [rec(Treatment.FOLLOWED, opinion=Opinion.MAJORITY)])
        assert report.non_binding_by_opinion == {}


class TestCoverageCaveat:
    """A signal resting on three passages is not the claim a signal resting on fifty is.

    Measured on the real corpus: a green light stood on a single `applied`
    against twenty-six citations nobody had classified. "Vigente" is the wrong
    headline for "found one favourable cite and could not read the rest".
    """

    def _unread(self, page: int) -> TreatmentRecord:
        return rec(Treatment.MENTIONED, page=page, confidence=0.2)

    def test_a_green_on_thin_evidence_says_so(self) -> None:
        records = [rec(Treatment.APPLIED, page=1)] + [self._unread(p) for p in range(2, 28)]
        report = aggregate(SUBJECT, records)
        assert report.signal is Signal.GREEN
        assert any("de 27 citas de la mayoría" in c for c in report.caveats)

    def test_a_green_on_read_evidence_does_not(self) -> None:
        records = [rec(Treatment.APPLIED, page=p) for p in range(1, 6)]
        assert not any("quedó sin leer" in c for c in aggregate(SUBJECT, records).caveats)

    def test_the_caveat_also_covers_yellow(self) -> None:
        records = [rec(Treatment.DISTINGUISHED, page=1)] + [self._unread(p) for p in range(2, 12)]
        report = aggregate(SUBJECT, records)
        assert report.signal is Signal.YELLOW
        assert any("quedó sin leer" in c for c in report.caveats)

    def test_red_is_not_softened_by_it(self) -> None:
        """A departure is a departure however little else was read."""
        records = [rec(Treatment.ABANDONED, page=1)] + [self._unread(p) for p in range(2, 30)]
        report = aggregate(SUBJECT, records)
        assert report.signal is Signal.RED
        assert not any("quedó sin leer" in c for c in report.caveats)
