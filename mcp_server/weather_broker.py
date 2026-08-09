"""Weather Intelligence MCP Broker — HTTP adapter for Open-Meteo APIs.

Provides geocoding, current weather, forecast, and recommendation services.
Self-contained — no parent imports.
"""

import re
import requests
from typing import Dict, Any

# WMO Weather interpretation codes (WMO 4677)
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def resolve_location(location: str) -> Dict[str, Any]:
    """
    Resolve a location string to lat/lon coordinates and label.

    Args:
        location: Either "lat,lon" (e.g. "48.8566,2.3522") or city name
                  (e.g. "Tokyo", "London, UK")

    Returns:
        {"lat": float, "lon": float, "label": str}

    Raises:
        ValueError: If location cannot be resolved
    """
    # Try parsing as "lat,lon" pattern
    coord_match = re.match(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$", location.strip())
    if coord_match:
        lat = float(coord_match.group(1))
        lon = float(coord_match.group(2))
        return {"lat": lat, "lon": lon, "label": location.strip()}

    # Otherwise, geocode via Open-Meteo
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {
        "name": location,
        "count": 1,
        "language": "en",
        "format": "json",
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if not data.get("results"):
            raise ValueError(f"Cannot resolve location: '{location}'")

        result = data["results"][0]
        lat = result["latitude"]
        lon = result["longitude"]
        name = result["name"]
        admin1 = result.get("admin1", "")
        country = result.get("country", "")

        # Build label: "Name, Admin1, Country" or variations
        label_parts = [name]
        if admin1:
            label_parts.append(admin1)
        if country:
            label_parts.append(country)
        label = ", ".join(label_parts)

        return {"lat": lat, "lon": lon, "label": label}

    except requests.exceptions.RequestException as e:
        raise ValueError(f"Geocoding request failed: {e}")


def get_current_weather(location: str) -> Dict[str, Any]:
    """
    Get real-time weather conditions for a location.

    Args:
        location: City name or "lat,lon"

    Returns:
        {
            "location": str,
            "temperature_f": float,
            "feels_like_f": float,
            "humidity_pct": int,
            "wind_speed_mph": float,
            "precipitation_in": float,
            "conditions": str,
            "timestamp": str
        }
    """
    loc = resolve_location(location)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    current = data["current"]
    weather_code = current.get("weather_code", 0)
    conditions = WMO_CODES.get(weather_code, "Unknown")

    return {
        "location": loc["label"],
        "temperature_f": current["temperature_2m"],
        "feels_like_f": current["apparent_temperature"],
        "humidity_pct": current["relative_humidity_2m"],
        "wind_speed_mph": current["wind_speed_10m"],
        "precipitation_in": current["precipitation"],
        "conditions": conditions,
        "timestamp": current["time"],
    }


def get_forecast(location: str, days: int = 7) -> Dict[str, Any]:
    """
    Get multi-day daily weather forecast.

    Args:
        location: City name or "lat,lon"
        days: Number of forecast days (1-16, default 7)

    Returns:
        {
            "location": str,
            "days": int,
            "forecast": [
                {
                    "date": str,
                    "temp_high_f": float,
                    "temp_low_f": float,
                    "precipitation_in": float,
                    "precip_probability_pct": int,
                    "conditions": str
                },
                ...
            ]
        }
    """
    loc = resolve_location(location)
    days = max(1, min(16, days))  # Clamp to 1-16

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": days,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    daily = data["daily"]
    forecast_list = []

    for i in range(len(daily["time"])):
        weather_code = daily["weather_code"][i]
        conditions = WMO_CODES.get(weather_code, "Unknown")

        forecast_list.append({
            "date": daily["time"][i],
            "temp_high_f": daily["temperature_2m_max"][i],
            "temp_low_f": daily["temperature_2m_min"][i],
            "precipitation_in": daily["precipitation_sum"][i],
            "precip_probability_pct": daily["precipitation_probability_max"][i],
            "conditions": conditions,
            "weather_code": weather_code,
        })

    return {
        "location": loc["label"],
        "days": days,
        "forecast": forecast_list,
    }


def predict_recommendation(location: str, date: str) -> Dict[str, Any]:
    """
    Get travel/activity recommendation for a specific date.

    Applies threshold logic for rain, snow, temperature extremes, thunderstorms.

    Args:
        location: City name or "lat,lon"
        date: Target date in YYYY-MM-DD format

    Returns:
        {
            "location": str,
            "date": str,
            "conditions": str,
            "temp_high_f": float,
            "temp_low_f": float,
            "precip_probability_pct": int,
            "recommendation": str,
            "alerts": [str],
            "confidence": str
        }
    """
    forecast_data = get_forecast(location, days=16)
    target_day = None

    for day in forecast_data["forecast"]:
        if day["date"] == date:
            target_day = day
            break

    if not target_day:
        return {
            "location": forecast_data["location"],
            "date": date,
            "recommendation": "No forecast available for this date.",
            "alerts": [],
            "confidence": "low",
        }

    # Extract values
    precip_prob = target_day["precip_probability_pct"]
    temp_high = target_day["temp_high_f"]
    temp_low = target_day["temp_low_f"]
    conditions = target_day["conditions"]
    weather_code = target_day.get("weather_code", 0)

    # Apply threshold logic
    alerts = []

    if precip_prob >= 60:
        alerts.append("High chance of rain — bring umbrella.")
    elif precip_prob >= 40:
        alerts.append("Possible rain — consider an umbrella.")

    if weather_code >= 95:
        alerts.append("Thunderstorm — avoid outdoor travel.")

    if 71 <= weather_code <= 77:
        alerts.append("Snow expected — check road conditions.")

    if temp_high < 40:
        alerts.append("Very cold — heavy winter clothing required.")
    elif temp_high < 55:
        alerts.append("Cool — light jacket recommended.")

    if temp_high > 95:
        alerts.append("Extreme heat — stay hydrated.")

    # Overall recommendation
    if not alerts:
        recommendation = f"Pleasant conditions expected. {conditions} with highs around {temp_high:.0f}°F."
        confidence = "high"
    else:
        # Pick most severe alert as primary recommendation
        recommendation = alerts[0]
        confidence = "medium" if len(alerts) == 1 else "high"

    return {
        "location": forecast_data["location"],
        "date": date,
        "conditions": conditions,
        "temp_high_f": temp_high,
        "temp_low_f": temp_low,
        "precip_probability_pct": precip_prob,
        "recommendation": recommendation,
        "alerts": alerts,
        "confidence": confidence,
    }