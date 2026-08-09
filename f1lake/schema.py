"""
Lakebase connection helper and the authoritative schema.

Lakebase is Databricks-managed Postgres. It is the OPERATIONAL half of this
project: the agent reads from it and, crucially, writes to it. The medallion
Gold marts in Delta remain the analytical half. Two stores, one shared key -
every table here carries (season, round), the same key `f1.silver.dim_race`
uses, so a semantic hit in a race report pivots straight into that race's
results.

WHY THE DDL LIVES HERE
----------------------
`databricks psql` connects as the workspace identity, while this code and the
deployed apps connect as the native Postgres role inside the secret URL.
Postgres grants table ownership to whoever ran CREATE TABLE. Tables created via
psql would be readable by the app but never alterable or indexable by it, which
surfaces much later as a 42501 on CREATE INDEX. Creating them here means the
role that reads and writes also owns.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

# all-MiniLM-L6-v2. Must match the embedding model and the VECTOR(n) column;
# pgvector rejects an insert whose dimensionality differs from the column.
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))

DOCUMENTS = "f1_documents"
EMBEDDINGS = "f1_embeddings"
WEATHER = "f1_race_weather"
RACES = "f1_races"
WATCHLIST = "f1_watchlist"
PREDICTIONS = "f1_predictions"
NOTES = "f1_race_notes"

_cached_url: str | None = None


def lakebase_url() -> str:
    """Resolve the Postgres URL, preferring an explicit env var for local dev."""
    global _cached_url
    if _cached_url is not None:
        return _cached_url

    explicit = os.environ.get("LAKEBASE_URL")
    if explicit:
        _cached_url = explicit
        return _cached_url

    # Imported lazily so LAKEBASE_URL alone is enough to run with no Databricks
    # auth configured at all.
    from databricks.sdk import WorkspaceClient

    secret = WorkspaceClient().secrets.get_secret(scope=_SCOPE, key=_KEY)
    _cached_url = base64.b64decode(secret.value).decode("utf-8")
    return _cached_url


@contextmanager
def connection():
    conn = psycopg2.connect(lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def returning(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a writing statement with a RETURNING clause, and COMMIT it.

    Distinct from query() on purpose. An INSERT ... RETURNING run through
    query() looks entirely successful - the RETURNING clause hands back the new
    row, id and all - but query() never commits, so closing the connection rolls
    the write back. The caller sees a row it will never be able to read again.

    That is a silent data-loss bug: an agent would tell a user "saved" and the
    database would disagree. Writes go through this function; reads go through
    query(); the two are not interchangeable.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            conn.commit()
            return rows


def execute(sql: str, params: tuple | dict | None = None) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",

    # --- reference: the race spine, mirrored from Gold ------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {RACES} (
        season          INT NOT NULL,
        round           INT NOT NULL,
        race_name       TEXT NOT NULL,
        race_date       DATE,
        circuit_id      TEXT,
        circuit_name    TEXT,
        circuit_country TEXT,
        circuit_lat     DOUBLE PRECISION,
        circuit_long    DOUBLE PRECISION,
        wikipedia_url   TEXT,
        PRIMARY KEY (season, round)
    )
    """,

    # --- unstructured: race reports ------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {DOCUMENTS} (
        id          TEXT PRIMARY KEY,
        season      INT NOT NULL,
        round       INT NOT NULL,
        race_name   TEXT NOT NULL,
        race_date   DATE,
        circuit_id  TEXT,
        section     TEXT NOT NULL,
        title       TEXT,
        url         TEXT,
        body        TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'wikipedia_race_report',
        synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENTS}_race ON {DOCUMENTS} (season, round)",

    f"""
    CREATE TABLE IF NOT EXISTS {EMBEDDINGS} (
        id          TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES {DOCUMENTS} (id) ON DELETE CASCADE,
        season      INT NOT NULL,
        round       INT NOT NULL,
        section     TEXT,
        chunk_index INT NOT NULL,
        chunk_text  TEXT NOT NULL,
        embedding   VECTOR({EMBEDDING_DIM}) NOT NULL,
        model_name  TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (document_id, chunk_index)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{EMBEDDINGS}_race ON {EMBEDDINGS} (season, round)",
    # HNSW over IVFFlat: IVFFlat picks centroids at build time and needs
    # representative rows to already exist, which is wrong for a table that
    # starts empty. vector_cosine_ops must match the `<=>` operator used at
    # query time - an index built with a different opclass is silently ignored.
    f"CREATE INDEX IF NOT EXISTS idx_{EMBEDDINGS}_hnsw "
    f"ON {EMBEDDINGS} USING hnsw (embedding vector_cosine_ops)",

    # --- measured race-day weather -------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {WEATHER} (
        season           INT NOT NULL,
        round            INT NOT NULL,
        race_name        TEXT NOT NULL,
        race_date        DATE,
        circuit_id       TEXT,
        circuit_name     TEXT,
        conditions       TEXT,
        temp_max         DOUBLE PRECISION,
        temp_min         DOUBLE PRECISION,
        temp_mean        DOUBLE PRECISION,
        precipitation_mm DOUBLE PRECISION,
        rain_mm          DOUBLE PRECISION,
        wind_speed_max   DOUBLE PRECISION,
        wind_gusts_max   DOUBLE PRECISION,
        was_wet          BOOLEAN NOT NULL,
        wet_threshold_mm DOUBLE PRECISION NOT NULL,
        source           TEXT NOT NULL DEFAULT 'open-meteo-archive',
        synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (season, round)
    )
    """,

    # --- agent WRITE targets --------------------------------------------------
    f"""
    CREATE TABLE IF NOT EXISTS {WATCHLIST} (
        id          BIGSERIAL PRIMARY KEY,
        user_id     TEXT NOT NULL DEFAULT 'default',
        entity_type TEXT NOT NULL CHECK (entity_type IN ('driver','constructor','circuit')),
        entity_ref  TEXT NOT NULL,
        note        TEXT,
        added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, entity_type, entity_ref)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PREDICTIONS} (
        id          BIGSERIAL PRIMARY KEY,
        user_id     TEXT NOT NULL DEFAULT 'default',
        season      INT NOT NULL,
        round       INT NOT NULL,
        prediction  TEXT NOT NULL,
        confidence  TEXT CHECK (confidence IN ('low','medium','high')),
        rationale   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{PREDICTIONS}_race ON {PREDICTIONS} (season, round)",
    f"""
    CREATE TABLE IF NOT EXISTS {NOTES} (
        id         BIGSERIAL PRIMARY KEY,
        user_id    TEXT NOT NULL DEFAULT 'default',
        season     INT NOT NULL,
        round      INT NOT NULL,
        note       TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{NOTES}_race ON {NOTES} (season, round)",
]


def ensure_schema() -> None:
    """Create every table and index. Idempotent, safe to call on any write path.

    Each statement runs in its own transaction (autocommit). psycopg2 puts a
    connection into an aborted state after any error, so in one shared
    transaction a single recoverable failure - the 42501 that CREATE EXTENSION
    raises for a non-superuser when the extension already exists - would roll
    back every table created before it.
    """
    with connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for statement in DDL:
                try:
                    cur.execute(statement)
                except psycopg2.errors.InsufficientPrivilege:
                    # Only CREATE EXTENSION should reach here. Anything else
                    # genuinely failing surfaces later as a missing table.
                    continue
