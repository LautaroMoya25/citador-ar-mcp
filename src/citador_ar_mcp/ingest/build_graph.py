"""Build the SQLite graph. Runs offline; the MCP server never calls this.

Two sources, same writer:

``--from-fixture``
    Loads the annotated golden chain. Small, deterministic, no network. This is
    what CI builds and what the tools are exercised against.

``--tomos 300-349``
    Crawls the CSJN. Idempotent and resumable by construction: every write is an
    upsert keyed on the canonical identifier, so a run that dies halfway can be
    repeated with no cleanup and no duplicates. It will die halfway.

``--ruling 332:1963``
    The whole pipeline over one ruling: fetch, extract, OCR when the text layer
    is unusable, find citations, attribute each to a vote, and classify the
    treatment. This is the path that produces edges carrying a real passage.

The crawl leaves treatments unclassified -- every edge lands as ``mentioned`` at
low confidence, which is the honest placeholder: we know A cites B, we do not
yet know how. ``--ruling`` classifies, because it has the text to classify from.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from citador_ar_mcp.config import DEFAULT_DB_PATH, SCHEMA_PATH, configure_logging
from citador_ar_mcp.domain.citation import (
    RulingId,
    find_fallos_citations,
    normalize_caption,
    normalize_expediente,
    short_name_key,
)
from citador_ar_mcp.domain.treatment import Method, Opinion, Treatment
from citador_ar_mcp.ingest.fetch import (
    PAGE_SIZE,
    UNKNOWN_YEAR,
    CsjnClient,
    SessionExpiredError,
    Sumario,
    parse_sumario,
    source_url_for,
)
from citador_ar_mcp.ingest.treatment import TreatmentClassifier

log = logging.getLogger(__name__)

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden" / "chain.json"

#: Confidence attached to an edge whose treatment has not been classified yet.
#: Low on purpose: it must never be enough to drive a signal on its own.
UNCLASSIFIED_CONFIDENCE = 0.2


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_ruling(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO rulings (id, volume, page, caption, short_name, decided_on,
                             decided_year, source_url, text_status, text_quality, csjn_doc_id)
        VALUES (:id, :volume, :page, :caption, :short_name, :decided_on,
                :decided_year, :source_url, :text_status, :text_quality, :csjn_doc_id)
        ON CONFLICT(id) DO UPDATE SET
            caption      = excluded.caption,
            short_name   = coalesce(excluded.short_name, rulings.short_name),
            decided_on   = coalesce(excluded.decided_on, rulings.decided_on),
            decided_year = excluded.decided_year,
            source_url   = excluded.source_url,
            text_status  = excluded.text_status,
            text_quality = excluded.text_quality,
            csjn_doc_id  = coalesce(excluded.csjn_doc_id, rulings.csjn_doc_id)
        """,
        {
            "text_quality": None,
            "csjn_doc_id": None,
            "short_name": None,
            "decided_on": None,
            **r,
        },
    )


def upsert_alias(
    conn: sqlite3.Connection, raw: str, ruling_id: str, form: str, source: str
) -> None:
    if not raw.strip():
        return
    conn.execute(
        "INSERT INTO aliases (raw, ruling_id, form, source) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(raw) DO UPDATE SET ruling_id = excluded.ruling_id",
        (raw, ruling_id, form, source),
    )


