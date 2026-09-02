"""Tests for the tool layer, against a graph built from the golden fixture."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from citador_ar_mcp.domain.treatment import Treatment
from citador_ar_mcp.ingest.build_graph import create_schema, load_fixture
from citador_ar_mcp.tools import citator
from citador_ar_mcp.tools._common import CitadorError


@pytest.fixture(scope="module")
def graph_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A real SQLite graph containing the golden chain."""
    path = tmp_path_factory.mktemp("graph") / "citador.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        load_fixture(conn)
        conn.commit()
    finally:
        conn.close()
    yield path


@pytest.fixture(autouse=True)
def _point_at_graph(graph_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITADOR_DB", str(graph_db))


class TestLookup:
    @pytest.mark.parametrize(
        "query",
        ["Fallos: 332:1963", "fallos:332:1963", "332:1963", "Arriola", "A. 891. XLIV"],
    )
    def test_every_written_form_reaches_arriola(self, query: str) -> None:
        out = citator.lookup_ruling(query, "json")
        assert json.loads(out)["id"] == "fallos:332:1963"

    def test_json_lists_the_known_aliases(self) -> None:
        data = json.loads(citator.lookup_ruling("Arriola", "json"))
        assert "A.891.XLIV" in data["aliases"]
        assert "ARRIOLA" in data["aliases"]

    def test_markdown_carries_the_disclaimer(self) -> None:
        out = citator.lookup_ruling("Arriola")
        assert "no asesoramiento legal" in out
        assert "sjconsulta.csjn.gov.ar" in out

    def test_a_typo_gets_a_suggestion_not_a_shrug(self) -> None:
        with pytest.raises(CitadorError) as exc:
            citator.lookup_ruling("Bazterica")
        message = str(exc.value)
        assert "Bazterrica" in message
        assert "308:1392" in message

    def test_ocr_provenance_is_disclosed(self) -> None:
        """A reader checking a quote deserves to know it came from OCR."""
        out = citator.lookup_ruling("Bazterrica")
        assert "OCR" in out


class TestCheckStatus:
    def test_montalvo_is_red_with_its_passage(self) -> None:
        data = json.loads(citator.check_status("Montalvo", "json"))["report"]
        assert data["signal"] == "red"
        assert data["binding_counts"]["abandoned"] == 1
        assert data["evidence"][0]["quote"]
        assert data["evidence"][0]["binding"] is True

    def test_every_piece_of_evidence_carries_confidence_and_method(self) -> None:
        """CLAUDE.md section 4, rule 5: no claim without confidence and source."""
        data = json.loads(citator.check_status("Montalvo", "json"))["report"]
        for e in data["evidence"]:
            assert 0.0 <= e["confidence"] <= 1.0
            assert e["method"] in {"rule", "llm", "manual"}
            assert e["quote"].strip()

    def test_gray_says_it_is_not_a_clean_bill_of_health(self) -> None:
        out = citator.check_status("Colavini")
        assert "sin tratamiento registrado" in out

    def test_markdown_shows_the_signal_and_the_breakdown(self) -> None:
        out = citator.check_status("Montalvo")
        assert "🔴" in out
        assert "doctrina abandonada" in out
        assert "Tratamiento por la mayoría" in out
        assert "> " in out  # the quote, as a blockquote

    def test_response_carries_corpus_provenance(self) -> None:
        data = json.loads(citator.check_status("Montalvo", "json"))
        assert data["corpus"]["source"] == "fixture"
        assert data["corpus"]["built_on"]


class TestPagination:
    def test_citing_reports_every_required_counter(self) -> None:
        data = json.loads(citator.citing_rulings("Bazterrica", response_format="json"))
        for key in ("total", "count", "offset", "has_more", "next_offset"):
            assert key in data
        assert data["total"] == 2
        assert data["has_more"] is False
        assert data["next_offset"] is None

    def test_offset_past_the_end_is_empty_not_an_error(self) -> None:
        data = json.loads(citator.citing_rulings("Bazterrica", offset=50, response_format="json"))
        assert data["count"] == 0
        assert data["total"] == 2

    def test_treatment_filter_narrows_the_result(self) -> None:
        abandoned = json.loads(
            citator.citing_rulings("Montalvo", treatment="abandoned", response_format="json")
        )
        followed = json.loads(
            citator.citing_rulings("Montalvo", treatment="followed", response_format="json")
        )
        assert abandoned["total"] == 1
        assert followed["total"] == 0

    def test_an_invalid_treatment_lists_the_valid_ones(self) -> None:
        with pytest.raises(CitadorError) as exc:
            citator.citing_rulings("Montalvo", treatment="overruled")
        assert all(t.value in str(exc.value) for t in Treatment)

    def test_cited_rulings_walks_the_other_direction(self) -> None:
        data = json.loads(citator.cited_rulings("Arriola", response_format="json"))
        assert data["total"] == 3
        assert {i["id"] for i in data["items"]} == {
            "fallos:300:254",
            "fallos:308:1392",
            "fallos:313:1333",
        }

    def test_limit_is_clamped(self) -> None:
        data = json.loads(citator.cited_rulings("Arriola", limit=10_000, response_format="json"))
        assert data["count"] <= 100


class TestTraceDoctrine:
    def test_traces_forward_from_bazterrica_to_arriola(self) -> None:
        data = json.loads(citator.trace_doctrine("Bazterrica", response_format="json"))
        steps = [(s["from"], s["to"], s["treatment"]) for s in data["steps"]]
        assert ("fallos:308:1392", "fallos:332:1963", "followed") in steps

    def test_bare_mentions_are_not_a_chain(self) -> None:
        """Colavini is only mentioned, so tracing it must not invent a line."""
        data = json.loads(citator.trace_doctrine("Colavini", response_format="json"))
        assert data["steps"] == []

    def test_every_step_carries_its_passage(self) -> None:
        data = json.loads(citator.trace_doctrine("Montalvo", response_format="json"))
        for s in data["steps"]:
            assert s["quote"].strip()
            assert 0.0 <= s["confidence"] <= 1.0
