"""Security and robustness tests for the tool surface.

An MCP server is an attack surface twice over: it takes strings from a model and
it puts strings back into one. These are the checks that keep both directions
honest -- input that reaches SQL, and output that reaches a context window.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from citador_ar_mcp.ingest.build_graph import create_schema, load_fixture
from citador_ar_mcp.store import queries
from citador_ar_mcp.tools import citator
from citador_ar_mcp.tools._common import (
    MAX_QUERY_LENGTH,
    MAX_QUOTE_CHARS,
    MAX_TRACE_STEPS,
    CitadorError,
    render_quote,
)

#: Payloads aimed at the two query languages the lookup path touches: SQL, and
#: FTS5's own match syntax.
INJECTION = [
    "'; DROP TABLE rulings; --",
    '" OR rulings_fts MATCH "x',
    "' UNION SELECT quote FROM citations --",
    "arriola* AND (bazterrica OR NOT colavini)",
    "NEAR(arriola bazterrica, 5)",
    "^arriola",
    'col*"*"*',
    "\x00\x01arriola",
    "../../etc/passwd",
    "%' OR '1'='1",
]


@pytest.fixture(scope="module")
def graph_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    path = tmp_path_factory.mktemp("sec") / "citador.db"
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


class TestInjection:
    @pytest.mark.parametrize("payload", INJECTION)
    def test_lookup_never_executes_a_payload(self, payload: str) -> None:
        """Either it resolves or it raises a CitadorError. Never anything else."""
        with contextlib.suppress(CitadorError):
            citator.lookup_ruling(payload)

    @pytest.mark.parametrize("payload", INJECTION)
    def test_suggest_survives_fts_metacharacters(self, payload: str, graph_db: Path) -> None:
        """`suggest` builds an FTS5 MATCH query from user text.

        Normalisation strips every non-word character, so no FTS operator can
        survive into the query. A malformed one would raise OperationalError.
        """
        with queries.connect(graph_db) as conn:
            queries.suggest(conn, payload)

    def test_the_graph_is_unchanged_afterwards(self, graph_db: Path) -> None:
        with queries.connect(graph_db) as conn:
            for payload in INJECTION:
                with contextlib.suppress(CitadorError):
                    citator.lookup_ruling(payload)
            assert conn.execute("SELECT count(*) FROM rulings").fetchone()[0] == 4
            assert conn.execute("SELECT count(*) FROM citations").fetchone()[0] == 5

    def test_the_connection_is_read_only(self, graph_db: Path) -> None:
        """The tools must not be able to write. The graph is a build artefact."""
        with queries.connect(graph_db) as conn, pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM citations")


class TestInputBounds:
    def test_an_oversized_query_is_refused_before_the_database(self) -> None:
        with pytest.raises(CitadorError) as exc:
            citator.lookup_ruling("a" * (MAX_QUERY_LENGTH + 1))
        assert str(MAX_QUERY_LENGTH) in str(exc.value)

    def test_an_empty_query_says_what_to_pass_instead(self) -> None:
        with pytest.raises(CitadorError) as exc:
            citator.lookup_ruling("   ")
        assert "Fallos: 332:1963" in str(exc.value)

    def test_a_negative_offset_does_not_reach_sql(self) -> None:
        data = json.loads(citator.citing_rulings("Bazterrica", offset=-100, response_format="json"))
        assert data["offset"] == 0

    def test_limit_is_clamped_at_both_ends(self) -> None:
        big = json.loads(citator.cited_rulings("Arriola", limit=10**9, response_format="json"))
        small = json.loads(citator.cited_rulings("Arriola", limit=-5, response_format="json"))
        assert big["count"] <= 100
        assert small["count"] >= 1

    def test_max_depth_is_clamped(self) -> None:
        data = json.loads(
            citator.trace_doctrine("Bazterrica", max_depth=10**6, response_format="json")
        )
        assert data["max_depth"] <= 6


class TestOutputBounds:
    """A tool that can fill a context window is a tool that can end a session."""

    def test_the_chain_is_capped_and_says_it_was(self, tmp_path: Path) -> None:
        """Measured before the cap existed: 2.020 steps, ~445.000 tokens.

        A leading case in the full corpus is cited by hundreds of later rulings,
        and the walk multiplies that by depth.
        """
        dense = tmp_path / "dense.db"
        conn = sqlite3.connect(dense)
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        quote = "pasaje de prueba " * 30
        n = 120
        for i in range(1, n + 1):
            conn.execute(
                "INSERT INTO rulings (id, volume, page, caption, decided_year, "
                "source_url, text_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"fallos:300:{i}", 300, i, f"Caso {i}", 1980 + i % 40, "u", "extracted"),
            )
        for i in range(1, n + 1):
            for j in range(i + 1, min(i + 21, n + 1)):
                conn.execute(
                    "INSERT INTO citations (citing_id, cited_id, treatment, opinion, "
                    "confidence, quote, method) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"fallos:300:{j}",
                        f"fallos:300:{i}",
                        "followed",
                        "majority",
                        0.9,
                        quote + str(j),
                        "rule",
                    ),
                )
        conn.commit()
        conn.close()

        import os

        os.environ["CITADOR_DB"] = str(dense)
        try:
            raw = citator.trace_doctrine("300:1", max_depth=6, response_format="json")
            data = json.loads(raw)
            assert len(data["steps"]) <= MAX_TRACE_STEPS
            assert data["truncated"] is True
            assert data["omitted_steps"] > 0
            # Comfortably inside any context window.
            assert len(raw) < 60_000

            markdown = citator.trace_doctrine("300:1", max_depth=6)
            assert "eslabones de" in markdown
            assert len(markdown) < 60_000
        finally:
            os.environ.pop("CITADOR_DB", None)


class TestQuoteRendering:
    """Passages come out of a file that ships as a downloadable release asset.

    Nothing in the schema bounds them and nothing guarantees this code wrote
    them, so they are untrusted input on the way into a model's context.
    """

    def test_newlines_cannot_escape_the_blockquote(self) -> None:
        rendered = render_quote("primera línea\n# Encabezado inyectado\nsegunda")
        assert rendered.count("\n") == 0
        assert rendered.startswith("> ")

    def test_backticks_are_neutralised(self) -> None:
        assert "`" not in render_quote("un ``` bloque ``` de código")

    def test_nested_blockquote_markers_are_neutralised(self) -> None:
        assert render_quote("> falso pasaje anidado").count(">") == 1

    def test_an_oversized_passage_is_capped(self) -> None:
        rendered = render_quote("x" * 100_000)
        assert len(rendered) < MAX_QUOTE_CHARS + 100
        assert "recortado" in rendered

    def test_ordinary_text_is_left_alone(self) -> None:
        """It is evidence. Evidence that has been rewritten is not evidence."""
        passage = "Que este Tribunal ha valorado la magnitud del problema en Fallos: 300:254."
        assert render_quote(passage) == f"> {passage}"

    def test_responses_with_passages_frame_them_as_source_material(self) -> None:
        out = citator.check_status("Montalvo")
        assert "no instrucciones" in out