def upsert_citation(conn: sqlite3.Connection, e: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO citations (citing_id, cited_id, treatment, opinion, confidence,
                               quote, method, sumario_id)
        VALUES (:citing_id, :cited_id, :treatment, :opinion, :confidence,
                :quote, :method, :sumario_id)
        ON CONFLICT(citing_id, cited_id, quote) DO UPDATE SET
            treatment  = excluded.treatment,
            opinion    = excluded.opinion,
            confidence = excluded.confidence,
            method     = excluded.method
        """,
        {"sumario_id": None, **e},
    )


def set_meta(conn: sqlite3.Connection, **values: str) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO corpus_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def add_source(conn: sqlite3.Connection, tag: str) -> None:
    """Record where the graph's contents came from, without erasing earlier steps.

    Each ingest step used to stamp its own ``source``, so whichever ran last
    described the whole file: a corpus crawled from twenty tomos and then topped
    up with the golden fixture ended up labelled ``fixture``, which is the one
    reading that makes a real corpus look like a toy.
    """
    row = conn.execute("SELECT value FROM corpus_meta WHERE key = 'source'").fetchone()
    tags = [t for t in (row[0].split(" + ") if row and row[0] else []) if t]
    if tag not in tags:
        tags.append(tag)
    set_meta(conn, source=" + ".join(tags))


def _es(n: int) -> str:
    """Thousands separator in Spanish: 5325 -> 5.325."""
    return f"{n:,}".replace(",", ".")


def _crawled_range(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = conn.execute("SELECT value FROM corpus_meta WHERE key = 'crawled_tomos'").fetchone()
    if not row or not row[0]:
        return None
    vols = [int(v) for v in row[0].split(",") if v.strip().isdigit()]
    return (min(vols), max(vols)) if vols else None


def stamp_provenance(conn: sqlite3.Connection) -> None:
    """Recompute the graph's composition into ``corpus_meta``.

    Measured, never hand-written. Every number here is a claim the tools and the
    ``citador://corpus`` resource repeat back to the reader, and a stale one is a
    falsehood carrying a citation. Two of these numbers exist to keep the corpus
    honest about its own thinness: only a fraction of the edges carry a stance,
    and only a fraction could be attributed to a vote -- and it is the attributed
    majority ones alone that can light the signal.
    """
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    edges = one("SELECT count(*) FROM citations")
    if not edges:
        return

    stance = one("SELECT count(*) FROM citations WHERE treatment != 'mentioned'")
    attributed = one("SELECT count(*) FROM citations WHERE opinion != 'unknown'")
    majority = one("SELECT count(*) FROM citations WHERE opinion = 'majority'")
    extracted = one("SELECT count(*) FROM rulings WHERE text_status = 'extracted'")
    ocr = one("SELECT count(*) FROM rulings WHERE text_status = 'ocr'")

    values = {
        "built_on": date.today().isoformat(),
        "fuente_oficial": "CSJN, Secretaría de Jurisprudencia — sjconsulta.csjn.gov.ar",
        "licencia": (
            "Corpus derivado. MIT para el software; los fallos son públicos y se "
            "atribuyen a la CSJN."
        ),
        "cobertura_clasificacion": (
            f"{stance} de {edges} aristas ({100 * stance / edges:.1f}%) tienen una postura "
            "distinta de 'mentioned'. El resto es cita sin clasificar, no cita neutral "
            "verificada."
        ),
        "cobertura_atribucion": (
            f"{attributed} de {edges} aristas ({100 * attributed / edges:.1f}%) pudieron "
            f"atribuirse a un voto; {majority} a la mayoría. Solo esas últimas pueden "
            "encender la señal."
        ),
        "texto_completo": (
            f"{extracted} fallos con texto extraído del PDF y {ocr} por OCR. El resto no "
            "tiene texto propio: el grafo se construye desde los sumarios y sus links."
        ),
    }

    span = _crawled_range(conn)
    if span is not None:
        low, high = span
        inside = one(f"SELECT count(*) FROM rulings WHERE volume BETWEEN {low} AND {high}")
        stub = one(f"SELECT count(*) FROM rulings WHERE volume < {low} OR volume > {high}")
        values["note"] = (
            f"{_es(inside)} fallos crawleados de los tomos {low}-{high}, más {_es(stub)} "
            "nodos stub creados para precedentes citados fuera de ese rango (sin texto ni "
            "carátula completa). Las aristas provienen del campo linksCitantes que publica "
            "la propia CSJN por sumario."
        )
    else:
        values["note"] = (
            "Grafo sin crawl registrado: solo los fallos cargados a mano. No es el corpus completo."
        )
    set_meta(conn, **values)


def load_fixture(conn: sqlite3.Connection, path: Path = FIXTURE) -> tuple[int, int]:
    """Load the annotated golden chain into an empty or existing graph."""
    data = json.loads(path.read_text(encoding="utf-8"))

    for r in data["rulings"]:
        upsert_ruling(conn, r)
        if r.get("short_name"):
            upsert_alias(conn, short_name_key(r["short_name"]), r["id"], "short_name", "manual")
        upsert_alias(conn, normalize_caption(r["caption"]), r["id"], "caption", "manual")
        upsert_alias(conn, r["id"], r["id"], "fallos", "manual")

    for raw, form, ruling_id in data["aliases"]:
        upsert_alias(conn, raw, ruling_id, form, "manual")

    for e in data["edges"]:
        upsert_citation(
            conn,
            {
                "citing_id": e["citing_id"],
                "cited_id": e["cited_id"],
                "treatment": e["treatment"],
                "opinion": e["opinion"],
                "confidence": e["confidence"],
                "quote": e["quote"],
                "method": e["method"],
            },
        )

    add_source(conn, "fixture dorado anotado a mano")
    stamp_provenance(conn)
    return len(data["rulings"]), len(data["edges"])


def _ruling_row(s: Sumario, text_status: str = "unavailable") -> dict[str, Any]:
    return {
        "id": str(s.ruling_id),
        "volume": s.ruling_id.volume,
        "page": s.ruling_id.page,
        "caption": s.caption or str(s.ruling_id),
        "short_name": None,
        "decided_on": s.decided_on,
        "decided_year": s.decided_year,
        "source_url": s.source_url,
        "text_status": text_status,
        "text_quality": None,
        "csjn_doc_id": s.doc_id,
    }


def store_sumario(conn: sqlite3.Connection, s: Sumario) -> int:
    """Write one sumario and the edges it declares. Returns the edge count.

    Self-references are dropped. ``linksCitantes`` lists the rulings that cite
    this *sumario*, and a ruling with several sumarios can appear among its own
    citers -- which is true and useless: a self-edge says nothing about whether
    a precedent is still good law, and the schema refuses it.
    """
    upsert_ruling(conn, _ruling_row(s))
    upsert_alias(conn, str(s.ruling_id), str(s.ruling_id), "fallos", "api")
    if s.caption:
        upsert_alias(conn, normalize_caption(s.caption), str(s.ruling_id), "caption", "api")
    if s.expediente and (exp := normalize_expediente(s.expediente)):
        upsert_alias(conn, exp, str(s.ruling_id), "expediente", "api")

    edges = 0
    for citing in s.citing:
        if citing == s.ruling_id:
            continue
        # The citing ruling may not be crawled yet; the foreign key needs a
        # node, so insert a stub a later pass will fill in.
        conn.execute(
            "INSERT OR IGNORE INTO rulings (id, volume, page, caption, "
            "decided_year, source_url, text_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(citing),
                citing.volume,
                citing.page,
                citing.human,
                UNKNOWN_YEAR,
                source_url_for(citing),
                "unavailable",
            ),
        )
        upsert_citation(
            conn,
            {
                "citing_id": str(citing),
                "cited_id": str(s.ruling_id),
                "treatment": Treatment.MENTIONED.value,
                "opinion": Opinion.UNKNOWN.value,
                "confidence": UNCLASSIFIED_CONFIDENCE,
                "quote": (
                    f"Referencia publicada por la CSJN en el sumario {s.sumario_id} "
                    f"de {s.ruling_id.human}. Sin clasificar: falta el pasaje."
                ),
                "method": Method.RULE.value,
                "sumario_id": s.sumario_id,
            },
        )
        edges += 1
    return edges


def done_volumes(conn: sqlite3.Connection) -> set[int]:
    """Tomos already crawled, from the graph's own metadata."""
    row = conn.execute("SELECT value FROM corpus_meta WHERE key = 'crawled_tomos'").fetchone()
    if not row or not row[0]:
        return set()
    return {int(v) for v in row[0].split(",") if v.strip().isdigit()}


def mark_done(conn: sqlite3.Connection, volume: int) -> None:
    done = done_volumes(conn) | {volume}
    set_meta(conn, crawled_tomos=",".join(str(v) for v in sorted(done)))


async def crawl(
    conn: sqlite3.Connection,
    volumes: range,
    *,
    delay: float = 0.5,
    resume: bool = True,
) -> tuple[int, int]:
    """Crawl a range of tomos into the graph.

    Edges come from ``linksCitantes``, which the CSJN publishes per sumario. That
    gives the shape of the graph without reading a single PDF. Treatments are
    left unclassified.

    The placeholder quote records where the assertion came from rather than
    pretending to be a passage, so nothing downstream can mistake it for one.

    Three things make "it will die halfway" survivable, and the first full run
    proved all three were needed. It died on tomo 330 -- 350 requests and about a
    gigabyte in -- on a single self-citing sumario, and left an empty database:

    * a row the schema refuses is logged and skipped, not fatal to the crawl;
    * the transaction commits per page rather than per tomo, so a crash costs
      ten sumarios rather than the three thousand five hundred of tomo 330;
    * completed tomos are recorded in ``corpus_meta`` and skipped on re-run, so
      restarting does not re-download six gigabytes.
    """
    rulings = edges = 0
    already = done_volumes(conn) if resume else set()
    if already:
        log.info("ya relevados, se saltean: %s", ",".join(str(v) for v in sorted(already)))

    async with CsjnClient(delay=delay) as csjn:
        for volume in volumes:
            if volume in already:
                continue
            try:
                total = await csjn.search(volume)
            except Exception:
                log.exception("tomo %s: no pude abrir la búsqueda, se saltea", volume)
                continue

            got = 0
            lost = 0
            for start in range(0, total, PAGE_SIZE):
                try:
                    batch = await csjn.page(start)
                except SessionExpiredError:
                    await csjn.search(volume)
                    batch = await csjn.page(start)
                except Exception:
                    # Already retried inside page(). Count it: a tomo missing a
                    # page is not a finished tomo, and marking it done would hide
                    # ten sumarios from every later run.
                    lost += 1
                    log.exception("tomo %s: falló la página %s, se saltea", volume, start)
                    continue
                if not batch:
                    break

                for raw in batch:
                    try:
                        s = parse_sumario(raw)
                    except Exception:
                        log.warning("tomo %s: sumario ilegible %s", volume, raw.get("id"))
                        continue
                    try:
                        edges += store_sumario(conn, s)
                    except sqlite3.IntegrityError as exc:
                        # One malformed row must not cost the run. Skip it and say
                        # which one, so it can be looked at later.
                        log.warning("sumario %s rechazado por el esquema: %s", s.sumario_id, exc)
                        continue
                    rulings += 1
                    got += 1

                conn.commit()
                await asyncio.sleep(delay)

            if lost:
                log.warning(
                    "tomo %s: %s página(s) perdida(s); NO se marca como relevado, "
                    "una nueva corrida lo reintenta",
                    volume,
                    lost,
                )
            else:
                mark_done(conn, volume)
            conn.commit()
            log.info(
                "tomo %s: %s/%s sumarios%s, %s aristas acumuladas",
                volume,
                got,
                total,
                f" ({lost} página(s) perdida(s))" if lost else "",
                edges,
            )

    set_meta(conn, tomos=f"{volumes.start}-{volumes.stop - 1}")
    add_source(conn, "csjn-crawl")
    stamp_provenance(conn)
    return rulings, edges


async def ingest_ruling(
    conn: sqlite3.Connection,
    volume: int,
    page: int,
    *,
    cache: Path,
    delay: float = 0.5,
) -> tuple[int, int]:
    """Run the whole pipeline over one ruling: fetch, extract, cite, classify, store.

    This is Fase 4 and Fase 5 end to end for a single node, and it is how the
    graph gets edges that carry a real passage instead of the crawl's
    placeholder. Returns ``(citations found, citations classified)``.
    """
    from citador_ar_mcp.ingest import llm

    # Opt-in: the stage costs money per passage, and a corpus crawl is a lot of
    # passages. Off unless CITADOR_LLM says otherwise.
    classifier = llm.build() if llm.enabled_by_env() else None

    cache.mkdir(parents=True, exist_ok=True)
    async with CsjnClient(delay=delay) as csjn:
        return await _ingest_one(conn, csjn, volume, page, cache=cache, classifier=classifier)


async def _ingest_one(
    conn: sqlite3.Connection,
    csjn: CsjnClient,
    volume: int,
    page: int,
    *,
    cache: Path,
    classifier: TreatmentClassifier | None = None,
) -> tuple[int, int]:
    """One ruling, on an already-open client. See :func:`ingest_ruling`."""
    from citador_ar_mcp.ingest.citations import find_citations, join_page_breaks
    from citador_ar_mcp.ingest.extract import TextStatus, extract_pdf
    from citador_ar_mcp.ingest.treatment import classify_passage

    sumarios = await csjn.sumarios(volume, page, limit=1)
    if not sumarios:
        log.error("Fallos %s:%s no está en la fuente", volume, page)
        return 0, 0
    s = sumarios[0]
    if s.doc_id is None:
        log.error("%s no tiene documento publicado", s.ruling_id.human)
        return 0, 0

    pdf_path = cache / f"{volume}-{page}.pdf"
    if not pdf_path.exists():
        body = await csjn.pdf(s.doc_id)
        if body is None:
            log.error("no pude bajar el PDF de %s", s.ruling_id.human)
            return 0, 0
        pdf_path.write_bytes(body)

    extracted = extract_pdf(pdf_path, ocr_fallback=True, ocr_cache=cache / "ocr")
    upsert_ruling(
        conn,
        {
            **_ruling_row(s, extracted.status.value),
            "text_quality": extracted.quality,
        },
    )
    upsert_alias(conn, str(s.ruling_id), str(s.ruling_id), "fallos", "api")
    if s.caption:
        upsert_alias(conn, normalize_caption(s.caption), str(s.ruling_id), "caption", "api")
    if s.expediente and (exp := normalize_expediente(s.expediente)):
        upsert_alias(conn, exp, str(s.ruling_id), "expediente", "api")

    if extracted.status not in (TextStatus.EXTRACTED, TextStatus.OCR):
        log.warning(
            "%s: texto '%s', no se pueden extraer pasajes",
            s.ruling_id.human,
            extracted.status.value,
        )
        conn.commit()
        return 0, 0

    text = join_page_breaks(extracted.text)
    found = classified = 0
    for cite in find_citations(text, exclude=s.ruling_id):
        found += 1
        # The citation's offset inside its own quote, which is what tells the
        # classifier which clause to read.
        local = cite.quote.find(cite.raw)
        result = classify_passage(
            cite.quote,
            citation_position=local if local >= 0 else None,
            cited=str(cite.cited),
            llm=classifier,
        )
        if not result.is_fallback:
            classified += 1

        # The cited ruling may not be in the graph yet; the foreign key needs a
        # node, so insert a stub for a later pass to fill in.
        conn.execute(
            "INSERT OR IGNORE INTO rulings (id, volume, page, caption, decided_year, "
            "source_url, text_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(cite.cited),
                cite.cited.volume,
                cite.cited.page,
                cite.cited.human,
                UNKNOWN_YEAR,
                source_url_for(cite.cited),
                "unavailable",
            ),
        )
        upsert_citation(
            conn,
            {
                "citing_id": str(s.ruling_id),
                "cited_id": str(cite.cited),
                "treatment": result.treatment.value,
                "opinion": cite.opinion.value,
                "confidence": result.confidence,
                "quote": cite.quote,
                "method": result.method.value,
                "sumario_id": None,
            },
        )

    add_source(conn, "csjn-pipeline")
    stamp_provenance(conn)
    conn.commit()
    log.info(
        "%s: %s citas, %s clasificadas por regla, %s en 'mentioned' de fallback",
        s.ruling_id.human,
        found,
        classified,
        found - classified,
    )
    return found, classified


