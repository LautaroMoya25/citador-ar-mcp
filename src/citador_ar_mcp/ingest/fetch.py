"""Client for the CSJN Secretaria de Jurisprudencia.

Everything here was mapped by hand during Fase 0 against the live site; there is
no published specification. The endpoints, and the one non-obvious thing about
them:

``GET https://sj.csjn.gov.ar/homeSJ/totalTomos/``
    Plain-text integer: how many tomos of the Fallos collection are loaded.
    Returned ``349`` on 2026-09-01 (tomo 349 is year 2026).

``GET /sjconsulta/consultaSumarios/buscarTomoPagina.html?tomo=&pagina=``
    Runs a search. Returns HTML, and the only thing worth reading out of it is
    ``var totalResultados = "N"``. Omitting ``pagina`` searches the whole tomo,
    which is what makes a full crawl possible: 349 requests to enumerate the
    corpus rather than one per page number.

``GET /sjconsulta/consultaSumarios/paginarSumarios.html?startIndex=N``
    The actual JSON, ten sumarios at a time. **This is the non-obvious part:**
    it returns the results of whatever search the *session* last ran, and takes
    no query of its own. A 401 means the session expired and the search has to
    be re-run. So the two calls are inseparable and the cookie jar is not
    optional -- hence :class:`CsjnClient` holding a client rather than exposing
    free functions.

``GET /sjconsulta/documentos/verDocumentoById.html?idDocumento=<idFallo>``
    The ruling as a PDF. See ``ingest/extract.py`` before trusting its text.

Two fields in the JSON carry most of the project's value and are not documented
anywhere: ``linksCitantes`` (an HTML blob listing the later rulings that cite
this sumario, grouped by year) and ``referencias``. Together they mean the
citation *edges* largely do not have to be mined out of PDF prose. What still
has to be derived, and what the project is actually about, is *how* each of
those citations treated the precedent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from citador_ar_mcp.domain.citation import RulingId

log = logging.getLogger(__name__)

HOME: Final = "https://sj.csjn.gov.ar/homeSJ"
BASE: Final = "https://sjconsulta.csjn.gov.ar/sjconsulta"

#: Identifies the crawler to the source, with a link back to the repo, as agreed
#: in CLAUDE.md section 5. Do not remove the URL.
USER_AGENT: Final = (
    "citador-ar-mcp/0.1 (+https://github.com/lautaromoya/citador-ar-mcp) "
    "research citator; contact via repository issues"
)

#: Ceiling on a single PDF download. Bazterrica, the largest ruling seen during
#: Fase 0, is 3 MB across 87 pages; this leaves an order of magnitude of headroom
#: while keeping a redirected or misbehaving endpoint from being read into memory
#: without limit.
MAX_PDF_BYTES: Final = 64 * 1024 * 1024

PAGE_SIZE: Final = 10
_TOTAL_RE: Final = re.compile(r'var totalResultados = "(\d+)"')
_CITANTE_RE: Final = re.compile(r">\s*Fallos:\s*(\d{1,3}):(\d{1,4})\s*<")
_TAGS_RE: Final = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class Sumario:
    """One sumario as the CSJN publishes it, reduced to what the graph needs."""

    sumario_id: int
    ruling_id: RulingId
    caption: str
    decided_year: int
    decided_on: str | None
    """``dd/mm/yyyy``, or ``None`` for old rulings where only the year is published."""
    expediente: str | None
    doc_id: int | None
    text: str
    voces: str
    votes_majority: str
    votes_concurrence: str
    votes_dissent: str
    votes_partial_dissent: str
    citing: tuple[RulingId, ...]
    """Later rulings that cite this sumario, parsed out of ``linksCitantes``."""

    @property
    def source_url(self) -> str:
        return (
            f"{BASE}/consultaSumarios/buscarTomoPagina.html"
            f"?tomo={self.ruling_id.volume}&pagina={self.ruling_id.page}"
        )

    @property
    def pdf_url(self) -> str | None:
        if self.doc_id is None:
            return None
        return f"{BASE}/documentos/verDocumentoById.html?idDocumento={self.doc_id}"


@dataclass
class CsjnClient:
    """Session-bound client. One instance per crawl, reused across tomos.

    Rate limited by :attr:`delay`, which is a courtesy to a public service that
    owes us nothing, not a performance knob.
    """

    delay: float = 0.5
    timeout: float = 120.0
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> CsjnClient:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("CsjnClient must be used as an async context manager")
        return self._client

    async def total_tomos(self) -> int:
        """How many tomos the collection currently has."""
        r = await self.client.get(f"{HOME}/totalTomos/")
        r.raise_for_status()
        return int(r.text.strip())

    async def search(self, volume: int, page: int | None = None) -> int:
        """Run a search in the session. Returns the number of sumarios it matched.

        Must be called before :meth:`page`; see the module docstring.
        """
        params: dict[str, Any] = {"tomo": volume}
        if page is not None:
            params["pagina"] = page
        r = await self.client.get(f"{BASE}/consultaSumarios/buscarTomoPagina.html", params=params)
        r.raise_for_status()
        m = _TOTAL_RE.search(r.text)
        return int(m.group(1)) if m else 0

    async def page(self, start_index: int) -> list[dict[str, Any]]:
        """Fetch one page of the current session's search results."""
        r = await self.client.get(
            f"{BASE}/consultaSumarios/paginarSumarios.html",
            params={"startIndex": start_index},
        )
        if r.status_code == 401:
            raise SessionExpiredError(start_index)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def sumarios(
        self, volume: int, page: int | None = None, *, limit: int | None = None
    ) -> list[Sumario]:
        """Every sumario for a tomo, or for one tomo:pagina. Re-runs the search on 401.

        ``limit`` stops early. It matters more than it looks: a single page of
        results carries every sumario's ``linksCitantes`` blob, so a tomo with a
        hundred sumarios is several megabytes of JSON. Callers that only need the
        ruling's identity should ask for one.
        """
        total = await self.search(volume, page)
        if limit is not None:
            total = min(total, limit)
        out: list[Sumario] = []
        start = 0
        while start < total:
            try:
                batch = await self.page(start)
            except SessionExpiredError:
                await self.search(volume, page)
                batch = await self.page(start)
            if not batch:
                break
            out.extend(parse_sumario(d) for d in batch if _has_position(d))
            start += PAGE_SIZE
            await asyncio.sleep(self.delay)
        return out

    async def pdf(self, doc_id: int) -> bytes | None:
        """Download a ruling PDF. ``None`` when the source returns something else.

        Streamed against a size ceiling rather than read whole. The largest
        ruling seen during Fase 0 was 3 MB (Bazterrica, 87 pages); MAX_PDF_BYTES
        leaves an order of magnitude of headroom while keeping a redirected or
        misbehaving endpoint from being read into memory without limit.
        """
        async with self.client.stream(
            "GET", f"{BASE}/documentos/verDocumentoById.html", params={"idDocumento": doc_id}
        ) as r:
            if r.status_code != 200:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    log.warning(
                        "el documento %s supera %s MB; se descarta",
                        doc_id,
                        MAX_PDF_BYTES // (1024 * 1024),
                    )
                    return None
                chunks.append(chunk)
        body = b"".join(chunks)
        return body if body[:4] == b"%PDF" else None


