"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "chain.json"


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The Colavini -> Arriola chain, as annotated in the fixture.

    Regenerate with ``uv run --extra ingest python scripts/build_golden_fixture.py``.
    """
    if not GOLDEN.exists():  # pragma: no cover
        pytest.skip(f"falta el fixture dorado: {GOLDEN}")
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def arriola_text() -> str:
    """The body of Arriola, cleaned. Skips when the PDF cache is absent.

    The PDF is not committed: it is 470 KB of someone else's publication, and
    ``scripts/build_golden_fixture.py`` re-downloads it on demand.
    """
    pdf = Path(__file__).parents[1] / "data" / "cache" / "arriola.pdf"
    if not pdf.exists():
        pytest.skip("falta data/cache/arriola.pdf: correr scripts/build_golden_fixture.py")
    pypdfium2 = pytest.importorskip("pypdfium2", reason="requiere el extra 'ingest'")
    assert pypdfium2 is not None
    from citador_ar_mcp.ingest.citations import join_page_breaks
    from citador_ar_mcp.ingest.extract import extract_pdf

    return join_page_breaks(extract_pdf(pdf).text)