def reclassify_stored(conn: sqlite3.Connection) -> tuple[int, int]:
    """Re-run the rules over passages already in the graph. Returns ``(seen, changed)``.

    The rule set grows as the corpus is read, and every improvement would
    otherwise mean re-downloading thousands of PDFs to see it. The passages are
    already stored; only the reading of them was out of date.

    Touches only rows still at fallback confidence. A row a rule or a person has
    already spoken for is left alone -- re-running must never quietly overwrite
    a judgement with a weaker one.
    """
    from citador_ar_mcp.ingest.treatment import classify_passage

    rows = conn.execute(
        """SELECT citing_id, cited_id, quote FROM citations
           WHERE confidence <= ? AND quote NOT LIKE 'Referencia publicada por la CSJN%'""",
        (UNCLASSIFIED_CONFIDENCE + 0.05,),
    ).fetchall()

    changed = 0
    for citing, cited, quote in rows:
        position = next(
            (c.start for c in find_fallos_citations(quote) if str(c.ruling_id) == cited),
            None,
        )
        result = classify_passage(quote, citation_position=position, cited=cited)
        if result.is_fallback:
            continue
        conn.execute(
            "UPDATE citations SET treatment = ?, confidence = ?, method = ? "
            "WHERE citing_id = ? AND cited_id = ? AND quote = ?",
            (result.treatment.value, result.confidence, result.method.value, citing, cited, quote),
        )
        changed += 1
    conn.commit()
    return len(rows), changed


