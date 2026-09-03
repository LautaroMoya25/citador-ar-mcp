"""The acceptance test of the project.

If this does not pass, nothing else matters. The chain is
the constitutionality of possession for personal use, turned over four times in
thirty years::

    Colavini   (1978)  penalisation is constitutional
    Bazterrica (1986)  abandoned Colavini
    Montalvo   (1990)  abandoned Bazterrica
    Arriola    (2009)  abandoned Montalvo, followed Bazterrica

All five edges are reconstructed from real text. The three old rulings needed
OCR to get there: their PDFs carry a text layer encoded through font subsets
with no usable Unicode map, which reads as a substitution cipher.

One edge does not carry the label the chain summary above would suggest, and the
divergence is the point rather than a defect. Bazterrica's majority cites
Colavini once and cites it *approvingly*; the departure comes later and never
names the precedent. The fixture therefore records ``mentioned``, which is what
the quoted passage supports. See :class:`TestImplicitOverruling`.
"""

from __future__ import annotations

from typing import Any

from citador_ar_mcp.domain.citation import RulingId, find_fallos_citations
from citador_ar_mcp.domain.signals import Signal, TreatmentRecord, aggregate
from citador_ar_mcp.domain.treatment import Method, Opinion, Treatment

COLAVINI = "fallos:300:254"
BAZTERRICA = "fallos:308:1392"
MONTALVO = "fallos:313:1333"
ARRIOLA = "fallos:332:1963"


def _edge(golden: dict[str, Any], citing: str, cited: str) -> dict[str, Any] | None:
    return next(
        (e for e in golden["edges"] if e["citing_id"] == citing and e["cited_id"] == cited),
        None,
    )


class TestChainRulings:
    def test_the_four_rulings_are_present_and_identified(self, golden: dict[str, Any]) -> None:
        by_id = {r["id"]: r for r in golden["rulings"]}
        assert set(by_id) == {COLAVINI, BAZTERRICA, MONTALVO, ARRIOLA}
        assert by_id[ARRIOLA]["short_name"] == "Arriola"
        assert by_id[ARRIOLA]["decided_on"] == "25/08/2009"
        assert by_id[COLAVINI]["decided_year"] == 1978

    def test_old_rulings_have_a_year_but_no_full_date(self, golden: dict[str, Any]) -> None:
        """The reason `decided_on` is nullable in the schema. See store/schema.sql."""
        by_id = {r["id"]: r for r in golden["rulings"]}
        assert by_id[COLAVINI]["decided_on"] is None
        assert by_id[BAZTERRICA]["decided_on"] is None
        assert by_id[MONTALVO]["decided_on"] == "11/12/1990"

    def test_every_ruling_id_round_trips_through_the_parser(self, golden: dict[str, Any]) -> None:
        for r in golden["rulings"]:
            rid = RulingId.build(r["volume"], r["page"])
            assert rid is not None and str(rid) == r["id"]
            assert RulingId.parse(rid.human) == rid

    def test_aliases_resolve_every_written_form(self, golden: dict[str, Any]) -> None:
        aliases = {raw: ruling for raw, _form, ruling in golden["aliases"]}
        assert aliases["ARRIOLA"] == ARRIOLA
        assert aliases["A.891.XLIV"] == ARRIOLA
        assert aliases["ARRIOLA SEBASTIAN"] == ARRIOLA
        assert aliases[ARRIOLA] == ARRIOLA


