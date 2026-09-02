"""The five tools.

``citador_check_status`` is the one that justifies the project; the other four
exist so that its answers can be checked. That ordering is why every one of them
renders quotes and confidences rather than bare conclusions.
"""

from __future__ import annotations

from typing import Any

from citador_ar_mcp.config import DEFAULT_LIMIT
from citador_ar_mcp.domain.signals import Signal, SignalReport, aggregate
from citador_ar_mcp.domain.treatment import Treatment
from citador_ar_mcp.store import queries
from citador_ar_mcp.tools._common import (
    DISCLAIMER,
    MAX_TRACE_STEPS,
    QUOTE_NOTICE,
    CitadorError,
    ResponseFormat,
    as_json,
    clamp_limit,
    clamp_offset,
    page_dict,
    parse_treatment,
    render_page,
    render_quote,
    report_dict,
    resolve_or_raise,
    ruling_dict,
    trim_quote,
    when,
)


def lookup_ruling(query: str, response_format: ResponseFormat = "markdown") -> str:
    """Resolve any written form of a citation to a single ruling."""
    with queries.connect() as conn:
        r = resolve_or_raise(conn, query)
        aliases = [
            row["raw"]
            for row in conn.execute(
                "SELECT raw FROM aliases WHERE ruling_id = ? ORDER BY form, raw", (r.id,)
            )
        ]

    if response_format == "json":
        return as_json({"query": query, **ruling_dict(r), "aliases": aliases})

    lines = [
        f"## {r.ruling_id.human}" + (f' — "{r.short_name}"' if r.short_name else ""),
        "",
        f"**Carátula:** {r.caption}",
        f"**Fecha:** {when(r)}",
        f"**Identificador:** `{r.id}`",
        f"**Texto del fallo:** {_text_status_es(r.text_status)}",
        f"**Fuente:** {r.source_url}",
    ]
    if aliases:
        lines += ["", "**Se cita también como:** " + ", ".join(f"`{a}`" for a in aliases)]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def check_status(ruling: str, response_format: ResponseFormat = "markdown") -> str:
    """Is this ruling still good law? The aggregate signal, with its evidence."""
    with queries.connect() as conn:
        r = resolve_or_raise(conn, ruling)
        records = queries.treatments_of(conn, r.id)
        meta = queries.corpus_meta(conn)

    report = aggregate(r.ruling_id, records)

    if response_format == "json":
        return as_json(
            {
                "ruling": ruling_dict(r),
                "report": report_dict(report),
                "corpus": meta,
            }
        )

    name = f' — "{r.short_name}"' if r.short_name else ""
    lines = [
        f"## {report.signal.glyph} {r.ruling_id.human}{name}",
        "",
        f"**Señal:** {report.signal.label_es} (confianza {report.confidence:.2f})",
        f"**Fallos posteriores que lo citan:** {report.total_citing}",
    ]

    if report.binding_counts:
        detail = ", ".join(
            f"{t.label_es} {n}"
            for t, n in sorted(report.binding_counts.items(), key=lambda kv: -kv[0].severity)
        )
        lines.append(f"**Tratamiento por la mayoría:** {detail}")
    non_binding = {t: n - report.binding_counts.get(t, 0) for t, n in report.counts.items()}
    non_binding = {t: n for t, n in non_binding.items() if n}
    if non_binding:
        detail = ", ".join(f"{t.label_es} {n}" for t, n in non_binding.items())
        lines.append(f"**En votos propios o disidencias:** {detail}")

    if report.caveats:
        lines += ["", "### Advertencias"]
        lines += [f"- {c}" for c in report.caveats]

    if report.evidence:
        lines += ["", "### Evidencia", "", QUOTE_NOTICE]
        for e in report.evidence:
            binding = (
                "mayoría" if e.counts_toward_signal else f"NO vinculante ({e.opinion.label_es})"
            )
            year = f" ({e.decided_year})" if e.decided_year else ""
            lines += [
                "",
                f"**{e.citing.human}{year} — {e.treatment.label_es}** "
                f"· confianza {e.confidence:.2f} · {binding} · método `{e.method.value}`",
                "",
                render_quote(e.quote),
            ]

    if meta:
        built = meta.get("built_on", "?")
        lines += ["", f"_Corpus: {meta.get('source', '?')}, generado {built}._"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def citing_rulings(
    ruling: str,
    treatment: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    response_format: ResponseFormat = "markdown",
) -> str:
    """Which later rulings cited this one, optionally filtered by treatment."""
    filt = parse_treatment(treatment)
    limit = clamp_limit(limit)

    with queries.connect() as conn:
        r = resolve_or_raise(conn, ruling)
        page = queries.citing_rulings(
            conn, r.id, treatment=filt, offset=clamp_offset(offset), limit=limit
        )

    if response_format == "json":
        return as_json(
            {"ruling": ruling_dict(r), "treatment": filt.value if filt else None, **page_dict(page)}
        )

    suffix = f" con tratamiento `{filt.value}`" if filt else ""
    return (
        render_page(
            page,
            heading=f"## Fallos que citan a {r.ruling_id.human}{suffix}",
            empty=f"Ningún fallo del corpus cita a {r.ruling_id.human}{suffix}.",
        )
        + f"\n\n{DISCLAIMER}"
    )


def cited_rulings(
    ruling: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    response_format: ResponseFormat = "markdown",
) -> str:
    """Which rulings this one relied on."""
    limit = clamp_limit(limit)
    with queries.connect() as conn:
        r = resolve_or_raise(conn, ruling)
        page = queries.cited_rulings(conn, r.id, offset=clamp_offset(offset), limit=limit)

    if response_format == "json":
        return as_json({"ruling": ruling_dict(r), **page_dict(page)})

    return (
        render_page(
            page,
            heading=f"## Precedentes en los que se apoya {r.ruling_id.human}",
            empty=f"No hay precedentes registrados para {r.ruling_id.human}.",
        )
        + f"\n\n{DISCLAIMER}"
    )


def trace_doctrine(
    ruling: str,
    max_depth: int = 3,
    response_format: ResponseFormat = "markdown",
) -> str:
    """Walk the chain of a doctrine forward in time from a starting ruling.

    Follows only the edges that move the doctrine -- ``abandoned``, ``followed``,
    ``applied``, ``limited``, ``criticized`` -- because a chain built out of bare
    mentions is noise, not a line of authority.
    """
    max_depth = max(1, min(max_depth, 6))
    moving = {
        Treatment.ABANDONED,
        Treatment.FOLLOWED,
        Treatment.APPLIED,
        Treatment.LIMITED,
        Treatment.CRITICIZED,
        Treatment.DISTINGUISHED,
    }

    with queries.connect() as conn:
        root = resolve_or_raise(conn, ruling)
        chain: list[dict[str, Any]] = []
        seen = {root.id}
        frontier = [root]
        depth = 0

        while frontier and depth < max_depth:
            nxt = []
            for node in frontier:
                for rec in queries.treatments_of(conn, node.id):
                    if rec.treatment not in moving:
                        continue
                    citing_id = str(rec.citing)
                    citing = queries.get_ruling(conn, citing_id)
                    if citing is None:
                        continue
                    chain.append(
                        {
                            "depth": depth,
                            "from": node.id,
                            "from_cite": node.ruling_id.human,
                            "to": citing_id,
                            "to_cite": rec.citing.human,
                            "to_name": citing.short_name,
                            "year": rec.decided_year,
                            "treatment": rec.treatment.value,
                            "treatment_label": rec.treatment.label_es,
                            "opinion": rec.opinion.value,
                            "binding": rec.counts_toward_signal,
                            "confidence": rec.confidence,
                            "quote": rec.quote,
                        }
                    )
                    if citing_id not in seen:
                        seen.add(citing_id)
                        nxt.append(citing)
            frontier = nxt
            depth += 1

        signals: dict[str, SignalReport] = {}
        for node_id in seen:
            link = queries.get_ruling(conn, node_id)
            if link is None:
                continue
            signals[node_id] = aggregate(link.ruling_id, queries.treatments_of(conn, node_id))

    chain.sort(key=lambda s: (s["year"] or 0, s["depth"]))

    # Truncate before rendering. A leading case in the full corpus can be cited
    # by hundreds of later rulings, and the walk multiplies that by depth: on a
    # graph with twenty citing rulings each, an uncapped depth-6 trace returns
    # 2.020 steps and roughly 445.000 tokens. Oldest first, because the earliest
    # links are what a doctrinal chain is about.
    omitted = max(0, len(chain) - MAX_TRACE_STEPS)
    chain = chain[:MAX_TRACE_STEPS]
    for step in chain:
        step["quote"] = trim_quote(step["quote"])

    if response_format == "json":
        return as_json(
            {
                "root": ruling_dict(root),
                "max_depth": max_depth,
                "truncated": omitted > 0,
                "omitted_steps": omitted,
                "steps": chain,
                "signals": {k: v.signal.value for k, v in signals.items()},
            }
        )

    lines = [f"## Cadena doctrinaria desde {root.ruling_id.human}", "", QUOTE_NOTICE, ""]
    if not chain:
        lines.append(
            f"No hay fallos posteriores que tomen postura sobre {root.ruling_id.human} "
            "en el corpus. Eso no confirma vigencia: puede ser falta de cobertura."
        )
    for step in chain:
        name = f' "{step["to_name"]}"' if step["to_name"] else ""
        year = f" ({step['year']})" if step["year"] else ""
        flag = "" if step["binding"] else " ⚠ no vinculante"
        lines += [
            f"### {step['from_cite']} → **{step['treatment_label']}** por "
            f"{step['to_cite']}{name}{year}{flag}",
            "",
            render_quote(step["quote"]),
            "",
            f"_confianza {step['confidence']:.2f}_",
            "",
        ]
    if omitted:
        lines += [
            f"_Se muestran {len(chain)} eslabones de {len(chain) + omitted}. "
            "El resto se omitió para no desbordar el contexto: acotá `max_depth` o "
            "consultá un eslabón concreto con `citador_check_status`._",
            "",
        ]
    if signals:
        lines += ["### Estado actual de cada eslabón", ""]
        for node_id, rep in sorted(signals.items()):
            lines.append(f"- {rep.signal.glyph} `{node_id}` — {rep.signal.label_es}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def _text_status_es(status: str) -> str:
    return {
        "extracted": "texto extraído del PDF, utilizable",
        "ocr": "recuperado por OCR",
        "garbled": (
            "**no utilizable**: el PDF trae capa de texto con fuente sin mapa Unicode. "
            "Este fallo no aporta aristas hasta pasar por OCR"
        ),
        "unavailable": "no disponible",
    }.get(status, status)


__all__ = [
    "CitadorError",
    "Signal",
    "check_status",
    "cited_rulings",
    "citing_rulings",
    "lookup_ruling",
    "trace_doctrine",
]