def citers_of_top(conn: sqlite3.Connection, n: int) -> list[str]:
    """Every ruling that cites one of the ``n`` most-cited rulings.

    This is the set that has to be read to answer "is this still good law" about
    the leading cases. Running the pipeline over the leading cases themselves
    classifies how *they* treated *their* precedents, which is a different
    question and leaves their own signal grey.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT citing_id FROM citations WHERE cited_id IN (
            SELECT cited_id FROM citations
            GROUP BY cited_id ORDER BY count(DISTINCT citing_id) DESC LIMIT ?
        )
        ORDER BY citing_id
        """,
        (n,),
    ).fetchall()
    return [r[0] for r in rows]


def already_classified(conn: sqlite3.Connection) -> set[str]:
    """Rulings whose text has been read already.

    ``text_status`` is the marker: the crawl leaves every node ``unavailable``
    because it never opens a PDF, and only the classification pass sets anything
    else. So this doubles as resume state without a second bookkeeping table.
    """
    return {r[0] for r in conn.execute("SELECT id FROM rulings WHERE text_status <> 'unavailable'")}


async def classify_rulings(
    conn: sqlite3.Connection,
    ruling_ids: list[str],
    *,
    cache: Path,
    delay: float = 0.5,
    resume: bool = True,
) -> tuple[int, int, int]:
    """Read and classify a batch of rulings. Returns ``(done, edges, failed)``.

    One HTTP session for the whole batch rather than one per ruling, and each
    ruling is independent: a PDF that will not download or parse costs that
    ruling and nothing else. Already-read rulings are skipped, so a batch that
    dies at 300 of 387 resumes at 300.
    """
    from citador_ar_mcp.ingest import llm

    classifier = llm.build() if llm.enabled_by_env() else None
    if classifier is not None:
        log.info("etapa LLM activa para lo que las reglas no resuelvan")

    seen = already_classified(conn) if resume else set()
    pending = [r for r in ruling_ids if r not in seen]
    if seen:
        log.info("ya leídos, se saltean: %s de %s", len(ruling_ids) - len(pending), len(ruling_ids))

    cache.mkdir(parents=True, exist_ok=True)
    done = edges = failed = 0
    async with CsjnClient(delay=delay) as csjn:
        for i, rid in enumerate(pending, 1):
            rp = RulingId.parse(rid)
            if rp is None:
                log.warning("identificador ilegible, se saltea: %s", rid)
                failed += 1
                continue
            try:
                _found, classified = await _ingest_one(
                    conn, csjn, rp.volume, rp.page, cache=cache, classifier=classifier
                )
            except Exception:
                # One ruling must not cost the batch. The graph keeps whatever
                # the earlier ones wrote.
                log.exception("%s falló, se saltea", rp.human)
                failed += 1
                conn.rollback()
                continue
            done += 1
            edges += classified
            if i % 10 == 0:
                log.info(
                    "progreso: %s/%s leídos, %s aristas clasificadas, %s fallidos",
                    i,
                    len(pending),
                    edges,
                    failed,
                )
    add_source(conn, "csjn-pipeline")
    stamp_provenance(conn)
    conn.commit()
    return done, edges, failed


