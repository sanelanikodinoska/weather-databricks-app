""
Weather client — two API adapters in one module.

NWS (WeatherClient)
--------------------
National Weather Service API (api.weather.gov).
US-only, no API key. Used for Day 2: harvest unstructured narrative text
(alerts + forecast discussions) → Lakebase pgvector for semantic search.

Open-Meteo (OpenMeteoClient)
-----------------------------
Open-Meteo API (api.open-meteo.com).
Global, no API key, ~10,000 calls/day (non-commercial).
Used for Day 2 real-time endpoints AND pre-built as the Day 3 MCP broker:
  - get_current_weather(lat, lon)   → Day 3 MCP tool: get_current_weather
  - get_forecast(lat, lon, days)    → Day 3 MCP tool: get_forecast
  - predict_recommendation(lat,lon,date) → Day 3 MCP tool: predict_recommendation

Day 3 note: the three Open-Meteo functions below are intentionally thin and
self-contained so they can be imported directly into a FastMCP @mcp.tool
decorator with zero refactoring. The broker pattern (HTTP logic here, thin
tool wrapper in mcp_server) is already in place.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

_TIMEOUT = 30
_USER_AGENT = "DatabricksWeatherApp/1.0 (snikodinoska@gmail.com)"

# Default locations (lat, lon, friendly name)
DEFAULT_LOCATIONS: list[tuple[float, float, str]] = [
    (41.8781, -87.6298, "Chicago, IL"),
    (30.2672, -97.7431, "Austin, TX"),
    (40.7128, -74.0060, "New York, NY"),
    (47.6062, -122.3321, "Seattle, WA"),
    (25.7617, -80.1918, "Miami, FL"),
]

# WMO weather interpretation codes → human-readable description
_WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _wmo_description(code: int) -> str:
    return _WMO_CODES.get(code, f"Unknown conditions (WMO {code})")


# ---------------------------------------------------------------------------
# Open-Meteo client (global, no API key) — Day 2 real-time + Day 3 MCP broker
# ---------------------------------------------------------------------------

_OM_BASE = "https://api.open-meteo.com/v1"


