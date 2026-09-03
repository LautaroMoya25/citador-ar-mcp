"""Tests for the ingest pipeline. No network, no PDFs, no Tesseract required."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from citador_ar_mcp.domain.citation import RulingId
from citador_ar_mcp.domain.treatment import Opinion
from citador_ar_mcp.ingest import ocr
from citador_ar_mcp.ingest.build_graph import (
    add_source,
    create_schema,
    done_volumes,
    mark_done,
    stamp_provenance,
    store_sumario,
)
from citador_ar_mcp.ingest.citations import (
    find_citations,
    join_page_breaks,
    split_opinions,
)
from citador_ar_mcp.ingest.extract import (
    HYPHEN_BREAK,
    QUALITY_FLOOR,
    TextStatus,
    classify,
    clean_pdf_text,
    text_quality,
)
from citador_ar_mcp.ingest.fetch import (
    CsjnClient,
    SessionExpiredError,
    Sumario,
    parse_citing,
    parse_sumario,
    strip_html,
)

# Verbatim from Bazterrica's PDF text layer, whose font subset has no usable
# ToUnicode map. Reads as `que en consecuencia` on the page.
CIPHERED = "V !B)V )7V %87@)%B)7%.#V &)2V ;)?@97#2V %97@B69V 2#V ?)@92B%.T7V"

REAL = (
    "Que este Tribunal ha valorado la magnitud del problema de la drogadicción "
    "en Fallos: 300:254, en que destacó la deletérea influencia de la creciente "
    "difusión actual de toxicomanía en el mundo entero."
)


class TestCleanPdfText:
    def test_rejoins_words_split_by_the_hyphen_marker(self) -> None:
        """The CSJN marks a hyphenated line break with U+FFFE, not a hyphen."""
        assert clean_pdf_text(f"juris{HYPHEN_BREAK}prudencia") == "jurisprudencia"

    def test_collapses_runs_of_spaces_but_keeps_paragraphs(self) -> None:
        assert clean_pdf_text("uno   dos\n\n\n\ntres") == "uno dos\n\ntres"


class TestTextQuality:
    def test_real_spanish_scores_high(self) -> None:
        assert text_quality(REAL) >= 0.80

    def test_ciphered_text_scores_low(self) -> None:
        """The failure this gate exists to catch. See the module docstring."""
        assert text_quality(CIPHERED) < 0.30

    def test_the_gate_separates_them(self) -> None:
        assert classify(REAL)[1] is TextStatus.EXTRACTED
        assert classify(CIPHERED)[1] is TextStatus.GARBLED

    def test_empty_text_is_unavailable_not_garbled(self) -> None:
        assert classify("   ")[1] is TextStatus.UNAVAILABLE

    def test_floor_sits_between_the_measured_populations(self) -> None:
        # Garbled rulings measured 0.42-0.61; clean ones 0.77-0.89.
        assert 0.61 < QUALITY_FLOOR < 0.77


class TestPageBreaks:
    def test_rejoins_a_heading_split_across_a_page_boundary(self) -> None:
        """`VO-//- <page furniture> -//-TO DEL SEÑOR` is the heading `VOTO DEL SEÑOR`.

        Verbatim shape from Arriola. A parser that does not rejoin this finds no
        headings at all and files every separate opinion under the majority.
        """
        raw = (
            "Hágase saber y devuélvase. RICARDO LUIS LORENZETTI. ES COPIA VO-//- -42- "
            "A. 891. XLIV. RECURSO DE HECHO Arriola, Sebastián y otros s/ causa n 9080. "
            "-43- -//-TO DEL SEÑOR MINISTRO DOCTOR DON CARLOS S. FAYT Considerando:"
        )
        assert "VOTO DEL SEÑOR MINISTRO" in join_page_breaks(raw)

    def test_leaves_ordinary_text_alone(self) -> None:
        assert join_page_breaks("texto normal sin cortes") == "texto normal sin cortes"


class TestSplitOpinions:
    def test_a_document_with_no_headings_is_all_majority(self) -> None:
        (span,) = split_opinions("Considerando: 1) Que el recurso es admisible.")
        assert span.opinion is Opinion.MAJORITY

    def test_detects_concurrence_and_dissent(self) -> None:
        text = (
            "Considerando: 1) Que corresponde. "
            "VOTO DEL SEÑOR MINISTRO DOCTOR DON CARLOS S. FAYT Considerando: 2) Que. "
            "DISIDENCIA DEL SEÑOR PRESIDENTE DOCTOR DON JOSÉ SEVERO CABALLERO "
            "Considerando: 3) Que."
        )
        kinds = [s.opinion for s in split_opinions(text)]
        assert kinds == [Opinion.MAJORITY, Opinion.CONCURRENCE, Opinion.DISSENT]

    def test_the_procurador_is_segmented_apart_from_the_court(self) -> None:
        """Printed volumes carry the dictamen before the ruling, in one document.

        In Bazterrica the Procurador argued for keeping Colavini and the Court
        went the other way, so attributing his citations to the Court would
        invert the result outright.
        """
        text = (
            "Sumarios del fallo. "
            "DICTAMEN DEL PROCURADOR GENERAL Suprema Corte: opino que corresponde "
            "confirmar, con cita de Fallos: 300:254. "
            "FALLO DE LA CORTE SUPREMA Buenos Aires, 29 de agosto de 1986. "
            "Vistos los autos: Considerando: 1) Que corresponde revocar."
        )
        spans = split_opinions(text)
        assert Opinion.DICTAMEN in [s.opinion for s in spans]

        (cite,) = find_citations(text)
        assert cite.opinion is Opinion.DICTAMEN
        assert not cite.opinion.is_binding


class TestFindCitations:
    def test_a_citation_carries_the_passage_that_contains_it(self) -> None:
        (cite,) = find_citations(REAL)
        assert str(cite.cited) == "fallos:300:254"
        assert "magnitud del problema" in cite.quote

    def test_self_references_are_dropped(self) -> None:
        """A ruling's own cite appears in its running header on every page."""
        from citador_ar_mcp.domain.citation import RulingId

        assert find_citations(REAL, exclude=RulingId.build(300, 254)) == []


