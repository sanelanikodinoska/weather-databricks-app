"""
Weather client — National Weather Service (NWS) API harvester.

No API key required. Mirrors the structure of massive_client.py.

Given a list of locations (lat/lon pairs or city strings that resolve via
the /points endpoint), this module:
  1. Resolves each location to a NWS grid point.
  2. Fetches active alerts and multi-day forecast discussions.
  3. Normalises each item into a document dict ready for weather_documents.

NWS API base: https://api.weather.gov
User-Agent header is required by NWS — we identify ourselves politely.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import requests

_BASE_URL = "https://api.weather.gov"
_TIMEOUT = 30
_USER_AGENT = "DatabricsWeatherApp/1.0 (snikodinoska@gmail.com)"

# Default locations (lat, lon, friendly name)
DEFAULT_LOCATIONS: list[tuple[float, float, str]] = [
    (41.8781, -87.6298, "Chicago, IL"),
    (30.2672, -97.7431, "Austin, TX"),
    (40.7128, -74.0060, "New York, NY"),
    (47.6062, -122.3321, "Seattle, WA"),
    (25.7617, -80.1918, "Miami, FL"),
]


class WeatherClient:
    """Thin, stateless NWS API client."""

    def __init__(self, timeout: int = _TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "application/geo+json",
            }
        )

    def _get(self, url: str, params: dict | None = None) -> Any:
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Grid resolution
    # ------------------------------------------------------------------

    def resolve_point(self, lat: float, lon: float) -> dict:
        """
        Resolve a lat/lon to NWS grid metadata.
        Returns the 'properties' dict from GET /points/{lat},{lon}.
        Keys of interest: gridId, gridX, gridY, relativeLocation.
        """
        data = self._get(f"{_BASE_URL}/points/{lat:.4f},{lon:.4f}")
        return data.get("properties", {})

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def get_alerts_for_point(self, lat: float, lon: float) -> list[dict]:
        """Fetch active alerts whose zone covers (lat, lon)."""
        data = self._get(
            f"{_BASE_URL}/alerts/active",
            params={"point": f"{lat:.4f},{lon:.4f}"},
        )
        return data.get("features", [])

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def get_forecast(self, grid_id: str, grid_x: int, grid_y: int) -> list[dict]:
        """
        Fetch multi-day forecast periods from the NWS grid endpoint.
        Returns a list of period dicts, each containing name + detailedForecast.
        """
        data = self._get(
            f"{_BASE_URL}/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast"
        )
        return data.get("properties", {}).get("periods", [])

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stable_id(text: str) -> str:
        """SHA-256 prefix used as a stable dedup key for forecast docs."""
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def normalise_alert(
        self, feature: dict, location_label: str, synced_at: str
    ) -> dict | None:
        """
        Convert a raw NWS alert feature into a weather_documents row.
        Returns None if the alert has no narrative text worth embedding.
        """
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

    def normalise_forecast_period(
        self,
        period: dict,
        location_label: str,
        grid_id: str,
        synced_at: str,
    ) -> dict | None:
        """
        Convert a single forecast period into a weather_documents row.
        """
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

    # ------------------------------------------------------------------
    # High-level harvest
    # ------------------------------------------------------------------

    def harvest(
        self,
        locations: list[tuple[float, float, str]],
        limit: int = 50,
        include_alerts: bool = True,
        include_forecast: bool = True,
    ) -> list[dict]:
        """
        Harvest documents for a list of (lat, lon, label) tuples.
        Returns a list of normalised document dicts.
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

            # -- alerts --
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

            # -- forecast --
            if include_forecast and grid_id and grid_x is not None and len(docs) < limit:
                try:
                    periods = self.get_forecast(grid_id, grid_x, grid_y)
                    time.sleep(0.5)
                    for period in periods:
                        doc = self.normalise_forecast_period(
                            period, label, grid_id, synced_at
                        )
                        if doc:
                            docs.append(doc)
                        if len(docs) >= limit:
                            break
                except Exception as exc:
                    print(f"  [warn] forecast failed for {label}: {exc}")

        return docs[:limit]


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
