"""Re-read, with a model, the passages the rules could not classify.

    uv run --extra llm python scripts/reclassify_with_llm.py --budget 20 --top 15

Aimed narrowly: only citations that point at the N most-cited rulings and that
came back unclassified. Those are the edges that decide whether a leading case
shows a real signal, and they are a few hundred rather than the corpus's tens of
thousands.

**The budget is enforced, not estimated.** Cost is accumulated from the ``usage``
each response reports and checked before every call, so the run stops on its own
rather than being trusted to land under the limit. It calibrates on a small
batch first and prints the projection, because a per-call cost measured on ten
real passages beats one reasoned from token counts.

Nothing here overrides a rule: only rows the rules left at fallback confidence
are touched, and the model's verdict still has to quote a phrase that appears in
the passage before it is written.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from citador_ar_mcp.domain.citation import find_fallos_citations
from citador_ar_mcp.ingest import llm
from citador_ar_mcp.ingest.treatment import UNCLASSIFIED_CONFIDENCE

log = logging.getLogger("reclassify")

TARGETS = """
WITH top AS (
    SELECT cited_id FROM citations
    GROUP BY cited_id ORDER BY count(DISTINCT citing_id) DESC LIMIT ?
)
SELECT citing_id, cited_id, quote FROM citations
WHERE cited_id IN (SELECT cited_id FROM top)
  AND confidence <= ?
  AND quote NOT LIKE 'Referencia publicada por la CSJN%'
ORDER BY cited_id, citing_id
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/corpus.db"))
    ap.add_argument("--budget", type=float, required=True, help="tope en dólares, obligatorio")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--calibrate", type=int, default=10, help="llamadas de calibración")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh"])
    ap.add_argument("--dry-run", action="store_true", help="contar y salir, sin gastar")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(TARGETS, (args.top, UNCLASSIFIED_CONFIDENCE + 0.05)).fetchall()
    print(f"pasajes sin clasificar que apuntan a los {args.top} más citados: {len(rows)}")
    if args.dry_run or not rows:
        return 0

    if not llm.available():
        print("no hay credencial ni SDK: instalá el extra 'llm' o corré `ant auth login`")
        return 1

    budget = llm.Budget(limit_usd=args.budget)
    classifier = llm.ClaudeTreatmentClassifier(effort=args.effort, budget=budget)

    # Calibration. Measure the real cost before committing to the rest.
    head = rows[: args.calibrate]
    print(f"\ncalibrando con {len(head)} llamadas (effort={args.effort})...")
    changed = _run(conn, classifier, head)
    conn.commit()
    print(f"  {budget.summary()}")
    if budget.calls:
        projection = budget.per_call * len(rows)
        print(f"  proyección para {len(rows)}: ${projection:.2f} de ${args.budget:.2f}")
        if projection > args.budget:
            alcanza = int(args.budget / budget.per_call)
            print(
                f"  no alcanza para todos; se harán {alcanza} y el resto queda sin leer.\n"
                f"  (bajar --effort abarata; el presupuesto frena solo de todos modos)"
            )

    rest = rows[args.calibrate :]
    print(f"\nclasificando el resto ({len(rest)})...")
    changed += _run(conn, classifier, rest)
    conn.commit()

    print(f"\n{budget.summary()}")
    print(f"  reclasificadas: {changed} de {len(rows)}")
    if budget.exhausted:
        print("  el presupuesto se agotó antes de terminar; volver a correr lo retoma")
    conn.close()
    return 0


def _run(
    conn: sqlite3.Connection,
    classifier: llm.ClaudeTreatmentClassifier,
    rows: list[tuple[str, str, str]],
) -> int:
    changed = 0
    for i, (citing, cited, quote) in enumerate(rows, 1):
        if classifier.budget is not None and not classifier.budget.affords():
            break
        # A passage often carries several citations, so the prompt names which
        # one is under review. The model gets the whole passage: unlike the
        # rules, reading across clauses is the point of asking it.
        if not any(c.ruling_id and str(c.ruling_id) == cited for c in find_fallos_citations(quote)):
            # The stored passage does not contain the citation it is filed
            # under. Nothing to read, and paying to ask would be worse.
            continue
        result = classifier.classify(quote, cited=cited)
        if result is None or result.is_fallback:
            continue
        conn.execute(
            "UPDATE citations SET treatment = ?, confidence = ?, method = ? "
            "WHERE citing_id = ? AND cited_id = ? AND quote = ?",
            (result.treatment.value, result.confidence, result.method.value, citing, cited, quote),
        )
        changed += 1
        if i % 25 == 0:
            conn.commit()
            b = classifier.budget
            log.info(
                "  %s/%s, %s reclasificadas%s",
                i,
                len(rows),
                changed,
                f", ${b.spent_usd:.2f}" if b else "",
            )
    return changed


if __name__ == "__main__":
    raise SystemExit(main())
