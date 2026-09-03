"""Mine the corpus for the phrases the Court actually uses around a citation.

    uv run python scripts/mine_formulas.py --db data/corpus.db --top 60

The rule set in ``ingest/treatment.py`` was written from the golden chain, which
is four rulings spread over thirty years. That is not how the Court cites day to
day, and it showed: 88% of real passages came back unclassified. Rather than pay
a model to read them one by one, this reads all of them at once and reports what
recurs.

Method, and its one deliberate choice: n-grams are taken from the words
**immediately before** the citation, inside its own clause. Spanish puts the
governing verb there -- *corresponde aplicar la doctrina de* **Fallos: X** --
so the window that matters is the left one. Everything is lowercased, accent-
folded and stripped of numbers so that spelling and citation noise collapse.

The output is a candidate list, not a rule set. A frequent phrase is not
necessarily a treatment-bearing one: "y su cita" is the most common thing in the
corpus and means only that the Court is stacking support. Reading the list and
deciding which phrases carry meaning is the part that cannot be automated, and
the whole point of doing this instead of asking a model.
"""

from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from citador_ar_mcp.domain.citation import find_fallos_citations
from citador_ar_mcp.ingest.treatment import local_clause

#: Words carrying no clausal meaning on their own. Dropped only from the ends of
#: an n-gram, so "de la doctrina" keeps its shape while "de la" is discarded.
EDGE_NOISE = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "y",
    "e",
    "o",
    "u",
    "a",
    "al",
    "en",
    "que",
    "por",
    "con",
    "un",
    "una",
    "su",
    "sus",
    "se",
    "lo",
    "es",
}

WINDOW_WORDS = 8
MIN_N, MAX_N = 2, 6

#: Only phrases built around one of these stems are reported.
#:
#: Without this the ranking is topic and party names -- "constitucion nacional",
#: "jose marmol ocupantes de la finca" -- because those recur far more than any
#: formula does. A treatment in Spanish is carried by a verb: the Court applies,
#: follows, departs from, reiterates, narrows. Anchoring on the verb is what
#: separates a formula from a subject matter.
VERB_STEMS = (
    "aplic",
    "segu",
    "sigu",
    "remit",
    "apart",
    "abandon",
    "reiter",
    "sostuv",
    "sostien",
    "sostien",
    "resolv",
    "resuelt",
    "decid",
    "declar",
    "confirm",
    "revoc",
    "compart",
    "coincid",
    "invoc",
    "recuerd",
    "senal",
    "expres",
    "establec",
    "sento",
    "sentad",
    "distingu",
    "difier",
    "limit",
    "restring",
    "critic",
    "objet",
    "analog",
    "doctrin",
    "criteri",
    "precedent",
    "jurisprud",
    "conform",
    "arregl",
    "corresponde",
    "cabe",
    "procede",
)


def carries_a_verb(gram: str) -> bool:
    return any(stem in gram for stem in VERB_STEMS)


def fold(text: str) -> str:
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(ch)
    )
    text = re.sub(r"\d+", " ", text)
    return " ".join(re.findall(r"[a-zñ]+", text))


def left_window(quote: str, position: int) -> list[str]:
    """The words just before the citation, bounded by its own clause."""
    clause = local_clause(quote, position)
    marker = quote[position : position + 8]
    idx = clause.find(marker)
    before = clause[:idx] if idx > 0 else clause
    return fold(before).split()[-WINDOW_WORDS:]


def ngrams(words: list[str]) -> set[str]:
    out: set[str] = set()
    for n in range(MIN_N, MAX_N + 1):
        for i in range(len(words) - n + 1):
            gram = words[i : i + n]
            while gram and gram[0] in EDGE_NOISE:
                gram = gram[1:]
            while gram and gram[-1] in EDGE_NOISE:
                gram = gram[:-1]
            if len(gram) >= MIN_N:
                out.add(" ".join(gram))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("data/corpus.db"))
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--min-count", type=int, default=12)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT cited_id, quote FROM citations
           WHERE quote NOT LIKE 'Referencia publicada por la CSJN%'
             AND confidence <= 0.25"""
    ).fetchall()

    counts: collections.Counter[str] = collections.Counter()
    examples: dict[str, str] = {}
    for cited, quote in rows:
        pos = next(
            (
                c.start
                for c in find_fallos_citations(quote)
                if c.ruling_id and str(c.ruling_id) == cited
            ),
            None,
        )
        if pos is None:
            continue
        for gram in ngrams(left_window(quote, pos)):
            if not carries_a_verb(gram):
                continue
            counts[gram] += 1
            examples.setdefault(gram, quote)

    print(f"pasajes analizados: {len(rows)}\n")
    print(f"{'veces':>6}  frase")
    print("-" * 72)
    for gram, n in counts.most_common(args.top):
        if n < args.min_count:
            break
        print(f"{n:>6}  {gram}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
