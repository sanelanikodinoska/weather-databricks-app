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
        return {"lat": lat, "lon": lon, "label": location.strip(), "is_imperial": False}

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

        country_code = result.get("country_code", "")
        is_imperial = country_code.upper() in ("US", "LR", "MM")  # Only US, Liberia, Myanmar use imperial

        return {"lat": lat, "lon": lon, "label": label, "is_imperial": is_imperial}

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
    imperial = loc["is_imperial"]
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
        "precipitation_unit": "inch" if imperial else "mm",
        "timezone": "auto",
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    current = data["current"]
    weather_code = current.get("weather_code", 0)
    conditions = WMO_CODES.get(weather_code, "Unknown")
    t_unit = "F" if imperial else "C"
    w_unit = "mph" if imperial else "km/h"
    p_unit = "in" if imperial else "mm"

    return {
        "location": loc["label"],
        "unit_system": "imperial" if imperial else "metric",
        "temperature": f"{current['temperature_2m']}°{t_unit}",
        "feels_like": f"{current['apparent_temperature']}°{t_unit}",
        "humidity_pct": current["relative_humidity_2m"],
        "wind_speed": f"{current['wind_speed_10m']} {w_unit}",
        "precipitation": f"{current['precipitation']} {p_unit}",
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
    imperial = loc["is_imperial"]
    days = max(1, min(16, days))  # Clamp to 1-16

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": loc["lat"],
        "longitude": loc["lon"],
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
        "precipitation_unit": "inch" if imperial else "mm",
        "timezone": "auto",
        "forecast_days": days,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    daily = data["daily"]
    t_unit = "F" if imperial else "C"
    p_unit = "in" if imperial else "mm"
    forecast_list = []

    for i in range(len(daily["time"])):
        weather_code = daily["weather_code"][i]
        conditions = WMO_CODES.get(weather_code, "Unknown")

        forecast_list.append({
            "date": daily["time"][i],
            "temp_high": f"{daily['temperature_2m_max'][i]}°{t_unit}",
            "temp_low": f"{daily['temperature_2m_min'][i]}°{t_unit}",
            "temp_high_raw": daily["temperature_2m_max"][i],
            "temp_low_raw": daily["temperature_2m_min"][i],
            "precipitation": f"{daily['precipitation_sum'][i]} {p_unit}",
            "precip_probability_pct": daily["precipitation_probability_max"][i],
            "conditions": conditions,
            "weather_code": weather_code,
        })

    return {
        "location": loc["label"],
        "unit_system": "imperial" if imperial else "metric",
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
    temp_high_raw = target_day["temp_high_raw"]   # numeric, in location's native unit
    temp_low_raw = target_day["temp_low_raw"]
    temp_high_str = target_day["temp_high"]       # formatted string e.g. "32.4°C"
    temp_low_str = target_day["temp_low"]
    conditions = target_day["conditions"]
    weather_code = target_day.get("weather_code", 0)
    imperial = forecast_data.get("unit_system", "metric") == "imperial"

    # Threshold logic — always in the location's native unit
    # Imperial thresholds in °F; metric equivalents: 40°F≈4°C, 55°F≈13°C, 95°F≈35°C
    alerts = []

    if precip_prob >= 60:
        alerts.append("High chance of rain — bring umbrella.")
    elif precip_prob >= 40:
        alerts.append("Possible rain — consider an umbrella.")

    if weather_code >= 95:
        alerts.append("Thunderstorm — avoid outdoor travel.")

    if 71 <= weather_code <= 77:
        alerts.append("Snow expected — check road conditions.")

    if imperial:
        if temp_high_raw < 40:
            alerts.append("Very cold — heavy winter clothing required.")
        elif temp_high_raw < 55:
            alerts.append("Cool — light jacket recommended.")
        if temp_high_raw > 95:
            alerts.append("Extreme heat — stay hydrated.")
    else:
        if temp_high_raw < 4:
            alerts.append("Very cold — heavy winter clothing required.")
        elif temp_high_raw < 13:
            alerts.append("Cool — light jacket recommended.")
        if temp_high_raw > 35:
            alerts.append("Extreme heat — stay hydrated.")

    # Overall recommendation
    if not alerts:
        recommendation = f"Pleasant conditions expected. {conditions} with highs around {temp_high_str}."
        confidence = "high"
    else:
        recommendation = alerts[0]
        confidence = "medium" if len(alerts) == 1 else "high"

    return {
        "location": forecast_data["location"],
        "unit_system": forecast_data.get("unit_system", "metric"),
        "date": date,
        "conditions": conditions,
        "temp_high": temp_high_str,
        "temp_low": temp_low_str,
        "precip_probability_pct": precip_prob,
        "recommendation": recommendation,
        "alerts": alerts,
        "confidence": confidence,
    }