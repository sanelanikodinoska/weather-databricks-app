"""Weather Intelligence MCP Server — FastMCP tools for Databricks Assistant.

Exposes 3 MCP tools:
  - get_current_weather_tool: Real-time conditions
  - get_forecast_tool: Multi-day forecast
  - predict_recommendation_tool: Travel recommendation with threshold logic

Run as a Databricks App with app.yaml.
"""

from fastmcp import FastMCP
import os
from weather_broker import (
    get_current_weather,
    get_forecast,
    predict_recommendation,
    convert_units,
)

mcp = FastMCP("weather-intelligence")


@mcp.tool()
def get_current_weather_tool(location: str) -> dict:
    """
    Get real-time weather conditions for any city or coordinates.

    Args:
        location: City name (e.g. "Tokyo", "London, UK") or "lat,lon"
                  (e.g. "48.8566,2.3522")

    Returns:
        temperature_f, feels_like_f, humidity_pct, wind_speed_mph,
        precipitation_in, conditions (human-readable), timestamp
    """
    try:
        return get_current_weather(location)
    except Exception as e:
        return {"error": str(e), "location": location}


@mcp.tool()
def get_forecast_tool(location: str, days: int = 7) -> dict:
    """
    Get a multi-day daily weather forecast.

    Args:
        location: City name or "lat,lon"
        days: Number of forecast days, 1 to 16 (default 7)

    Returns:
        location, days, forecast list with date/temp/precip/conditions per day
    """
    try:
        return get_forecast(location, days)
    except Exception as e:
        return {"error": str(e), "location": location}


@mcp.tool()
def predict_recommendation_tool(location: str, date: str) -> dict:
    """
    Get a travel or activity recommendation for a specific date.

    Applies threshold logic — umbrella if rain >= 60%, jacket if high < 55F,
    thunderstorm warning if severe weather expected.

    Args:
        location: City name or "lat,lon"
        date: Target date in YYYY-MM-DD format

    Returns:
        recommendation (plain English), alerts (list), confidence,
        plus conditions/temp_high_f/temp_low_f/precip_probability_pct
    """
    try:
        return predict_recommendation(location, date)
    except Exception as e:
        return {"error": str(e), "location": location, "date": date}


@mcp.tool()
def convert_units_tool(value: float, from_unit: str, to_unit: str) -> dict:
    """
    Convert a weather measurement between unit systems.

    Use this after getting weather data to present values in the user's preferred units.
    The weather tools always return Fahrenheit, mph, and inches — call this tool to convert.

    Supported conversions:
        Temperature:   F ↔ C
        Wind speed:    mph ↔ kmh
        Precipitation: inch ↔ mm

    Args:
        value: Numeric value to convert
        from_unit: Source unit (F, C, mph, kmh, inch, mm)
        to_unit: Target unit (F, C, mph, kmh, inch, mm)

    Returns:
        value, from_unit, converted, to_unit
    """
    try:
        return convert_units(value, from_unit, to_unit)
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("DATABRICKS_APP_PORT", 8000)),
    )