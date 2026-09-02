"""Build the golden fixture: the Colavini -> Arriola chain, annotated.

The split of labour here is deliberate. **Metadata and passages are extracted
from the source; treatment labels are assigned by hand.** Which paragraph of
Arriola carries the departure from Montalvo is a judgement about legal meaning,
and the point of the fixture is to hold the pipeline to that judgement -- so it
cannot be produced by the same code the fixture is meant to test.

Run it when the corpus changes::

    uv run --extra ingest python scripts/build_golden_fixture.py

It writes ``tests/fixtures/golden/chain.json`` and caches the PDFs under
``data/cache/`` so re-runs do not hit the CSJN again.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from citador_ar_mcp.domain.citation import (
    normalize_caption,
    normalize_expediente,
    short_name_key,
)
from citador_ar_mcp.domain.treatment import Method, Treatment
from citador_ar_mcp.ingest.citations import find_citations, join_page_breaks
from citador_ar_mcp.ingest.extract import TextStatus, extract_pdf
from citador_ar_mcp.ingest.fetch import CsjnClient, Sumario

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "tests" / "fixtures" / "golden" / "chain.json"

# The chain, with the short name each ruling is known by.
CHAIN: list[tuple[str, int, int]] = [
    ("Colavini", 300, 254),
    ("Bazterrica", 308, 1392),
    ("Montalvo", 313, 1333),
    ("Arriola", 332, 1963),
]

# The hand annotation. `marker` selects which of the several passages citing a
# precedent is the one that carries the treatment; it is the human judgement the
# pipeline is being held to.
EXPECTED: list[dict[str, Any]] = [
    {
        "citing": "Arriola",
        "cited": "Bazterrica",
        "treatment": Treatment.FOLLOWED,
        "marker": "han sido resueltas acertadamente en",
        "note": (
            "Considerando 10 del voto de la mayoría: adopta expresamente la solución de "
            "Bazterrica. Se elige este pasaje y no el equivalente del voto de Lorenzetti "
            "('corresponde aplicar el criterio...') porque un voto concurrente no fija la "
            "doctrina del tribunal."
        ),
    },
    {
        "citing": "Arriola",
        "cited": "Montalvo",
        "treatment": Treatment.ABANDONED,
        "marker": "vuelve nuevamente sobre sus pasos",
        "note": (
            "El considerando 12 narra el zigzag y anuncia el regreso a Bazterrica, "
            "lo que implica apartarse de Montalvo."
        ),
    },
    {
        "citing": "Arriola",
        "cited": "Colavini",
        "treatment": Treatment.MENTIONED,
        "marker": "se pronunció a favor de la criminalización",
        "note": (
            "Arriola cita a Colavini para narrar la historia del problema, no para "
            "tomar postura sobre él: Bazterrica ya se había apartado en 1986. "
            "Ante duda, mentioned."
        ),
    },
    {
        "citing": "Bazterrica",
        "cited": "Colavini",
        "treatment": Treatment.MENTIONED,
        "marker": "ha valorado la magnitud del problema de la drogadicción",
        "note": (
            "OJO: doctrinariamente Bazterrica abandona a Colavini, y el propio Arriola lo "
            "dice ('en Bazterrica y Capalbo, se apartó de tal doctrina'). Pero el voto de "
            "la mayoría de Bazterrica cita a Colavini una sola vez, en el considerando 6, "
            "y lo cita a favor: acepta su valoración de la magnitud del problema. El "
            "apartamiento llega en el considerando 8 ('sin embargo, no se debe presumir') "
            "y no menciona el precedente. Es un abandono implícito. El pasaje sostiene "
            "'mentioned' y nada más fuerte, así que eso se anota. Ver la nota "
            "'implicit_overruling' del fixture."
        ),
    },
    {
        "citing": "Montalvo",
        "cited": "Bazterrica",
        "treatment": Treatment.ABANDONED,
        "marker": "No es compatible",
        "note": (
            "Considerando del voto de la mayoría: 'No es compatible, pues, el criterio "
            "expuesto en el primer voto de Fallos 308:1392 (consid. 8)'. Rechazo expreso "
            "de la doctrina de Bazterrica, con la cita al lado."
        ),
    },
]

#: Findings that the fixture records but no single edge can express. Kept in the
#: file so the limitation travels with the data.
NOTES: dict[str, str] = {
    "implicit_overruling": (
        "Bazterrica se aparta de Colavini sin citarlo en el pasaje donde se aparta. Un "
        "citador construido sobre el grafo de citas no puede, estructuralmente, detectar "
        "un abandono implícito: no hay cita que clasificar. Es una limitación del método, "
        "no un bug, y el README la declara."
    ),
    "dictamen_del_procurador": (
        "Los tomos impresos traen el dictamen del Procurador General antes del fallo, en "
        "el mismo documento. En Bazterrica el Procurador pedía mantener Colavini y la "
        "Corte resolvió al revés, así que atribuir sus citas al tribunal invertiría el "
        "resultado. Se segmenta como opinion='dictamen' y no es vinculante."
    ),
}


async def collect() -> dict[str, Any]:
    CACHE.mkdir(parents=True, exist_ok=True)
    by_name: dict[str, Sumario] = {}
    texts: dict[str, tuple[str, TextStatus, float]] = {}

    async with CsjnClient() as csjn:
        total_tomos = await csjn.total_tomos()
        for name, volume, page in CHAIN:
            sums = await csjn.sumarios(volume, page, limit=1)
            if not sums:
                raise SystemExit(f"{name}: la fuente no devolvió sumarios para {volume}:{page}")
            by_name[name] = sums[0]

            doc_id = sums[0].doc_id
            if doc_id is None:
                texts[name] = ("", TextStatus.UNAVAILABLE, 0.0)
                continue

            pdf_path = CACHE / f"{name.lower()}.pdf"
            if not pdf_path.exists():
                body = await csjn.pdf(doc_id)
                if body is None:
                    texts[name] = ("", TextStatus.UNAVAILABLE, 0.0)
                    continue
                pdf_path.write_bytes(body)
            ex = extract_pdf(pdf_path, ocr_fallback=True, ocr_cache=CACHE / "ocr")
            texts[name] = (join_page_breaks(ex.text), ex.status, ex.quality)
            print(f"  {name:11} {ex.pages:>3} pags  calidad={ex.quality:.3f}  {ex.status.value}")

    rulings = []
    aliases = []
    for name, _volume, _page in CHAIN:
        s = by_name[name]
        text, status, quality = texts[name]
        rulings.append(
            {
                "id": str(s.ruling_id),
                "volume": s.ruling_id.volume,
                "page": s.ruling_id.page,
                "caption": s.caption,
                "short_name": name,
                "decided_on": s.decided_on,
                "decided_year": s.decided_year,
                "source_url": s.source_url,
                "csjn_doc_id": s.doc_id,
                "text_status": status.value,
                "text_quality": round(quality, 4),
            }
        )
        aliases.append({"raw": str(s.ruling_id), "form": "fallos", "ruling_id": str(s.ruling_id)})
        aliases.append(
            {"raw": short_name_key(name), "form": "short_name", "ruling_id": str(s.ruling_id)}
        )
        if s.caption:
            aliases.append(
                {
                    "raw": normalize_caption(s.caption),
                    "form": "caption",
                    "ruling_id": str(s.ruling_id),
                }
            )
        if s.expediente and (exp := normalize_expediente(s.expediente)):
            aliases.append({"raw": exp, "form": "expediente", "ruling_id": str(s.ruling_id)})

    edges: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for spec in EXPECTED:
        citing_name, cited_name = spec["citing"], spec["cited"]
        citing, cited = by_name[citing_name], by_name[cited_name]
        text, status, _q = texts[citing_name]

        if status not in (TextStatus.EXTRACTED, TextStatus.OCR):
            pending.append(
                {
                    "citing_id": str(citing.ruling_id),
                    "cited_id": str(cited.ruling_id),
                    "citing": citing_name,
                    "cited": cited_name,
                    "expected_treatment": spec["treatment"].value,
                    "note": spec["note"],
                    "blocker": (
                        f"El PDF de {citing_name} tiene text_status='{status.value}': "
                        "capa de texto con fuente sin mapa Unicode. No se puede citar un "
                        "pasaje verificable sin OCR, y sin pasaje no hay arista."
                    ),
                }
            )
            continue

        found = [
            f for f in find_citations(text, exclude=citing.ruling_id) if f.cited == cited.ruling_id
        ]
        marker = spec["marker"]
        picked = next((f for f in found if marker and marker.lower() in f.quote.lower()), None)
        if picked is None:
            pending.append(
                {
                    "citing_id": str(citing.ruling_id),
                    "cited_id": str(cited.ruling_id),
                    "citing": citing_name,
                    "cited": cited_name,
                    "expected_treatment": spec["treatment"].value,
                    "note": spec["note"],
                    "blocker": (
                        f"No se encontró el pasaje marcador en el texto de {citing_name}. "
                        f"Ocurrencias halladas: {len(found)}."
                    ),
                }
            )
            continue

        edges.append(
            {
                "citing_id": str(citing.ruling_id),
                "cited_id": str(cited.ruling_id),
                "citing": citing_name,
                "cited": cited_name,
                "treatment": spec["treatment"].value,
                "opinion": picked.opinion.value,
                "opinion_author": picked.author,
                "confidence": 1.0,
                "method": Method.MANUAL.value,
                "quote": picked.quote,
                "offset": picked.offset,
                "note": spec["note"],
            }
        )

    return {
        "_comment": (
            "Fixture dorado del proyecto. Metadatos y pasajes extraídos de la fuente; "
            "etiquetas de tratamiento asignadas a mano. Regenerar con "
            "scripts/build_golden_fixture.py."
        ),
        "generated_on": date.today().isoformat(),
        "source": "CSJN, Secretaría de Jurisprudencia (sjconsulta.csjn.gov.ar)",
        "corpus_tomos": total_tomos,
        "notes": NOTES,
        "rulings": rulings,
        "aliases": sorted({(a["raw"], a["form"], a["ruling_id"]) for a in aliases}),
        "edges": edges,
        "pending": pending,
    }


def main() -> None:
    print("Construyendo el fixture dorado (Colavini -> Arriola)...")
    data = asyncio.run(collect())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  aristas resueltas : {len(data['edges'])}")
    print(f"  aristas pendientes: {len(data['pending'])}")
    for p in data["pending"]:
        print(f"    - {p['citing']} -> {p['cited']}: {p['blocker']}")
    print(f"\nEscrito: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
