"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL stored in a Databricks secret scope.
Extended from the Day-2 boilerplate to add weather_documents and
weather_embeddings DDL via ensure_weather_tables().
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

_w = WorkspaceClient()

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _lakebase_url() -> str:
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


# ---------------------------------------------------------------------------
# Weather schema DDL
# ---------------------------------------------------------------------------

_DDL_WEATHER_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS weather_documents (
    id              TEXT PRIMARY KEY,
    location        TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    headline        TEXT NOT NULL DEFAULT '',
    narrative_text  TEXT NOT NULL,
    issued_at       TIMESTAMPTZ,
    payload         JSONB,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_DDL_WEATHER_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS weather_embeddings (
    id              SERIAL PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index     INT  NOT NULL,
    chunk_text      TEXT NOT NULL,
    embedding       vector(384),
    model_name      TEXT NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, chunk_index)
);
"""

_DDL_WEATHER_EMBEDDINGS_INDEX = """
CREATE INDEX IF NOT EXISTS weather_embeddings_hnsw_idx
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""


def ensure_weather_tables() -> None:
    """
    Idempotent DDL: create weather_documents and weather_embeddings tables
    (plus the HNSW vector index) if they don't already exist.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(_DDL_WEATHER_DOCUMENTS)
            cur.execute(_DDL_WEATHER_EMBEDDINGS)
            cur.execute(_DDL_WEATHER_EMBEDDINGS_INDEX)
        conn.commit()
    print("[lakebase] weather tables ready")