class TestFetchParsing:
    def test_strip_html_flattens_the_api_fields(self) -> None:
        assert strip_html("<p>Cabe declarar la <b>inconstitucionalidad</b></p>") == (
            "Cabe declarar la inconstitucionalidad"
        )
        assert strip_html(None) == ""

    def test_parse_citing_reads_the_accordion(self) -> None:
        blob = (
            "<div><a href='/x'>Fallos: 347:1031</a></div>"
            "<div><a href='/y'>CSJ 001086/2022/CS001</a></div>"
            "<div><a href='/z'>Fallos: 347:688</a></div>"
        )
        got = [str(r) for r in parse_citing(blob)]
        assert got == ["fallos:347:1031", "fallos:347:688"]

    def test_docket_numbers_are_not_guessed_into_nodes(self) -> None:
        """A docket number cannot be resolved without the alias table."""
        assert parse_citing("<a>CSJ 001086/2022/CS001</a>") == ()

    def test_old_rulings_yield_a_year_but_no_date(self) -> None:
        """`decided_on` is nullable in the schema precisely because of this."""
        raw = {
            "id": 1,
            "tomo": 300,
            "pagina": 254,
            "caratulaWeb": "Colavini, Ariel Omar.",
            "fechaString": "1978",
            "anioFallo": 1978,
            "numeroExpediente": "CCC. ",
            "idFallo": 7834791,
        }
        s = parse_sumario(raw)
        assert s.decided_on is None
        assert s.decided_year == 1978
        assert str(s.ruling_id) == "fallos:300:254"

    def test_modern_rulings_yield_a_full_date(self) -> None:
        raw = {
            "id": 2,
            "tomo": 332,
            "pagina": 1963,
            "caratulaWeb": "ARRIOLA SEBASTIAN Y OTROS s/CAUSA N 9080",
            "fechaString": "25/08/2009",
            "anioFallo": 2009,
            "numeroExpediente": "A. 891. XLIV. RHE",
            "idFallo": 6711401,
        }
        s = parse_sumario(raw)
        assert s.decided_on == "25/08/2009"
        assert s.expediente == "A. 891. XLIV. RHE"


