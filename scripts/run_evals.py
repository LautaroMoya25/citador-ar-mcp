"""Check every claim the evals assert, against a real graph.

    uv run python scripts/run_evals.py --db data/corpus.db

This is not an agent harness: it does not put the questions to a model and grade
the prose that comes back. It does the part that can be automated and that
actually rots -- verifying that the *facts* each expected answer states are
still true of the graph. An eval whose expected answer has quietly become wrong
is worse than no eval, because it will be trusted.

Each check names the eval it belongs to and runs through the tools, not through
SQL, so a change in how a tool renders or aggregates is caught here too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

COLAVINI = "fallos:300:254"
BAZTERRICA = "fallos:308:1392"
MONTALVO = "fallos:313:1333"
ARRIOLA = "fallos:332:1963"


def checks() -> list[tuple[str, Callable[[], object]]]:
    """One entry per eval, in the order they appear in evaluation.xml."""
    from citador_ar_mcp.tools import citator
    from citador_ar_mcp.tools._common import CitadorError

    def j(raw: str) -> dict:
        return json.loads(raw)

    def eval_1() -> object:
        d = j(citator.lookup_ruling("A. 891. XLIV", "json"))
        assert d["id"] == ARRIOLA, d["id"]
        assert d["decided_on"] == "25/08/2009", d["decided_on"]
        assert "ARRIOLA" in d["caption"].upper()
        return "resuelve a Arriola, 25/08/2009"

    def eval_2() -> object:
        r = j(citator.check_status(MONTALVO, "json"))["report"]
        assert r["signal"] == "red", r["signal"]
        assert r["binding_counts"].get("abandoned") == 1, r["binding_counts"]
        top = r["evidence"][0]
        assert top["treatment"] == "abandoned" and top["cite"] == "Fallos: 332:1963"
        assert top["quote"].strip()
        return f"rojo, abandonado por Arriola, con pasaje ({len(top['quote'])} chars)"

    def eval_3() -> object:
        r = j(citator.check_status(BAZTERRICA, "json"))["report"]
        assert r["signal"] == "yellow", r["signal"]
        assert r["binding_counts"].get("abandoned") == 1
        assert r["binding_counts"].get("followed") == 1
        assert any("retomado" in c for c in r["caveats"])
        return "amarillo: abandonado por Montalvo, retomado por Arriola"

    def eval_4() -> object:
        d = j(citator.cited_rulings(ARRIOLA, limit=100, response_format="json"))
        ids = {i["id"] for i in d["items"]}
        assert {COLAVINI, BAZTERRICA, MONTALVO} <= ids, sorted(ids)[:5]
        return f"los tres precedentes de la cadena están (de {d['total']} citados)"

    def eval_5() -> object:
        out = {}
        for rid in (COLAVINI, BAZTERRICA, MONTALVO, ARRIOLA):
            r = j(citator.check_status(rid, "json"))["report"]
            out[rid] = r["binding_counts"].get("abandoned", 0)
        assert out[BAZTERRICA] == 1 and out[MONTALVO] == 1, out
        assert out[COLAVINI] == 0, out
        return "abandonados por la mayoría: Bazterrica y Montalvo; Colavini no"

    def eval_6() -> object:
        r = j(citator.check_status(COLAVINI, "json"))["report"]
        assert r["signal"] == "gray", r["signal"]
        # Anchored on meaning, not on wording: both the "nothing cites it" and
        # the "nothing could be classified" caveats say the precedent is not
        # confirmed, and the exact phrasing has already been improved once.
        assert any("confirmad" in c for c in r["caveats"]), r["caveats"]
        return "gris, y la respuesta advierte que gris no confirma vigencia"

    def eval_7() -> object:
        r = j(citator.check_status(BAZTERRICA, "json"))["report"]
        followed = [e for e in r["evidence"] if e["treatment"] == "followed"]
        assert followed, "no hay tratamiento 'followed'"
        assert followed[0]["opinion"] == "majority", followed[0]["opinion"]
        assert followed[0]["binding"] is True
        return "el pasaje que sigue a Bazterrica es del voto de la mayoría"

    def eval_8() -> object:
        status = {
            rid: j(citator.lookup_ruling(rid, "json"))["text_status"]
            for rid in (COLAVINI, BAZTERRICA, MONTALVO, ARRIOLA)
        }
        assert status[ARRIOLA] == "extracted", status
        assert all(status[r] == "ocr" for r in (COLAVINI, BAZTERRICA, MONTALVO)), status
        return "tres por OCR, Arriola extraído del PDF"

    def eval_9() -> object:
        d = j(citator.trace_doctrine(BAZTERRICA, max_depth=3, response_format="json"))
        steps = {(s["from"], s["to"], s["treatment"]) for s in d["steps"]}
        assert (BAZTERRICA, MONTALVO, "abandoned") in steps, sorted(steps)[:4]
        assert (BAZTERRICA, ARRIOLA, "followed") in steps
        assert (MONTALVO, ARRIOLA, "abandoned") in steps
        assert d["signals"][MONTALVO] == "red"
        assert d["signals"][BAZTERRICA] == "yellow"
        return "la cadena se reconstruye con los tres eslabones y sus señales"

    def eval_10() -> object:
        try:
            citator.lookup_ruling("Fallos 308:1932")
        except CitadorError as exc:
            assert "308:1392" in str(exc), str(exc)
            return "no resuelve y sugiere 308:1392 (Bazterrica)"
        raise AssertionError("resolvió una cita que no existe")

    return [
        ("1. resolver por número de expediente", eval_1),
        ("2. Montalvo: ¿sigue siendo buen derecho?", eval_2),
        ("3. Bazterrica: historia completa", eval_3),
        ("4. precedentes en que se apoyó Arriola", eval_4),
        ("5. qué fallos fueron abandonados", eval_5),
        ("6. ¿se puede afirmar que Colavini sigue vigente?", eval_6),
        ("7. mayoría o voto concurrente", eval_7),
        ("8. qué fallos necesitaron OCR", eval_8),
        ("9. cadena desde Bazterrica", eval_9),
        ("10. cita con dígitos transpuestos", eval_10),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/corpus.db"))
    args = ap.parse_args()
    os.environ["CITADOR_DB"] = str(args.db)

    print(f"evals contra {args.db}\n")
    passed = failed = 0
    for name, check in checks():
        try:
            detail = check()
        except AssertionError as exc:
            print(f"  MAL  {name}\n         {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERR  {name}\n         {type(exc).__name__}: {str(exc)[:160]}")
            failed += 1
        else:
            print(f"  OK   {name}\n         {detail}")
            passed += 1

    print(f"\n  {passed}/{passed + failed} evals verificadas")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
