"""The ``citador://corpus`` resource: what the loaded graph actually covers.

Separate from ``server.py``, which is registration only (CLAUDE.md, section 2).

This exists because every other answer the server gives is relative to it. A
grey signal means "no treatment recorded", and whether that is reassuring or
meaningless depends entirely on how much of the corpus is loaded.
"""

from __future__ import annotations

from citador_ar_mcp.store import queries


def status() -> str:
    """Render the corpus provenance: source, build date, size and gaps."""
    with queries.connect() as conn:
        meta = queries.corpus_meta(conn)
        rulings = conn.execute("SELECT count(*) FROM rulings").fetchone()[0]
        citations = conn.execute("SELECT count(*) FROM citations").fetchone()[0]
        by_status = dict(
            conn.execute("SELECT text_status, count(*) FROM rulings GROUP BY text_status")
        )

    lines = [
        "# Estado del corpus",
        "",
        f"- Fallos: {rulings}",
        f"- Aristas de cita: {citations}",
    ]
    if by_status:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
        lines.append(f"- Texto de los fallos: {detail}")
    lines += [f"- {k}: {v}" for k, v in sorted(meta.items())]
    lines += [
        "",
        "La ausencia de tratamiento negativo sobre un fallo no confirma su vigencia: "
        "puede significar que el corpus no cubre los fallos que lo trataron.",
        "",
        "Los fallos marcados `garbled` traen una capa de texto ilegible y no aportan "
        "aristas hasta pasar por OCR; los marcados `ocr` sí aportan, pero su pasaje "
        "es una transcripción automática y puede tener erratas.",
    ]
    return "\n".join(lines)