class TestOcrDiscovery:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / "tesseract.exe"
        fake.write_text("")
        monkeypatch.setenv("CITADOR_TESSERACT", str(fake))
        assert ocr.find_tesseract() == fake
        assert ocr.available()

    def test_a_bad_override_does_not_fall_back_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CITADOR_TESSERACT", str(tmp_path / "nope.exe"))
        assert ocr.find_tesseract() is None

    def test_the_error_says_how_to_install_it(self) -> None:
        message = str(ocr.TesseractUnavailableError())
        assert "winget" in message
        assert "spa" in message


class TestCrawlRobustness:
    """The crawl is meant to survive dying halfway. The first full run proved it did not.

    It died on tomo 330, 350 requests and about a gigabyte in, on a single
    self-citing sumario, and left an empty database because the transaction only
    committed once per tomo.
    """

    def _graph(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "g.db")
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        return conn

    def _sumario(self, citing: tuple[RulingId, ...]) -> Sumario:
        rid = RulingId.build(330, 100)
        assert rid is not None
        return Sumario(
            sumario_id=1,
            ruling_id=rid,
            caption="Caso de prueba",
            decided_year=2007,
            decided_on="01/01/2007",
            expediente=None,
            doc_id=None,
            text="",
            voces="",
            votes_majority="",
            votes_concurrence="",
            votes_dissent="",
            votes_partial_dissent="",
            citing=citing,
        )

    def test_a_ruling_listed_among_its_own_citers_is_not_a_self_edge(self, tmp_path: Path) -> None:
        """`linksCitantes` is per sumario, so a ruling can list itself.

        The schema refuses a self-edge; before this was filtered, one such row
        aborted the entire crawl.
        """
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        other = RulingId.build(347, 688)
        assert me is not None and other is not None

        edges = store_sumario(conn, self._sumario((me, other)))
        conn.commit()

        assert edges == 1
        rows = conn.execute("SELECT citing_id, cited_id FROM citations").fetchall()
        assert rows == [("fallos:347:688", "fallos:330:100")]

    def test_a_sumario_that_only_cites_itself_writes_the_node_anyway(self, tmp_path: Path) -> None:
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        assert me is not None

        assert store_sumario(conn, self._sumario((me,))) == 0
        conn.commit()
        assert conn.execute("SELECT count(*) FROM rulings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM citations").fetchone()[0] == 0

    def test_completed_tomos_are_recorded_so_a_restart_can_skip_them(self, tmp_path: Path) -> None:
        """Restarting must not re-download the six gigabytes already fetched."""
        conn = self._graph(tmp_path)
        assert done_volumes(conn) == set()
        mark_done(conn, 331)
        mark_done(conn, 330)
        conn.commit()
        assert done_volumes(conn) == {330, 331}


class TestPageRetry:
    """One 504 in about 1.500 requests, seen in a real crawl of tomos 330-349.

    The next request succeeded immediately, so the failure was transient and the
    page's ten sumarios were lost for no reason.
    """

    class _Response:
        def __init__(self, status: int, payload: object = None) -> None:
            self.status_code = status
            self._payload = payload if payload is not None else []
            self.request = httpx.Request("GET", "https://example.test")

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=self.request, response=self)  # type: ignore[arg-type]

        def json(self) -> object:
            return self._payload

    class _Client:
        def __init__(self, statuses: list[int]) -> None:
            self.statuses = statuses
            self.calls = 0

        async def get(self, url: str, params: object = None) -> object:
            status = self.statuses[min(self.calls, len(self.statuses) - 1)]
            self.calls += 1
            return TestPageRetry._Response(status, [{"id": 1}])

    def _client(self, statuses: list[int]) -> CsjnClient:
        c = CsjnClient(delay=0.0)
        c._client = TestPageRetry._Client(statuses)  # type: ignore[assignment]
        return c

    async def test_a_transient_504_is_retried_and_succeeds(self) -> None:
        csjn = self._client([504, 200])
        assert await csjn.page(620) == [{"id": 1}]
        assert csjn._client.calls == 2  # type: ignore[union-attr]

    async def test_it_gives_up_after_the_allowed_attempts(self) -> None:
        csjn = self._client([504])
        with pytest.raises(httpx.HTTPStatusError):
            await csjn.page(620, attempts=3)
        assert csjn._client.calls == 3  # type: ignore[union-attr]

    async def test_a_401_is_not_retried_because_it_is_not_transient(self) -> None:
        """It means the session lost the search; the caller has to re-run it."""
        csjn = self._client([401, 200])
        with pytest.raises(SessionExpiredError):
            await csjn.page(620)
        assert csjn._client.calls == 1  # type: ignore[union-attr]

    async def test_a_client_error_is_not_retried_either(self) -> None:
        csjn = self._client([404])
        with pytest.raises(httpx.HTTPStatusError):
            await csjn.page(620)
        assert csjn._client.calls == 1  # type: ignore[union-attr]


