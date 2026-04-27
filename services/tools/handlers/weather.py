"""Weather handler: wttr.in (no key) or OpenWeatherMap."""

import os

import httpx

WTTR_URL = "https://wttr.in/{location}"
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


async def get_weather(location: str) -> dict:
    """Get weather for location. Output: weather dict. Input: location string."""
    if not os.getenv("TOOLS_WEATHER_ENABLED", "false").lower() in ("true", "1", "yes"):
        return {"error": "weather is disabled (TOOLS_WEATHER_ENABLED=false)"}

    provider = os.getenv("TOOLS_WEATHER_PROVIDER", "wttr").lower().strip()
    if provider == "openweathermap":
        return await _owm_weather(location)
    return await _wttr_weather(location)


async def _wttr_weather(location: str) -> dict:
    """wttr.in JSON weather (no API key required). Output: weather dict. Input: location."""
    url = WTTR_URL.format(location=location)
    params = {"format": "j1"}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "location": location}

    try:
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        area_name = area["areaName"][0]["value"]
        country = area["country"][0]["value"]
        desc = current["weatherDesc"][0]["value"]

        return {
            "provider": "wttr.in",
            "location": f"{area_name}, {country}",
            "temperature_c": int(current["temp_C"]),
            "temperature_f": int(current["temp_F"]),
            "feels_like_c": int(current["FeelsLikeC"]),
            "humidity_pct": int(current["humidity"]),
            "wind_kmph": int(current["windspeedKmph"]),
            "wind_direction": current["winddir16Point"],
            "visibility_km": int(current["visibility"]),
            "description": desc,
            "uv_index": int(current.get("uvIndex", 0)),
        }
    except (KeyError, IndexError, ValueError) as exc:
        return {"error": f"Failed to parse wttr.in response: {exc}", "location": location}


async def _owm_weather(location: str) -> dict:
    """OpenWeatherMap weather. Output: weather dict. Input: location."""
    api_key = os.getenv("TOOLS_WEATHER_OWM_KEY", "").strip()
    if not api_key:
        return {"error": "OpenWeatherMap selected but TOOLS_WEATHER_OWM_KEY is not set"}

    params = {"q": location, "appid": api_key, "units": "metric"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(OWM_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"OWM API error {exc.response.status_code}", "location": location}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "location": location}

    return {
        "provider": "openweathermap",
        "location": f"{data.get('name', location)}, {data.get('sys', {}).get('country', '')}",
        "temperature_c": round(data["main"]["temp"], 1),
        "feels_like_c": round(data["main"]["feels_like"], 1),
        "humidity_pct": data["main"]["humidity"],
        "wind_mps": data.get("wind", {}).get("speed", 0),
        "description": data["weather"][0]["description"] if data.get("weather") else "",
        "visibility_m": data.get("visibility", 0),
    }
