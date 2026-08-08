"""
Flask app — Weather Intelligence on Databricks Lakebase.

Routes:
  GET  /healthz              — health check
  POST /weather/sync         — harvest NWS data → weather_documents
  POST /weather/search       — cosine-similarity search over weather_embeddings
  GET  /weather/search?query= — same search + LLM-generated natural-language summary (RAG)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import lakebase
from lakebase import get_connection, run_query
from weather_client import DEFAULT_LOCATIONS, WeatherClient, OpenMeteoClient, parse_location_string

# Open-Meteo client — singleton, stateless, no auth needed
_om = OpenMeteoClient()

# ---------------------------------------------------------------------------
# LLM client (Databricks-hosted model via OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
_LLM_MODEL = os.environ.get("LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")


def _llm_summarise(query: str, results: list[dict]) -> str:
    """
    Generate a natural-language summary of the top weather search results
    using the Databricks-hosted LLM via the OpenAI-compatible endpoint.
    Returns an empty string on any failure (so the GET endpoint degrades gracefully).
    """
    try:
        from openai import OpenAI
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        client = OpenAI(
            api_key=w.config.token,
            base_url=f"{w.config.host}/serving-endpoints",
        )
        context = "\n\n".join(
            f"[{r['location']} — {r['headline']}]\n{r['chunk_text']}"
            for r in results
        )
        prompt = (
            f"You are a weather intelligence assistant. "
            f"A user asked: \"{query}\"\n\n"
            f"Here are the most relevant weather reports:\n\n{context}\n\n"
            f"Write a concise 2-3 sentence natural-language summary answering the user's question "
            f"based only on the reports above. Be specific about locations and risks."
        )
        resp = client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        return f"[summary unavailable: {exc}]"

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Sentence-transformer — loaded ONCE at startup, shared across requests
# ---------------------------------------------------------------------------
_EMBED_MODEL = None
_EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        from sentence_transformers import SentenceTransformer
        _EMBED_MODEL = SentenceTransformer(_EMBED_MODEL_NAME)
    return _EMBED_MODEL


# ---------------------------------------------------------------------------
# Bootstrap — ensure weather tables exist on startup
# ---------------------------------------------------------------------------

def _bootstrap():
    try:
        lakebase.ensure_weather_tables()
    except Exception as exc:
        app.logger.warning(f"[bootstrap] weather table DDL skipped: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    from flask import Response
    return Response('{"status": "ok"}', status=200, mimetype="application/json")


@app.route("/weather/current", methods=["GET"])
def weather_current():
    """
    GET /weather/current?location=Chicago, IL
    Real-time current conditions via Open-Meteo (global, no API key).
    Day 3: this logic becomes the get_current_weather MCP tool.
    """
    location = (request.args.get("location") or "").strip()
    if not location:
        return jsonify({"error": "'location' parameter required"}), 400
    parsed = parse_location_string(location)
    if not parsed:
        return jsonify({"error": f"Cannot resolve '{location}'. Use 'lat,lon' or a known city."}), 400
    lat, lon, label = parsed
    try:
        result = _om.get_current_weather(lat, lon)
        result["location"] = label
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/weather/forecast", methods=["GET"])
def weather_forecast():
    """
    GET /weather/forecast?location=Chicago, IL&days=7
    Multi-day forecast via Open-Meteo (global, no API key).
    Day 3: this logic becomes the get_forecast MCP tool.
    """
    location = (request.args.get("location") or "").strip()
    days = max(1, min(int(request.args.get("days", 7)), 16))
    if not location:
        return jsonify({"error": "'location' parameter required"}), 400
    parsed = parse_location_string(location)
    if not parsed:
        return jsonify({"error": f"Cannot resolve '{location}'."}), 400
    lat, lon, label = parsed
    try:
        forecast = _om.get_forecast(lat, lon, days=days)
        return jsonify({"location": label, "days": days, "forecast": forecast})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/weather/recommend", methods=["GET"])
def weather_recommend():
    """
    GET /weather/recommend?location=Chicago, IL&date=2026-08-10
    Travel/activity recommendation for a specific date via Open-Meteo.
    Applies threshold logic — not a raw API passthrough.
    Day 3: this logic becomes the predict_recommendation MCP tool.
    """
    location = (request.args.get("location") or "").strip()
    date = (request.args.get("date") or "").strip()
    if not location or not date:
        return jsonify({"error": "'location' and 'date' (YYYY-MM-DD) required"}), 400
    parsed = parse_location_string(location)
    if not parsed:
        return jsonify({"error": f"Cannot resolve '{location}'."}), 400
    lat, lon, label = parsed
    try:
        rec = _om.predict_recommendation(lat, lon, date)
        rec["location"] = label
        return jsonify(rec)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/weather/sync", methods=["POST"])
def weather_sync():
    """
    Harvest NWS weather data and upsert into weather_documents.

    Body (JSON, optional):
        {"locations": ["Chicago, IL", "30.2672,-97.7431"], "limit": 50}

    If locations is omitted, 5 default US cities are used.
    Returns: {"synced": <count>}
    """
    body = request.get_json(silent=True) or {}
    raw_locations = body.get("locations", [])
    limit = min(int(body.get("limit", 50)), 200)

    if raw_locations:
        resolved = [parse_location_string(loc) for loc in raw_locations]
        resolved = [r for r in resolved if r]
        if not resolved:
            return jsonify({"error": "No valid locations. Use 'lat,lon' or a known city label."}), 400
    else:
        resolved = list(DEFAULT_LOCATIONS)

    client = WeatherClient()
    try:
        docs = client.harvest(resolved, limit=limit)
    except Exception as exc:
        app.logger.error(f"/weather/sync harvest error: {exc}")
        return jsonify({"error": str(exc)}), 500

    if not docs:
        return jsonify({"synced": 0, "message": "No weather documents returned from NWS."})

    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for d in docs:
                cur.execute(
                    """
                    INSERT INTO weather_documents
                        (id, location, source_type, headline, narrative_text,
                         issued_at, payload, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        headline       = EXCLUDED.headline,
                        narrative_text = EXCLUDED.narrative_text,
                        issued_at      = EXCLUDED.issued_at,
                        payload        = EXCLUDED.payload,
                        synced_at      = EXCLUDED.synced_at
                    """,
                    (d["id"], d["location"], d["source_type"], d["headline"],
                     d["narrative_text"], d["issued_at"], d["payload"], d["synced_at"]),
                )
                count += cur.rowcount
        conn.commit()

    return jsonify({"synced": count})


@app.route("/weather/search", methods=["POST"])
def weather_search():
    """
    Semantic search over weather_embeddings using pgvector cosine distance.

    Body (JSON):
        {"query": "flash flood risk near rivers", "top_k": 5}

    Returns: list of {location, headline, chunk_text, similarity, source_type}
    """
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "'query' is required"}), 400

    top_k = max(1, min(int(body.get("top_k", 5)), 20))

    model = _get_embed_model()
    try:
        vec = model.encode(query).tolist()
    except Exception as exc:
        return jsonify({"error": f"Embedding failed: {exc}"}), 500

    count_rows = run_query("SELECT COUNT(*) AS n FROM weather_embeddings")
    if not count_rows or count_rows[0]["n"] == 0:
        return jsonify({
            "results": [],
            "message": "No embeddings yet. Run POST /weather/sync then the ingestion notebook."
        })

    vec_str = "[" + ",".join(str(x) for x in vec) + "]"
    sql = """
        SELECT
            d.id, d.location, d.source_type, d.headline,
            e.chunk_text,
            1 - (e.embedding <=> %s::vector) AS similarity
        FROM weather_embeddings e
        JOIN weather_documents d ON d.id = e.document_id
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
    """
    try:
        rows = run_query(sql, (vec_str, vec_str, top_k))
    except Exception as exc:
        app.logger.error(f"/weather/search query error: {exc}")
        return jsonify({"error": str(exc)}), 500

    results = [
        {
            "id": r["id"],
            "location": r["location"],
            "source_type": r["source_type"],
            "headline": r["headline"],
            "chunk_text": r["chunk_text"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
    return jsonify({"results": results, "query": query, "top_k": top_k})


@app.route("/weather/search", methods=["GET"])
def weather_search_rag():
    """
    Stretch goal: GET /weather/search?query=...&top_k=5
    Same vector search as POST, plus an LLM-generated natural-language summary.

    Returns: {query, summary, results}
    """
    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "'query' parameter is required"}), 400

    top_k = max(1, min(int(request.args.get("top_k", 5)), 20))

    model = _get_embed_model()
    try:
        vec = model.encode(query).tolist()
    except Exception as exc:
        return jsonify({"error": f"Embedding failed: {exc}"}), 500

    count_rows = run_query("SELECT COUNT(*) AS n FROM weather_embeddings")
    if not count_rows or count_rows[0]["n"] == 0:
        return jsonify({
            "results": [],
            "summary": "No weather data available yet.",
            "message": "Run POST /weather/sync then the ingestion notebook first."
        })

    vec_str = "[" + ",".join(str(x) for x in vec) + "]"
    sql = """
        SELECT
            d.id, d.location, d.source_type, d.headline,
            e.chunk_text,
            1 - (e.embedding <=> %s::vector) AS similarity
        FROM weather_embeddings e
        JOIN weather_documents d ON d.id = e.document_id
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
    """
    try:
        rows = run_query(sql, (vec_str, vec_str, top_k))
    except Exception as exc:
        app.logger.error(f"GET /weather/search query error: {exc}")
        return jsonify({"error": str(exc)}), 500

    results = [
        {
            "id": r["id"],
            "location": r["location"],
            "source_type": r["source_type"],
            "headline": r["headline"],
            "chunk_text": r["chunk_text"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]

    summary = _llm_summarise(query, results)

    return jsonify({"query": query, "summary": summary, "top_k": top_k, "results": results})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _bootstrap()
    app.run(host="0.0.0.0", port=8000, debug=False)