class TestChainEdges:
    def test_arriola_followed_bazterrica(self, golden: dict[str, Any]) -> None:
        e = _edge(golden, ARRIOLA, BAZTERRICA)
        assert e is not None, "falta la arista Arriola -> Bazterrica"
        assert e["treatment"] == Treatment.FOLLOWED.value

    def test_arriola_abandoned_montalvo(self, golden: dict[str, Any]) -> None:
        e = _edge(golden, ARRIOLA, MONTALVO)
        assert e is not None, "falta la arista Arriola -> Montalvo"
        assert e["treatment"] == Treatment.ABANDONED.value

    def test_arriola_only_mentions_colavini(self, golden: dict[str, Any]) -> None:
        """Arriola narrates Colavini's history; Bazterrica is what departed from it."""
        e = _edge(golden, ARRIOLA, COLAVINI)
        assert e is not None
        assert e["treatment"] == Treatment.MENTIONED.value

    def test_every_edge_carries_an_auditable_quote(self, golden: dict[str, Any]) -> None:
        """No quote, no row."""
        for e in golden["edges"]:
            assert e["quote"].strip(), f"arista sin pasaje: {e['citing']} -> {e['cited']}"
            assert len(e["quote"]) > 40

    def test_the_cited_ruling_actually_appears_in_its_own_quote(
        self, golden: dict[str, Any]
    ) -> None:
        """The passage has to contain the cite it is offered as evidence for."""
        for e in golden["edges"]:
            found = {str(c.ruling_id) for c in find_fallos_citations(e["quote"])}
            assert e["cited_id"] in found, (
                f"el pasaje de {e['citing']} -> {e['cited']} no contiene la cita"
            )

    def test_chain_edges_state_the_doctrine_of_the_court(self, golden: dict[str, Any]) -> None:
        """A citation in a concurrence or dissent is not the Court's holding.

        Arriola was decided with a two-judge majority and five separate votes, so
        this is not a formality: the same proposition appears in Lorenzetti's
        concurrence, and anchoring the chain there would misstate what binds.
        """
        for e in golden["edges"]:
            assert e["opinion"] == Opinion.MAJORITY.value, (
                f"{e['citing']} -> {e['cited']} quedó anclada en un {e['opinion']}"
            )


class TestChainSignals:
    """The chain, run through the aggregation the central tool depends on."""

    def _records(self, golden: dict[str, Any], cited: str) -> list[TreatmentRecord]:
        # decided_year comes from the citing ruling, exactly as
        # queries.treatments_of joins it. Without it the aggregation cannot order
        # departures against restorations, so leaving it out here would test a
        # different function than the one that runs in production.
        years = {r["id"]: r["decided_year"] for r in golden["rulings"]}
        out = []
        for e in golden["edges"]:
            if e["cited_id"] != cited:
                continue
            citing = RulingId.parse(e["citing_id"])
            assert citing is not None
            out.append(
                TreatmentRecord(
                    citing=citing,
                    treatment=Treatment(e["treatment"]),
                    opinion=Opinion(e["opinion"]),
                    confidence=e["confidence"],
                    quote=e["quote"],
                    method=Method(e["method"]),
                    decided_year=years.get(e["citing_id"]),
                )
            )
        return out

    def test_montalvo_is_red(self, golden: dict[str, Any]) -> None:
        """Arriola departed from Montalvo. That has to show as a red light."""
        subject = RulingId.parse(MONTALVO)
        assert subject is not None
        report = aggregate(subject, self._records(golden, MONTALVO))
        assert report.signal is Signal.RED
        assert report.binding_counts[Treatment.ABANDONED] == 1
        assert report.evidence[0].treatment is Treatment.ABANDONED
        assert any("herramienta de investigación" in c for c in report.caveats)

    def test_bazterrica_was_abandoned_then_restored(self, golden: dict[str, Any]) -> None:
        """The case that makes a time-blind citator wrong.

        Montalvo departed from Bazterrica in 1990; Arriola returned to it in
        2009. Reporting it red in 2026 would tell a lawyer to discard live
        authority. Reporting it green would hide that it spent nineteen years
        overruled. Yellow, with the history in the caveats.
        """
        subject = RulingId.parse(BAZTERRICA)
        assert subject is not None
        report = aggregate(subject, self._records(golden, BAZTERRICA))

        assert report.binding_counts[Treatment.ABANDONED] == 1
        assert report.binding_counts[Treatment.FOLLOWED] == 1
        assert report.signal is Signal.YELLOW
        assert not report.is_uniform
        assert any("abandonado y después retomado" in c for c in report.caveats)
        assert any("Fallos: 332:1963" in c for c in report.caveats)

    def test_a_bare_mention_never_turns_the_light_green(self, golden: dict[str, Any]) -> None:
        """Colavini is only mentioned by Arriola, so the corpus says nothing."""
        subject = RulingId.parse(COLAVINI)
        assert subject is not None
        report = aggregate(subject, self._records(golden, COLAVINI))
        assert report.signal is Signal.GRAY
        assert report.confidence <= 0.5


