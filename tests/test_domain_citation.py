"""Unit tests for the citation parser. No network, no PDF, no database."""

from __future__ import annotations

import pytest

from citador_ar_mcp.domain.citation import (
    CitationForm,
    RulingId,
    find_fallos_citations,
    normalize_caption,
    normalize_expediente,
    short_name_key,
)


class TestRulingId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Fallos: 332:1963", "fallos:332:1963"),
            ("Fallos 332:1963", "fallos:332:1963"),
            ("fallos:332:1963", "fallos:332:1963"),
            ("332:1963", "fallos:332:1963"),
            ("  Fallos:  332 : 1963  ", "fallos:332:1963"),
            # The CSJN prints a space after the second colon often enough to matter.
            ("Fallos: 308: 1392", "fallos:308:1392"),
        ],
    )
    def test_parses_every_written_form(self, raw: str, expected: str) -> None:
        rid = RulingId.parse(raw)
        assert rid is not None
        assert str(rid) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "Arriola",
            "A. 891. XLIV",
            "Fallos 332",
            "1963",
            "0:1963",  # tomo 0 does not exist
            "9999:1",  # beyond any plausible tomo
        ],
    )
    def test_refuses_what_it_cannot_be_sure_of(self, raw: str) -> None:
        assert RulingId.parse(raw) is None

    def test_human_form_round_trips(self) -> None:
        rid = RulingId.parse("Fallos: 332:1963")
        assert rid is not None
        assert rid.human == "Fallos: 332:1963"
        assert RulingId.parse(rid.human) == rid

    def test_is_hashable_and_frozen(self) -> None:
        a, b = RulingId(volume=332, page=1963), RulingId(volume=332, page=1963)
        assert a == b
        assert len({a, b}) == 1
        with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
            a.volume = 1  # type: ignore[misc]


class TestFindFallosCitations:
    def test_finds_a_single_reference(self) -> None:
        cites = find_fallos_citations("doctrina de Fallos: 308:1392, que el Tribunal comparte")
        assert [str(c.ruling_id) for c in cites] == ["fallos:308:1392"]
        assert cites[0].form is CitationForm.FALLOS

    def test_pinpoint_page_is_not_a_second_ruling(self) -> None:
        """`Fallos: 331:2691, 2699` is one ruling cited at a page, not two rulings."""
        cites = find_fallos_citations("ver Fallos: 331:2691, 2699")
        assert [str(c.ruling_id) for c in cites] == ["fallos:331:2691"]

    def test_run_of_references_yields_each_ruling(self) -> None:
        cites = find_fallos_citations("Fallos: 301:341; 302:1284 y 303:1029")
        assert [str(c.ruling_id) for c in cites] == [
            "fallos:301:341",
            "fallos:302:1284",
            "fallos:303:1029",
        ]

    def test_run_mixing_pinpoints_and_rulings(self) -> None:
        cites = find_fallos_citations("Fallos: 331:2691, 2699; 332:1963")
        assert [str(c.ruling_id) for c in cites] == ["fallos:331:2691", "fallos:332:1963"]

    def test_the_real_arriola_passage(self) -> None:
        """The considerando that carries three edges at once. Verbatim from the PDF."""
        passage = (
            'Así en "Colavini" (Fallos: 300:254) se pronunció a favor de la '
            'criminalización; en "Bazterrica" y "Capalbo", se apartó de tal doctrina '
            '(Fallos: 308:1392); y en 1990, en "Montalvo" vuelve nuevamente sobre sus '
            "pasos a favor de la criminalización de la tenencia para consumo personal "
            "(Fallos: 313:1333)"
        )
        assert [str(c.ruling_id) for c in find_fallos_citations(passage)] == [
            "fallos:300:254",
            "fallos:308:1392",
            "fallos:313:1333",
        ]

    def test_spans_point_at_the_text_that_produced_them(self) -> None:
        text = "el precedente Fallos: 332:1963 resolvió"
        (cite,) = find_fallos_citations(text)
        assert text[cite.start : cite.end] == "332:1963"

    def test_ignores_numbers_that_are_not_citations(self) -> None:
        assert find_fallos_citations("la ley 23.737, artículo 14, segundo párrafo") == []
        assert find_fallos_citations("a las 14:30 del 25 de agosto") == []


class TestNormalizeExpediente:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("A. 891. XLIV. RHE", "A.891.XLIV"),
            ("A. 891. XLIV", "A.891.XLIV"),
            ("A.891.XLIV", "A.891.XLIV"),
            ("CSJ 001086/2022/CS001", "CSJ.1086/2022/CS001"),
            ("CNE 006781/2017/CS001", "CNE.6781/2017/CS001"),
        ],
    )
    def test_collapses_written_variants(self, raw: str, expected: str) -> None:
        assert normalize_expediente(raw) == expected

    @pytest.mark.parametrize("raw", ["CCC.", "CCCVIII.", "CCCXIII", "", "   "])
    def test_rejects_the_roman_tomo_placeholder(self, raw: str) -> None:
        """The API puts the tomo in roman numerals where old rulings have no docket.

        `CCC.` is tomo 300, not an expediente. Storing it as an alias would map
        every 1978 ruling onto the same key.
        """
        assert normalize_expediente(raw) is None


class TestNormalizeCaption:
    def test_two_transcriptions_of_arriola_collapse(self) -> None:
        a = normalize_caption("ARRIOLA SEBASTIAN Y OTROS s/CAUSA N° 9080")
        b = normalize_caption("Arriola, Sebastián y otros s/ causa n° 9080.")
        assert a == b == "ARRIOLA SEBASTIAN"

    def test_folds_accents(self) -> None:
        assert normalize_caption("Asociación") == normalize_caption("Asociacion")

    def test_short_name_strips_quotes(self) -> None:
        assert short_name_key('"Arriola"') == "ARRIOLA"
        assert short_name_key("“Bazterrica”") == "BAZTERRICA"
        assert short_name_key("Montalvo") == "MONTALVO"