class OpenMeteoClient:
    """
    Adapter for the Open-Meteo API.

    All three public methods (get_current_weather, get_forecast,
    predict_recommendation) are designed to be called directly from
    Day 3 FastMCP @mcp.tool functions — no changes needed, just import and wrap.
    """

    def __init__(self, timeout: int = _TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def _get(self, path: str, params: dict) -> dict:
        resp = self._session.get(f"{_OM_BASE}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_current_weather(self, lat: float, lon: float) -> dict:
        """
        Fetch current conditions for a location.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            dict with keys: temperature_f, feels_like_f, humidity_pct,
            wind_speed_mph, precipitation_in, conditions (human-readable),
            weather_code, timestamp

        Day 3 MCP tool: get_current_weather(location) — call resolve_lat_lon()
        first to convert a city name to lat/lon, then call this function.
        """
        data = self._get("/forecast", {
            "latitude": lat,
            "longitude": lon,
            "current": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "wind_speed_10m",
                "precipitation",
                "weather_code",
            ]),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
        })
        c = data.get("current", {})
        return {
            "temperature_f": c.get("temperature_2m"),
            "feels_like_f": c.get("apparent_temperature"),
            "humidity_pct": c.get("relative_humidity_2m"),
            "wind_speed_mph": c.get("wind_speed_10m"),
            "precipitation_in": c.get("precipitation"),
            "conditions": _wmo_description(c.get("weather_code", 0)),
            "weather_code": c.get("weather_code"),
            "timestamp": c.get("time"),
        }

    def get_forecast(self, lat: float, lon: float, days: int = 7) -> list[dict]:
        """
        Fetch a multi-day daily forecast.

        Args:
            lat: Latitude
            lon: Longitude
            days: Number of forecast days (1–16, default 7)

        Returns:
            List of daily dicts, each with: date, temp_high_f, temp_low_f,
            precipitation_in, precip_probability_pct, conditions, weather_code

        Day 3 MCP tool: get_forecast(location, days) — thin wrapper calling this.
        """
        days = max(1, min(days, 16))
        data = self._get("/forecast", {
            "latitude": lat,
            "longitude": lon,
            "daily": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "weather_code",
            ]),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
            "forecast_days": days,
        })
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        result = []
        for i, date in enumerate(dates):
            result.append({
                "date": date,
                "temp_high_f": daily.get("temperature_2m_max", [None])[i],
                "temp_low_f": daily.get("temperature_2m_min", [None])[i],
                "precipitation_in": daily.get("precipitation_sum", [None])[i],
                "precip_probability_pct": daily.get("precipitation_probability_max", [None])[i],
                "conditions": _wmo_description(daily.get("weather_code", [0])[i] or 0),
                "weather_code": daily.get("weather_code", [None])[i],
            })
        return result

    def predict_recommendation(self, lat: float, lon: float, date: str) -> dict:
        """
        Generate a travel/activity recommendation for a specific date.

        Applies threshold logic to the forecast — does NOT echo raw API data.
        Thresholds:
          - precip_probability >= 60%  → bring umbrella
          - weather_code >= 95         → thunderstorm warning, avoid travel
          - temp_high < 40°F           → heavy winter clothing required
          - temp_high < 55°F           → light jacket recommended
          - temp_high > 95°F           → heat advisory, stay hydrated
          - weather_code in 71-77      → snow, check road conditions

        Args:
            lat: Latitude
            lon: Longitude
            date: ISO date string (YYYY-MM-DD)

        Returns:
            dict with: date, conditions, temp_high_f, temp_low_f,
            precip_probability_pct, recommendation (plain English),
            alerts (list of specific warnings), confidence ("high"/"medium"/"low")

        Day 3 MCP tool: predict_recommendation(location, date) — thin wrapper.
        """
        forecast = self.get_forecast(lat, lon, days=16)
        day = next((d for d in forecast if d["date"] == date), None)

        if not day:
            return {
                "date": date,
                "recommendation": f"No forecast available for {date} (too far ahead or past date).",
                "alerts": [],
                "confidence": "low",
            }

        alerts = []
        code = day.get("weather_code") or 0
        precip_prob = day.get("precip_probability_pct") or 0
        temp_high = day.get("temp_high_f") or 70
        temp_low = day.get("temp_low_f") or 50

        # Precipitation
        if precip_prob >= 60:
            alerts.append(f"High chance of precipitation ({precip_prob}%) — bring an umbrella.")
        elif precip_prob >= 40:
            alerts.append(f"Moderate chance of precipitation ({precip_prob}%) — consider an umbrella.")

        # Thunderstorm
        if code >= 95:
            alerts.append("Thunderstorm forecast — consider rescheduling outdoor activities.")

        # Snow
        if 71 <= code <= 77:
            alerts.append("Snow expected — check road conditions before travelling.")

        # Temperature
        if temp_high < 40:
            alerts.append(f"Very cold (high {temp_high}°F) — heavy winter clothing required.")
        elif temp_high < 55:
            alerts.append(f"Cool day (high {temp_high}°F) — light jacket recommended.")
        elif temp_high > 95:
            alerts.append(f"Extreme heat (high {temp_high}°F) — stay hydrated and avoid midday sun.")

        # Overall recommendation
        if code >= 95:
            recommendation = "Avoid outdoor travel if possible due to thunderstorm risk."
        elif 71 <= code <= 77:
            recommendation = "Dress warmly and allow extra travel time due to snow."
        elif precip_prob >= 60:
            recommendation = "Pack an umbrella and waterproof layers."
        elif not alerts:
            recommendation = "Good conditions — no special precautions needed."
        else:
            recommendation = " ".join(alerts)

        confidence = "high" if len(forecast) > 0 and forecast[0]["date"] <= date <= forecast[min(6, len(forecast)-1)]["date"] else "medium"

        return {
            "date": date,
            "conditions": day["conditions"],
            "temp_high_f": temp_high,
            "temp_low_f": temp_low,
            "precip_probability_pct": precip_prob,
            "recommendation": recommendation,
            "alerts": alerts,
            "confidence": confidence,
        }


# ---------------------------------------------------------------------------
# NWS client (US-only) — Day 2 harvest pipeline for pgvector
# ---------------------------------------------------------------------------

_NWS_BASE = "https://api.weather.gov"