class TestChainProvenance:
    """Where the text of each link came from, and what that cost."""

    def test_old_rulings_needed_ocr_and_say_so(self, golden: dict[str, Any]) -> None:
        """Their PDFs carry a text layer, but through a broken font encoding.

        Recovered by rasterising and reading back with Tesseract, which takes the
        quality score from roughly 0.42-0.61 up over 0.87. The status records
        that the text is OCR output and not the publisher's own, because OCR
        makes mistakes that a reader checking a quote deserves to know about.
        """
        by_id = {r["id"]: r for r in golden["rulings"]}
        assert by_id[COLAVINI]["text_status"] == "ocr"
        assert by_id[BAZTERRICA]["text_status"] == "ocr"
        assert by_id[MONTALVO]["text_status"] == "ocr"
        assert by_id[ARRIOLA]["text_status"] == "extracted"

    def test_every_text_clears_the_quality_gate(self, golden: dict[str, Any]) -> None:
        for r in golden["rulings"]:
            assert r["text_quality"] >= 0.70, f"{r['short_name']} no llega al umbral"

    def test_nothing_is_left_pending(self, golden: dict[str, Any]) -> None:
        assert golden["pending"] == []

    def test_chain_is_complete(self, golden: dict[str, Any]) -> None:
        """The acceptance criterion. All five edges, reconstructed from real text."""
        expected = {
            (ARRIOLA, BAZTERRICA),
            (ARRIOLA, MONTALVO),
            (ARRIOLA, COLAVINI),
            (BAZTERRICA, COLAVINI),
            (MONTALVO, BAZTERRICA),
        }
        assert {(e["citing_id"], e["cited_id"]) for e in golden["edges"]} == expected


class TestImplicitOverruling:
    """The limitation the completed chain exposed, pinned down so it stays known.

    Doctrinally Bazterrica abandons Colavini -- Arriola says so in as many words.
    But Bazterrica's majority cites Colavini exactly once, in considerando 6, and
    cites it *approvingly*: it accepts Colavini's appraisal of how serious the
    drug problem is. The departure arrives two considerandos later ("sin embargo,
    no se debe presumir...") and never names the precedent.

    A citator built on the citation graph cannot detect that: there is no
    citation at the point of departure to classify. Recording the edge as
    ``abandoned`` anyway would mean asserting something the quoted passage does
    not support, which is the one thing the project must not do.
    """

    def test_bazterrica_cites_colavini_without_departing_from_it(
        self, golden: dict[str, Any]
    ) -> None:
        e = _edge(golden, BAZTERRICA, COLAVINI)
        assert e is not None
        assert e["treatment"] == Treatment.MENTIONED.value
        assert "valorado la magnitud del problema" in e["quote"]

    def test_the_limitation_travels_with_the_fixture(self, golden: dict[str, Any]) -> None:
        assert "implicit_overruling" in golden["notes"]
        assert "dictamen_del_procurador" in golden["notes"]

    def test_montalvo_by_contrast_departs_on_the_record(self, golden: dict[str, Any]) -> None:
        """Montalvo rejects Bazterrica with the cite right there, so it is catchable."""
        e = _edge(golden, MONTALVO, BAZTERRICA)
        assert e is not None
        assert e["treatment"] == Treatment.ABANDONED.value
        assert "No es compatible" in e["quote"]