def _parse_ruling(spec: str) -> tuple[int, int]:
    volume, _, page = spec.partition(":")
    return int(volume), int(page)


def _parse_tomos(spec: str) -> range:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return range(int(lo), int(hi) + 1)
    n = int(spec)
    return range(n, n + 1)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="Construye el grafo de citas en SQLite.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--from-fixture", action="store_true", help="cargar solo la cadena dorada")
    ap.add_argument("--tomos", help="rango de tomos a crawlear, p. ej. 330-349")
    ap.add_argument(
        "--ruling",
        help=(
            "pipeline completo sobre un fallo: fetch, OCR si hace falta, "
            "citas y clasificación. Ej: 332:1963"
        ),
    )
    ap.add_argument(
        "--classify-citers-of-top",
        type=int,
        metavar="N",
        help=(
            "leer y clasificar todos los fallos que citan a los N más citados. "
            "Es lo que hace falta para que esos N dejen de estar en gris."
        ),
    )
    ap.add_argument(
        "--reclassify",
        action="store_true",
        help=(
            "re-aplicar las reglas a los pasajes ya guardados, sin red. "
            "Para cuando el set de reglas mejora."
        ),
    )
    ap.add_argument("--cache", type=Path, default=DEFAULT_DB_PATH.parent / "cache")
    ap.add_argument("--delay", type=float, default=0.5, help="segundos entre pedidos")
    args = ap.parse_args(argv)

    if not (
        args.from_fixture
        or args.tomos
        or args.ruling
        or args.classify_citers_of_top
        or args.reclassify
    ):
        ap.error("elegí --from-fixture, --tomos, --ruling, --classify-citers-of-top o --reclassify")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        if args.reclassify:
            seen, edges = reclassify_stored(conn)
            rulings = 0
            log.info("re-leídos %s pasajes, %s pasaron a tener tratamiento", seen, edges)
        elif args.from_fixture:
            rulings, edges = load_fixture(conn)
        elif args.classify_citers_of_top:
            targets = citers_of_top(conn, args.classify_citers_of_top)
            log.info(
                "%s fallos citan a los %s más citados",
                len(targets),
                args.classify_citers_of_top,
            )
            rulings, edges, failed = asyncio.run(
                classify_rulings(conn, targets, cache=args.cache, delay=args.delay)
            )
            if failed:
                log.warning("%s fallos no se pudieron leer", failed)
        elif args.ruling:
            volume, page = _parse_ruling(args.ruling)
            _found, edges = asyncio.run(
                ingest_ruling(conn, volume, page, cache=args.cache, delay=args.delay)
            )
            rulings = 1
        else:
            rulings, edges = asyncio.run(crawl(conn, _parse_tomos(args.tomos), delay=args.delay))
        conn.commit()
    finally:
        conn.close()

    # Counted from the graph, not from the loop. The crawl iterates sumarios and
    # a ruling has many, so a loop counter would report the corpus several times
    # larger than it is.
    conn = sqlite3.connect(args.db)
    try:
        nodes = conn.execute("SELECT count(*) FROM rulings").fetchone()[0]
        arcs = conn.execute("SELECT count(*) FROM citations").fetchone()[0]
        stubs = conn.execute(
            "SELECT count(*) FROM rulings WHERE decided_year = ?", (UNKNOWN_YEAR,)
        ).fetchone()[0]
    finally:
        conn.close()
    log.info(
        "grafo en %s: %s fallos (%s todavía sin relevar), %s aristas; "
        "esta corrida procesó %s sumarios y escribió %s aristas",
        args.db,
        nodes,
        stubs,
        arcs,
        rulings,
        edges,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