class WeatherClient:
    """
    Adapter for the NWS API.
    Used by the Day 2 harvest pipeline: harvest() → weather_documents → pgvector.
    For real-time current/forecast data use OpenMeteoClient instead.
    """

    def __init__(self, timeout: int = _TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/geo+json",
        })

    def _get(self, url: str, params: dict | None = None) -> Any:
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def resolve_point(self, lat: float, lon: float) -> dict:
        data = self._get(f"{_NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        return data.get("properties", {})

    def get_alerts_for_point(self, lat: float, lon: float) -> list[dict]:
        data = self._get(f"{_NWS_BASE}/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
        return data.get("features", [])

    def get_forecast(self, grid_id: str, grid_x: int, grid_y: int) -> list[dict]:
        data = self._get(f"{_NWS_BASE}/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast")
        return data.get("properties", {}).get("periods", [])

    @staticmethod
    def _stable_id(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def normalise_alert(self, feature: dict, location_label: str, synced_at: str) -> dict | None:
        props = feature.get("properties", {})
        alert_id = props.get("id") or feature.get("id", "")
        headline = props.get("headline") or props.get("event", "")
        description = props.get("description", "")
        instruction = props.get("instruction", "")
        narrative = "\n\n".join(filter(None, [description, instruction])).strip()
        if not narrative:
            return None
        effective = props.get("effective") or props.get("sent") or synced_at
        return {
            "id": alert_id or self._stable_id(f"alert:{location_label}:{narrative[:80]}"),
            "location": location_label,
            "source_type": "alert",
            "headline": headline[:500] if headline else "",
            "narrative_text": narrative,
            "issued_at": effective,
            "payload": json.dumps(props),
            "synced_at": synced_at,
        }

    def normalise_forecast_period(self, period: dict, location_label: str, grid_id: str, synced_at: str) -> dict | None:
        narrative = period.get("detailedForecast", "").strip()
        if not narrative:
            return None
        period_name = period.get("name", "")
        start_time = period.get("startTime", synced_at)
        dedup_key = f"forecast:{location_label}:{grid_id}:{period_name}:{start_time}"
        return {
            "id": self._stable_id(dedup_key),
            "location": location_label,
            "source_type": "forecast",
            "headline": f"{period_name} forecast — {location_label}",
            "narrative_text": narrative,
            "issued_at": start_time,
            "payload": json.dumps(period),
            "synced_at": synced_at,
        }

    def harvest(self, locations: list[tuple[float, float, str]], limit: int = 50,
                include_alerts: bool = True, include_forecast: bool = True) -> list[dict]:
        """
        Harvest NWS alert + forecast narrative documents for pgvector ingestion.
        Returns a list of normalised document dicts for weather_documents.
        """
        synced_at = datetime.now(timezone.utc).isoformat()
        docs: list[dict] = []

        for lat, lon, label in locations:
            if len(docs) >= limit:
                break
            try:
                grid = self.resolve_point(lat, lon)
                grid_id = grid.get("gridId")
                grid_x = grid.get("gridX")
                grid_y = grid.get("gridY")
            except Exception as exc:
                print(f"  [warn] grid resolve failed for {label}: {exc}")
                grid_id = grid_x = grid_y = None

            if include_alerts and len(docs) < limit:
                try:
                    alerts = self.get_alerts_for_point(lat, lon)
                    time.sleep(0.5)
                    for feat in alerts:
                        doc = self.normalise_alert(feat, label, synced_at)
                        if doc:
                            docs.append(doc)
                        if len(docs) >= limit:
                            break
                except Exception as exc:
                    print(f"  [warn] alerts failed for {label}: {exc}")

            if include_forecast and grid_id and grid_x is not None and len(docs) < limit:
                try:
                    periods = self.get_forecast(grid_id, grid_x, grid_y)
                    time.sleep(0.5)
                    for period in periods:
                        doc = self.normalise_forecast_period(period, label, grid_id, synced_at)
                        if doc:
                            docs.append(doc)
                        if len(docs) >= limit:
                            break
                except Exception as exc:
                    print(f"  [warn] forecast failed for {label}: {exc}")

        return docs[:limit]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_location_string(loc_str: str) -> tuple[float, float, str] | None:
    """
    Accept either:
      - "City, ST" strings — matched against DEFAULT_LOCATIONS by label
      - "lat,lon" numeric strings — used directly
    Returns (lat, lon, label) or None if unparsable.
    """
    loc_str = loc_str.strip()
    parts = loc_str.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
            return (lat, lon, loc_str)
        except ValueError:
            pass
    for lat, lon, label in DEFAULT_LOCATIONS:
        if label.lower() == loc_str.lower():
            return (lat, lon, label)
    print(f"  [warn] cannot resolve '{loc_str}' — use 'lat,lon' or a known city label")
    return None
