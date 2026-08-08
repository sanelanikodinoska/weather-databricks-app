# Databricks notebook source
# MAGIC %md
# MAGIC # Weather Embeddings Ingestion
# MAGIC
# MAGIC Reads unembedded rows from `weather_documents`, chunks `narrative_text`,
# MAGIC embeds with `sentence-transformers/all-MiniLM-L6-v2` (384-dim), and writes
# MAGIC vectors into `weather_embeddings` via psycopg2.
# MAGIC
# MAGIC **Run after** `POST /weather/sync` has populated `weather_documents`.
# MAGIC Do NOT use Spark JDBC writes — not supported against Lakebase.

# COMMAND ----------
# MAGIC %pip install sentence-transformers psycopg2-binary databricks-sdk --quiet

# COMMAND ----------

import base64
import os
from itertools import islice
from typing import Iterator

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor, execute_values
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

dbutils.widgets.text("lakebase_secret_scope", "database")
dbutils.widgets.text("lakebase_secret_key", "lakebase-url")
dbutils.widgets.text("embedding_model", "sentence-transformers/all-MiniLM-L6-v2")
dbutils.widgets.text("chunk_size", "800")
dbutils.widgets.text("chunk_overlap", "100")
dbutils.widgets.text("batch_size", "32")

LAKEBASE_SECRET_SCOPE = dbutils.widgets.get("lakebase_secret_scope")
LAKEBASE_SECRET_KEY   = dbutils.widgets.get("lakebase_secret_key")
EMBEDDING_MODEL       = dbutils.widgets.get("embedding_model")
CHUNK_SIZE            = int(dbutils.widgets.get("chunk_size"))
CHUNK_OVERLAP         = int(dbutils.widgets.get("chunk_overlap"))
BATCH_SIZE            = int(dbutils.widgets.get("batch_size"))

print(f"Model: {EMBEDDING_MODEL} | chunk_size={CHUNK_SIZE} | overlap={CHUNK_OVERLAP} | batch={BATCH_SIZE}")

# ---------------------------------------------------------------------------
# Lakebase connection
# ---------------------------------------------------------------------------

def _get_lakebase_url() -> str:
    w = WorkspaceClient()
    secret = w.secrets.get_secret(scope=LAKEBASE_SECRET_SCOPE, key=LAKEBASE_SECRET_KEY)
    return base64.b64decode(secret.value).decode("utf-8")

_LAKEBASE_URL = _get_lakebase_url()


def get_connection():
    return psycopg2.connect(_LAKEBASE_URL, cursor_factory=RealDictCursor)

# ---------------------------------------------------------------------------
# Chunking — sliding window over words
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks, start = [], 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += chunk_size - overlap
    return chunks


def batched(iterable, n: int) -> Iterator[list]:
    it = iter(iterable)
    while True:
        batch = list(islice(it, n))
        if not batch:
            break
        yield batch

# ---------------------------------------------------------------------------
# Load model
# ---------------------------------------------------------------------------

print(f"Loading {EMBEDDING_MODEL} ...")
model = SentenceTransformer(EMBEDDING_MODEL)
print("Model loaded.")

# ---------------------------------------------------------------------------
# Fetch unembedded documents
# ---------------------------------------------------------------------------

with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.narrative_text
            FROM weather_documents d
            WHERE NOT EXISTS (
                SELECT 1 FROM weather_embeddings e WHERE e.document_id = d.id
            )
            ORDER BY d.synced_at
            """
        )
        unembedded = cur.fetchall()

print(f"Unembedded documents: {len(unembedded)}")

if not unembedded:
    print("Nothing to embed. Exiting.")
    dbutils.notebook.exit("No new documents to embed.")

# ---------------------------------------------------------------------------
# Chunk → embed → write
# ---------------------------------------------------------------------------

all_chunks: list[tuple[str, int, str]] = []
for row in unembedded:
    chunks = chunk_text(row["narrative_text"]) or [row["narrative_text"]]
    for idx, chunk in enumerate(chunks):
        all_chunks.append((row["id"], idx, chunk))

print(f"Total chunks to embed: {len(all_chunks)}")

total_written = 0

with get_connection() as conn:
    with conn.cursor() as cur:
        for batch in batched(all_chunks, BATCH_SIZE):
            texts = [c[2] for c in batch]
            embeddings = model.encode(texts, show_progress_bar=False)

            rows_to_insert = [
                (
                    doc_id,
                    chunk_idx,
                    chunk_txt,
                    "[" + ",".join(str(float(v)) for v in emb.tolist()) + "]",
                    EMBEDDING_MODEL,
                )
                for (doc_id, chunk_idx, chunk_txt), emb in zip(batch, embeddings)
            ]

            execute_values(
                cur,
                """
                INSERT INTO weather_embeddings
                    (document_id, chunk_index, chunk_text, embedding, model_name, created_at)
                VALUES %s
                ON CONFLICT (document_id, chunk_index) DO UPDATE SET
                    chunk_text = EXCLUDED.chunk_text,
                    embedding  = EXCLUDED.embedding,
                    model_name = EXCLUDED.model_name,
                    created_at = NOW()
                """,
                rows_to_insert,
                template="(%s, %s, %s, %s::vector, %s, NOW())",
            )
            total_written += len(rows_to_insert)
            print(f"  Written {total_written}/{len(all_chunks)} chunks ...")

    conn.commit()

print(f"\nDone. Embedded {total_written} chunks from {len(unembedded)} documents.")

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM weather_embeddings")
        n = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(DISTINCT document_id) AS d FROM weather_embeddings")
        d = cur.fetchone()["d"]

print(f"weather_embeddings: {n} rows across {d} documents.")
