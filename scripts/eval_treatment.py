"""Measure the treatment classifier against the golden fixture.

    uv run python scripts/eval_treatment.py

Prints per-edge results and a confusion matrix. The point is not to reach a
number; it is to see *which* passages the rules cannot read, because that is the
specification for the LLM stage.

Read the failures rather than the score. Five edges is not a benchmark.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from citador_ar_mcp.domain.citation import find_fallos_citations
from citador_ar_mcp.domain.treatment import Treatment
from citador_ar_mcp.ingest.treatment import classify_passage

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "golden" / "chain.json"


def citation_offset(quote: str, cited_id: str) -> int | None:
    """Where the cited ruling appears inside its own quote."""
    for cite in find_fallos_citations(quote):
        if cite.ruling_id is not None and str(cite.ruling_id) == cited_id:
            return cite.start
    return None


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    edges = data["edges"]

    rows = []
    confusion: Counter[tuple[str, str]] = Counter()
    for e in edges:
        expected = Treatment(e["treatment"])
        offset = citation_offset(e["quote"], e["cited_id"])
        got = classify_passage(e["quote"], citation_position=offset, cited=e["cited_id"])
        confusion[(expected.value, got.treatment.value)] += 1
        rows.append((e, expected, got, offset))

    correct = sum(1 for _e, exp, got, _o in rows if exp is got.treatment)
    print(f"Clasificador de tratamiento, contra el golden: {correct}/{len(rows)}\n")

    for e, expected, got, offset in rows:
        mark = "OK  " if expected is got.treatment else "MAL "
        loc = "sin offset" if offset is None else f"offset {offset}"
        print(
            f"{mark} {e['citing']:>10} -> {e['cited']:<11} "
            f"esperado={expected.value:<11} obtuvo={got.treatment.value:<11} "
            f"conf={got.confidence:.2f} regla={got.rule or '-'} ({got.scope}, {loc})"
        )

    print("\n--- matriz de confusión (esperado -> obtenido) ---")
    for (exp, got_), n in sorted(confusion.items()):
        flag = "" if exp == got_ else "   <-- error"
        print(f"  {exp:<12} -> {got_:<12} {n}{flag}")

    # The asymmetry that matters: calling live law dead is the expensive mistake.
    false_negatives = sum(
        n
        for (exp, got_), n in confusion.items()
        if got_ == Treatment.ABANDONED.value and exp != Treatment.ABANDONED.value
    )
    missed = sum(
        n
        for (exp, got_), n in confusion.items()
        if exp == Treatment.ABANDONED.value and got_ != Treatment.ABANDONED.value
    )
    print(f"\n  falsos 'abandonado' (rojo indebido) : {false_negatives}")
    print(f"  'abandonado' no detectados          : {missed}")

    # A missed departure costs coverage; a false one costs a lawyer their
    # authority. Only the second fails the build.
    if false_negatives:
        print(
            "\n  FALLA: un falso rojo hace que un abogado descarte doctrina viva. "
            "Es el error caro y tiene que ser cero."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
