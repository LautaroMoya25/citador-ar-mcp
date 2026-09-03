"""Read-only access to the graph.

Every function here opens the database in read-only mode. The tools must not be
able to write: the graph is a build artefact produced offline by ``ingest/`` and
shipped as a file, so a write path from a tool would mean the answers change
between calls (CLAUDE.md, section 2).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from citador_ar_mcp.config import db_path
from citador_ar_mcp.domain.citation import (
    RulingId,
    normalize_caption,
    normalize_expediente,
    short_name_key,
)
from citador_ar_mcp.domain.signals import TreatmentRecord
from citador_ar_mcp.domain.treatment import Method, Opinion, Treatment


class GraphUnavailableError(RuntimeError):
    """The database is missing. Carries an actionable message, not just a fact."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"No encontré el grafo en {path}. "
            "El .db no se versiona: se genera con el pipeline de ingesta o se baja "
            "del release. Corré `uv run --extra ingest python -m "
            "citador_ar_mcp.ingest.build_graph` "
            "o definí CITADOR_DB apuntando al archivo."
        )
        self.path = path


class Ruling(BaseModel):
    """A node of the graph, as the tools see it."""

    model_config = ConfigDict(frozen=True)

    id: str
    volume: int
    page: int
    caption: str
    short_name: str | None
    decided_on: str | None
    decided_year: int
    source_url: str
    text_status: str

    @property
    def ruling_id(self) -> RulingId:
        rid = RulingId.build(self.volume, self.page)
        if rid is None:  # pragma: no cover - the schema constrains this
            raise ValueError(f"posición fuera de rango: {self.volume}:{self.page}")
        return rid

    @property
    def human(self) -> str:
        name = f' "{self.short_name}"' if self.short_name else ""
        return f"Fallos: {self.volume}:{self.page}{name}"