class TestProvenance:
    """What ``corpus_meta`` claims has to describe the file the reader has.

    Each ingest step used to stamp its own ``source``, so whichever ran last
    described the whole graph. A corpus crawled from twenty tomos and then topped
    up with the golden fixture came out labelled ``fixture``, with a note reading
    "solo la cadena dorada. No es el corpus completo" -- the one description that
    makes five thousand real rulings look like a toy, and it was a command away
    from being published on the release page.
    """

    def _graph(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "g.db")
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        return conn

    def _meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        return dict(conn.execute("SELECT key, value FROM corpus_meta"))

    def test_a_later_step_does_not_erase_an_earlier_source(self, tmp_path: Path) -> None:
        conn = self._graph(tmp_path)
        add_source(conn, "csjn-crawl")
        add_source(conn, "fixture dorado anotado a mano")
        assert self._meta(conn)["source"] == "csjn-crawl + fixture dorado anotado a mano"

    def test_the_same_step_run_twice_is_recorded_once(self, tmp_path: Path) -> None:
        conn = self._graph(tmp_path)
        add_source(conn, "csjn-pipeline")
        add_source(conn, "csjn-pipeline")
        assert self._meta(conn)["source"] == "csjn-pipeline"

    def test_stub_nodes_are_counted_apart_from_crawled_rulings(self, tmp_path: Path) -> None:
        """A cited precedent outside the crawled range is a node without a ruling.

        Reporting the two together inflates the corpus: the published graph has
        2.628 such stubs against 5.325 real ones, and they carry no text at all.
        """
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        outside = RulingId.build(347, 688)
        assert me is not None and outside is not None
        store_sumario(conn, TestCrawlRobustness()._sumario((me, outside)))
        mark_done(conn, 330)
        stamp_provenance(conn)

        note = self._meta(conn)["note"]
        assert "1 fallos crawleados de los tomos 330-330" in note
        assert "1 nodos stub" in note

    def test_an_unclassified_edge_is_not_reported_as_a_neutral_one(self, tmp_path: Path) -> None:
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        outside = RulingId.build(347, 688)
        assert me is not None and outside is not None
        store_sumario(conn, TestCrawlRobustness()._sumario((me, outside)))
        stamp_provenance(conn)

        meta = self._meta(conn)
        assert "0 de 1 aristas (0.0%)" in meta["cobertura_clasificacion"]
        assert "no cita neutral verificada" in meta["cobertura_clasificacion"]
        assert "0 de 1 aristas (0.0%)" in meta["cobertura_atribucion"]

    def test_attribution_and_licence_survive_a_rebuild(self, tmp_path: Path) -> None:
        """The contract requires the derived corpus to carry its attribution."""
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        outside = RulingId.build(347, 688)
        assert me is not None and outside is not None
        store_sumario(conn, TestCrawlRobustness()._sumario((me, outside)))
        stamp_provenance(conn)

        meta = self._meta(conn)
        assert "CSJN" in meta["fuente_oficial"]
        assert "sjconsulta" in meta["fuente_oficial"]
        assert "MIT" in meta["licencia"]

    def test_a_graph_with_no_crawl_says_it_is_not_the_whole_corpus(self, tmp_path: Path) -> None:
        conn = self._graph(tmp_path)
        me = RulingId.build(330, 100)
        outside = RulingId.build(347, 688)
        assert me is not None and outside is not None
        store_sumario(conn, TestCrawlRobustness()._sumario((me, outside)))
        stamp_provenance(conn)
        assert "No es el corpus completo" in self._meta(conn)["note"]
