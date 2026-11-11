"""The day's weather at the office, for the greetings at the door: from Open-Meteo (open-meteo.com),
free and without a key. Only the office's place is sent, never anything about people.

`forecast` answers one English sentence for Claude, e.g. "Weather in Hà Nội today: light rain
around 08:00, 24°C; 22-27°C over the day, 80% chance of rain.", or raises when it cannot.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 10.0
COORDS = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*")
# WMO weather interpretation codes, as Open-Meteo gives them
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "light rain showers", 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def _get(url: str, params: dict) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "FaceAttendance"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.load(response)


def place(location: str, fetch=_get) -> tuple[float, float, str]:
    """(latitude, longitude, name) of "21.03,105.85" or of a place name such as "Hà Nội"."""
    found = COORDS.fullmatch(location)
    if found:
        lat, lon = float(found[1]), float(found[2])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"{location} is not a latitude,longitude")
        return lat, lon, location.strip()
    results = fetch(GEOCODE, {"name": location.strip(), "count": 1, "language": "vi", "format": "json"}).get("results") or []
    if not results:
        raise LookupError(f"no place called {location}")
    r = results[0]
    return float(r["latitude"]), float(r["longitude"]), r.get("name") or location.strip()


def forecast(lat: float, lon: float, name: str, day: dt.date, hour: int, fetch=_get) -> str:
    """The weather on `day`, around `hour` (when people arrive) and over the day."""
    data = fetch(FORECAST, {"latitude": lat, "longitude": lon, "timezone": "auto",
                            "start_date": day.isoformat(), "end_date": day.isoformat(),
                            "hourly": "temperature_2m,weather_code",
                            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    hourly, daily = data["hourly"], data["daily"]
    at = f"{day.isoformat()}T{hour:02d}:00"
    i = hourly["time"].index(at) if at in hourly["time"] else 0
    morning = f"{WMO.get(hourly['weather_code'][i], 'unknown weather')} around {hour:02d}:00, {round(hourly['temperature_2m'][i])}°C"
    low, high = round(daily["temperature_2m_min"][0]), round(daily["temperature_2m_max"][0])
    text = f"Weather in {name} today: {morning}; {low}-{high}°C over the day"
    rain = daily.get("precipitation_probability_max", [None])[0]
    if rain is not None:
        text += f", {round(rain)}% chance of rain"
    return text + "."