@dataclass(frozen=True, slots=True)
class Page:
    """One page of results, with the counters the tools are required to report."""

    total: int
    offset: int
    items: list[Ruling]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def has_more(self) -> bool:
        return self.offset + self.count < self.total

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.count if self.has_more else None


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the graph read-only. Raises :class:`GraphUnavailableError` when absent."""
    target = path or db_path()
    if not target.exists():
        raise GraphUnavailableError(target)

    # Percent-encode the path before it becomes a URI. `?` and `#` are legal in
    # POSIX filenames and are the URI's own delimiters, so a path containing one
    # would either break the connection or -- worse -- append its own query
    # parameters after ours: `/data/x?mode=rwc/citador.db` would hand back a
    # writable handle to a server that must never write.
    uri = f"file:{quote(target.as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


_COLUMNS = (
    "id",
    "volume",
    "page",
    "caption",
    "short_name",
    "decided_on",
    "decided_year",
    "source_url",
    "text_status",
)
_RULING_COLS = ", ".join(_COLUMNS)
_R_COLS = ", ".join(f"r.{c}" for c in _COLUMNS)


def _row_to_ruling(row: sqlite3.Row) -> Ruling:
    return Ruling(
        id=row["id"],
        volume=row["volume"],
        page=row["page"],
        caption=row["caption"],
        short_name=row["short_name"],
        decided_on=row["decided_on"],
        decided_year=row["decided_year"],
        source_url=row["source_url"],
        text_status=row["text_status"],
    )


def get_ruling(conn: sqlite3.Connection, ruling_id: str) -> Ruling | None:
    row = conn.execute(f"SELECT {_RULING_COLS} FROM rulings WHERE id = ?", (ruling_id,)).fetchone()
    return _row_to_ruling(row) if row else None


def resolve(conn: sqlite3.Connection, raw: str) -> Ruling | None:
    """Resolve any written form of a citation to a ruling.

    Tries, in order: the canonical cite, the alias table under each
    normalisation, and finally the short name. Returns ``None`` rather than
    guessing; :func:`suggest` exists to turn that into a useful error.
    """
    text = raw.strip()
    if not text:
        return None

    rid = RulingId.parse(text)
    if rid is not None:
        found = get_ruling(conn, str(rid))
        if found is not None:
            return found

    keys = [text, short_name_key(text), normalize_caption(text)]
    if (exp := normalize_expediente(text)) is not None:
        keys.append(exp)

    for key in keys:
        row = conn.execute(
            f"SELECT {_RULING_COLS} FROM rulings WHERE id = "
            "(SELECT ruling_id FROM aliases WHERE raw = ?)",
            (key,),
        ).fetchone()
        if row:
            return _row_to_ruling(row)
    return None


def _typo_pages(page: int) -> list[int]:
    """Page numbers one plausible slip away from ``page``.

    Adjacent-digit transpositions, then single-digit substitutions. These are
    the mistakes people actually make copying a cite out of a brief, and they
    are not near the original numerically -- swapping two digits can move the
    number by hundreds.
    """
    digits = str(page)
    out: list[int] = []
    for i in range(len(digits) - 1):
        swapped = list(digits)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        candidate = int("".join(swapped))
        if candidate != page:
            out.append(candidate)
    for i, original in enumerate(digits):
        for replacement in "0123456789":
            if replacement == original:
                continue
            changed = digits[:i] + replacement + digits[i + 1 :]
            candidate = int(changed)
            if candidate != page and candidate > 0:
                out.append(candidate)
    return list(dict.fromkeys(out))


def suggest(conn: sqlite3.Connection, raw: str, limit: int = 5) -> list[Ruling]:
    """Near matches for something that did not resolve.

    Feeds the actionable-error requirement: "no encontrado" is useless, "did you
    mean Fallos: 331:2691 (Arriola)?" is not.
    """
    text = raw.strip()
    out: dict[str, Ruling] = {}

    rid = RulingId.parse(text)
    if rid is not None:
        # Typo candidates before numeric neighbours. `Fallos 308:1932` for
        # `308:1392` is a transposition, not a near miss, and ordering by
        # |page difference| buries the right answer 540 pages down -- which is
        # what happened once the corpus held sixty-eight rulings from tomo 308
        # instead of one.
        for page in _typo_pages(rid.page):
            candidate = RulingId.build(rid.volume, page)
            if candidate is None:
                continue
            row = conn.execute(
                f"SELECT {_RULING_COLS} FROM rulings WHERE id = ?", (str(candidate),)
            ).fetchone()
            if row:
                out[row["id"]] = _row_to_ruling(row)

        # Then the plain near miss: a digit misread rather than swapped.
        for row in conn.execute(
            f"SELECT {_RULING_COLS} FROM rulings WHERE volume = ? ORDER BY abs(page - ?) LIMIT ?",
            (rid.volume, rid.page, limit),
        ):
            out.setdefault(row["id"], _row_to_ruling(row))

    # A name or caption fragment. Matched on a truncated prefix and OR-joined,
    # because the input that reaches here is usually a typo -- "Bazterica" has to
    # find "Bazterrica" or the actionable error is not actionable. Roughly 70% of
    # each token survives, which absorbs a wrong or missing letter near the end
    # without collapsing short names into each other.
    tokens = [t for t in normalize_caption(text).split() if len(t) > 2]
    query = " OR ".join(f"{t[: max(4, int(len(t) * 0.7))]}*" for t in tokens)
    if query:
        try:
            for row in conn.execute(
                f"SELECT {_R_COLS} FROM rulings_fts f "
                "JOIN rulings r ON r.rowid = f.rowid WHERE rulings_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit),
            ):
                out.setdefault(row["id"], _row_to_ruling(row))
        except sqlite3.OperationalError:
            pass  # malformed FTS query: a suggestion is a nicety, never an error

    return list(out.values())[:limit]


def citing_rulings(
    conn: sqlite3.Connection,
    cited_id: str,
    *,
    treatment: Treatment | None = None,
    offset: int = 0,
    limit: int = 20,
) -> Page:
    """Later rulings that cite ``cited_id``, most recent first."""
    where = "c.cited_id = ?"
    params: list[object] = [cited_id]
    if treatment is not None:
        where += " AND c.treatment = ?"
        params.append(treatment.value)

    total = conn.execute(
        f"SELECT count(DISTINCT c.citing_id) FROM citations c WHERE {where}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT DISTINCT {_R_COLS} "
        f"FROM citations c JOIN rulings r ON r.id = c.citing_id WHERE {where} "
        "ORDER BY r.decided_year DESC, r.volume DESC, r.page DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return Page(total=total, offset=offset, items=[_row_to_ruling(r) for r in rows])


def cited_rulings(
    conn: sqlite3.Connection, citing_id: str, *, offset: int = 0, limit: int = 20
) -> Page:
    """Rulings that ``citing_id`` relied on, oldest first."""
    total = conn.execute(
        "SELECT count(DISTINCT cited_id) FROM citations WHERE citing_id = ?", (citing_id,)
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT DISTINCT {_R_COLS} "
        "FROM citations c JOIN rulings r ON r.id = c.cited_id WHERE c.citing_id = ? "
        "ORDER BY r.decided_year ASC, r.volume ASC, r.page ASC LIMIT ? OFFSET ?",
        (citing_id, limit, offset),
    ).fetchall()
    return Page(total=total, offset=offset, items=[_row_to_ruling(r) for r in rows])


def treatments_of(conn: sqlite3.Connection, cited_id: str) -> list[TreatmentRecord]:
    """Every treatment ``cited_id`` received. The input to signal aggregation."""
    rows = conn.execute(
        "SELECT c.citing_id, c.treatment, c.opinion, c.confidence, c.quote, c.method, "
        "r.decided_year FROM citations c JOIN rulings r ON r.id = c.citing_id "
        "WHERE c.cited_id = ?",
        (cited_id,),
    ).fetchall()

    out: list[TreatmentRecord] = []
    for row in rows:
        citing = RulingId.parse(row["citing_id"])
        if citing is None:  # pragma: no cover - schema-constrained
            continue
        out.append(
            TreatmentRecord(
                citing=citing,
                treatment=Treatment(row["treatment"]),
                opinion=Opinion(row["opinion"]),
                confidence=row["confidence"],
                quote=row["quote"],
                method=Method(row["method"]),
                decided_year=row["decided_year"],
            )
        )
    return out


def corpus_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """Provenance of the corpus: when it was built, from what, how far it reaches."""
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM corpus_meta")}
    except sqlite3.OperationalError:  # pragma: no cover - very old db files
        return {}
