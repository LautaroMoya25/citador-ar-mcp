-- citador_ar_mcp -- the citation graph.
--
-- Three tables, as specified in CLAUDE.md section 3, plus one FTS5 index.
-- Three deviations from the DDL in that section, each forced by something Fase 0
-- verified against the real source. They are listed here rather than buried:
--
--   1. `decided_on` is nullable and `decided_year` is not. The CSJN API returns
--      only a year for old rulings (Colavini -> "1978", Bazterrica -> "1986");
--      a full date appears from roughly tomo 313 on. NOT NULL on `decided_on`
--      would have forced us to invent 1 January dates, and an invented date in a
--      citator is exactly the kind of false precision the project is against.
--   2. `citations.opinion` is new. CLAUDE.md section 5 requires that a citation
--      written in a dissent be marked, or "el citador miente"; that cannot be
--      honoured without storing which opinion the passage came from. The API
--      supplies it (stringVotosMayoria / Voto / Disidencia / DisidenciaParcial).
--   3. `citations.sumario_id` is new and nullable. The CSJN publishes references
--      per *sumario*, not per ruling, which is finer than the model assumed and
--      is what makes "abandoned on one point, good law on another" answerable
--      rather than merely stated.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- A ruling.
CREATE TABLE IF NOT EXISTS rulings (
    id            TEXT PRIMARY KEY,   -- 'fallos:332:1963', canonical identifier
    volume        INTEGER NOT NULL,   -- tomo
    page          INTEGER NOT NULL,
    caption       TEXT    NOT NULL,   -- caratula completa
    short_name    TEXT,               -- 'Arriola', when the ruling has a name of use
    decided_on    DATE,               -- NULL for old rulings: only the year is published
    decided_year  INTEGER NOT NULL,
    source_url    TEXT    NOT NULL,
    text_status   TEXT    NOT NULL,   -- 'extracted' | 'ocr' | 'garbled' | 'unavailable'
    text_quality  REAL,               -- 0.0-1.0, see ingest/extract.py
    csjn_doc_id   INTEGER,            -- idFallo, to re-fetch the PDF

    CHECK (volume > 0 AND page > 0),
    CHECK (text_status IN ('extracted', 'ocr', 'garbled', 'unavailable')),
    UNIQUE (volume, page)
);

CREATE INDEX IF NOT EXISTS idx_rulings_volume_page ON rulings (volume, page);
CREATE INDEX IF NOT EXISTS idx_rulings_year        ON rulings (decided_year);
CREATE INDEX IF NOT EXISTS idx_rulings_short_name  ON rulings (short_name);

-- A citation: ruling A mentions ruling B.
CREATE TABLE IF NOT EXISTS citations (
    citing_id     TEXT NOT NULL REFERENCES rulings (id) ON DELETE CASCADE,
    cited_id      TEXT NOT NULL REFERENCES rulings (id) ON DELETE CASCADE,
    treatment     TEXT NOT NULL,      -- see domain/treatment.py
    opinion       TEXT NOT NULL DEFAULT 'unknown',
    confidence    REAL NOT NULL,      -- 0.0 to 1.0
    quote         TEXT NOT NULL,      -- the passage where it happens, for auditing
    method        TEXT NOT NULL,      -- 'rule' | 'llm' | 'manual'
    sumario_id    INTEGER,            -- CSJN sumario the reference hangs off, if known

    PRIMARY KEY (citing_id, cited_id, quote),
    CHECK (citing_id <> cited_id),
    CHECK (confidence BETWEEN 0.0 AND 1.0),
    CHECK (length(trim(quote)) > 0),  -- no quote, no row. CLAUDE.md section 5.
    CHECK (treatment IN ('applied', 'followed', 'distinguished', 'limited',
                         'criticized', 'abandoned', 'mentioned')),
    CHECK (opinion IN ('majority', 'concurrence', 'dissent',
                       'partial_dissent', 'dictamen', 'unknown')),
    CHECK (method IN ('rule', 'llm', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_citations_cited  ON citations (cited_id, treatment);
CREATE INDEX IF NOT EXISTS idx_citations_citing ON citations (citing_id);

-- The ways one and the same citation gets written.
CREATE TABLE IF NOT EXISTS aliases (
    raw           TEXT PRIMARY KEY,   -- normalised key: 'A.891.XLIV', 'ARRIOLA SEBASTIAN'
    ruling_id     TEXT NOT NULL REFERENCES rulings (id) ON DELETE CASCADE,
    form          TEXT NOT NULL,      -- 'fallos' | 'expediente' | 'short_name' | 'caption'
    source        TEXT NOT NULL,      -- 'api' | 'manual'

    CHECK (form IN ('fallos', 'expediente', 'short_name', 'caption')),
    CHECK (source IN ('api', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_aliases_ruling ON aliases (ruling_id);

-- Full text search over captions and short names, for citador_lookup_ruling
-- when the user has a name rather than a cite. External-content table: the rows
-- live in `rulings`, this is only an index.
CREATE VIRTUAL TABLE IF NOT EXISTS rulings_fts USING fts5 (
    caption,
    short_name,
    content = 'rulings',
    content_rowid = 'rowid',
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS rulings_fts_insert AFTER INSERT ON rulings BEGIN
    INSERT INTO rulings_fts (rowid, caption, short_name)
    VALUES (new.rowid, new.caption, new.short_name);
END;

CREATE TRIGGER IF NOT EXISTS rulings_fts_delete AFTER DELETE ON rulings BEGIN
    INSERT INTO rulings_fts (rulings_fts, rowid, caption, short_name)
    VALUES ('delete', old.rowid, old.caption, old.short_name);
END;

CREATE TRIGGER IF NOT EXISTS rulings_fts_update AFTER UPDATE ON rulings BEGIN
    INSERT INTO rulings_fts (rulings_fts, rowid, caption, short_name)
    VALUES ('delete', old.rowid, old.caption, old.short_name);
    INSERT INTO rulings_fts (rowid, caption, short_name)
    VALUES (new.rowid, new.caption, new.short_name);
END;

-- Provenance of the corpus. One row. Every tool response that states a fact
-- about coverage reads it from here rather than hardcoding a date.
CREATE TABLE IF NOT EXISTS corpus_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
);