class SessionExpiredError(RuntimeError):
    """``paginarSumarios`` lost the search. Re-run it and retry."""

    def __init__(self, start_index: int) -> None:
        super().__init__(f"session expired at startIndex={start_index}")
        self.start_index = start_index


def _has_position(raw: dict[str, Any]) -> bool:
    return isinstance(raw.get("tomo"), int) and isinstance(raw.get("pagina"), int)


def strip_html(value: str | None) -> str:
    """Flatten one of the API's HTML-bearing fields into plain text."""
    if not value:
        return ""
    return " ".join(_TAGS_RE.sub(" ", value).split())


def parse_citing(links_citantes: str | None) -> tuple[RulingId, ...]:
    """Parse the later rulings out of ``linksCitantes``.

    The field is a rendered Bootstrap accordion, one panel per year. Entries are
    either a canonical ``Fallos: 347:1031`` or a docket number such as
    ``CSJ 001086/2022/CS001``. Only the canonical ones are returned: a docket
    number cannot be resolved to a node without the alias table, and guessing is
    how a citator ends up asserting edges that do not exist.
    """
    if not links_citantes:
        return ()
    seen: dict[str, RulingId] = {}
    for volume, page in _CITANTE_RE.findall(links_citantes):
        rid = RulingId.build(int(volume), int(page))
        if rid is not None:
            seen.setdefault(str(rid), rid)
    return tuple(seen.values())


def parse_sumario(raw: dict[str, Any]) -> Sumario:
    """Map one raw API record onto :class:`Sumario`."""
    rid = RulingId.build(int(raw["tomo"]), int(raw["pagina"]))
    if rid is None:  # pragma: no cover - guarded by _has_position upstream
        raise ValueError(f"out-of-range position: {raw.get('tomo')}:{raw.get('pagina')}")

    fecha = (raw.get("fechaString") or "").strip()
    decided_on = fecha if re.fullmatch(r"\d{2}/\d{2}/\d{4}", fecha) else None
    year = raw.get("anioFallo")
    if not isinstance(year, int):
        year = int(fecha[-4:]) if re.search(r"\d{4}$", fecha) else 0

    return Sumario(
        sumario_id=int(raw["id"]),
        ruling_id=rid,
        caption=(raw.get("caratulaWeb") or raw.get("autos") or "").strip(),
        decided_year=year,
        decided_on=decided_on,
        expediente=(raw.get("numeroExpediente") or "").strip() or None,
        doc_id=raw.get("idFallo") if isinstance(raw.get("idFallo"), int) else None,
        text=strip_html(raw.get("texto")),
        voces=(raw.get("voces") or "").strip(),
        votes_majority=(raw.get("stringVotosMayoria") or "").strip(),
        votes_concurrence=(raw.get("stringVotosVoto") or "").strip(),
        votes_dissent=(raw.get("stringVotosDisidencia") or "").strip(),
        votes_partial_dissent=(raw.get("stringVotosDisidenciaParcial") or "").strip(),
        citing=parse_citing(raw.get("linksCitantes")),
    )
