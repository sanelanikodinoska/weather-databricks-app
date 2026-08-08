# Weather Intelligence — NWS Vector Search Pipeline

Day 2 homework: Unstructured Data → Lakebase pgvector → REST API

---

## Data Source

**National Weather Service API** (`api.weather.gov`) — free, no API key, generous rate limits.

Returns rich unstructured narrative text ideal for embedding:
- **Active alerts** — `GET /alerts/active?point={lat},{lon}` — free-text description + instruction (e.g. "A Flash Flood Warning means…")
- **Forecast periods** — `GET /gridpoints/{office}/{x},{y}/forecast` — narrative `detailedForecast` per period (e.g. "Partly cloudy, high near 72. Southwest wind 10–15 mph")

No auth plumbing needed — the focus stays on harvest → vectorize → retrieve.

---

## Architecture

```
POST /weather/sync
    └─► WeatherClient (weather_client.py)
            ├─ GET /points/{lat},{lon}           → grid resolution
            ├─ GET /alerts/active?point=...      → active alerts
            └─ GET /gridpoints/{id}/{x},{y}/forecast → forecast periods
        ↓ normalise → upsert → weather_documents (Lakebase Postgres)

notebooks/ingest_weather_embeddings.py   [run in Databricks]
    └─► read unembedded rows from weather_documents
        chunk narrative_text (800 words / 100 overlap)
        embed with all-MiniLM-L6-v2 (384-dim)
        write → weather_embeddings via psycopg2 execute_values

POST /weather/search
    └─► embed query (same model, loaded at app startup)
        pgvector <=> cosine distance over weather_embeddings
        JOIN weather_documents → return top_k as JSON
```

---

## Schema

### `weather_documents`

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | Alert id from NWS, or SHA-256 hash of location+period for forecasts |
| `location` | TEXT | City, ST label |
| `source_type` | TEXT | `'alert'` or `'forecast'` |
| `headline` | TEXT | Alert event name or "Period — Location" |
| `narrative_text` | TEXT | Free-text body to embed |
| `issued_at` | TIMESTAMPTZ | `effective` / `startTime` from NWS |
| `payload` | JSONB | Raw NWS properties for provenance |
| `synced_at` | TIMESTAMPTZ | When this row was written |

### `weather_embeddings`

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `document_id` | TEXT FK | References weather_documents |
| `chunk_index` | INT | Chunk position within the document |
| `chunk_text` | TEXT | The text slice that was embedded |
| `embedding` | vector(384) | all-MiniLM-L6-v2 output |
| `model_name` | TEXT | For future model versioning |
| `created_at` | TIMESTAMPTZ | |

**Index:** `USING hnsw (embedding vector_cosine_ops)` — enables sub-linear ANN search.

**Why 384 dimensions?** Matches `all-MiniLM-L6-v2`, same as the `ticker_news_embeddings` pipeline, so both tables are queryable with the same `<=>` operator conventions.

**Chunking:** `CHUNK_SIZE=800` words, `CHUNK_OVERLAP=100`. Most NWS texts are under 200 words so most documents produce one chunk. The window mainly helps for combined alert description + instruction text.

---

## End-to-End Run

### 1. Deploy the app

Tables are created automatically on startup via `ensure_weather_tables()` in `lakebase.py`.

### 2. Sync weather documents

```bash
curl -X POST https://<your-app-url>/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX", "Miami, FL"], "limit": 50}'
```

Response: `{"synced": <n>}`

Omit `locations` to use 5 built-in defaults (Chicago, Austin, New York, Seattle, Miami).

Locations must be either a known label (`"Chicago, IL"`) or a `"lat,lon"` string (`"41.8781,-87.6298"`).

### 3. Run the embedding notebook

Open `notebooks/ingest_weather_embeddings.py` in Databricks and run all cells. It reads unembedded documents, chunks, embeds, and writes to `weather_embeddings`. Re-running is safe (`ON CONFLICT DO UPDATE`).

### 4. Search

```bash
curl -X POST https://<your-app-url>/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flash flood risk this weekend", "top_k": 5}'
```

Response:
```json
{
  "query": "flash flood risk this weekend",
  "top_k": 5,
  "results": [
    {
      "location": "Chicago, IL",
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "chunk_text": "A Flash Flood Warning means ...",
      "similarity": 0.8812
    }
  ]
}
```

`top_k` is clamped to [1, 20].

---

## Known Limitations & Future Improvements

- **City name resolution** — NWS only accepts lat/lon. Named locations are matched against a hardcoded default list. A production version would use a geocoding API (Census Geocoder or Nominatim) for arbitrary city names.
- **Alert availability** — during calm weather periods many locations have zero active alerts; `/weather/sync` will return forecast-only documents. Expected NWS behaviour.
- **Cold-start latency** — `all-MiniLM-L6-v2` loads at app startup (~3s). Pin to a pre-warmed container in production.
- **Stretch: scheduled refresh** — add a Databricks Workflow cron (e.g. every 6 hours) on the embedding notebook to keep the vector store current.
- **Stretch: RAG summary** — `GET /weather/search?query=...` with an LLM call over the top-k results would complete a minimal RAG pipeline. The retrieval infrastructure is already in place.
