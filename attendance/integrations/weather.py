"""The day's weather at the office, for the greetings at the door: from Open-Meteo (open-meteo.com),
free and without a key. Only the office's place is sent, never anything about people.

`forecast` answers one English sentence for Claude, e.g. "Weather in Hà Nội today: light rain
around 08:00, 24°C; 22-27°C over the day, 80% chance of rain.", or raises when it cannot.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
SECOND = "https://wttr.in/{lat},{lon}"  # asked when Open-Meteo does not answer
TIMEOUT_S = 8.0
TRIES = 3  # Open-Meteo sometimes does not answer a connection at all: asked again before giving up
COORDS = re.compile(r"\s*\(?\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*")
NUMBER = r"(-?\d{1,3}(?:\.\d+)?)"
# Where a Google Maps address carries the place: the pin (!3d..!4d..) before the middle of the map (@.. , ..).
IN_LINK = [re.compile(rf"!3d{NUMBER}!4d{NUMBER}"), re.compile(rf"[?&](?:q|query|ll|destination|center)={NUMBER}(?:,|%2C){NUMBER}"),
           re.compile(rf"/@{NUMBER},{NUMBER}"), re.compile(rf"/(?:place|search)/{NUMBER},(?:\+|%20)?{NUMBER}")]
SHORT_LINK = re.compile(r"https://(maps\.app\.goo\.gl|goo\.gl/maps|g\.co/kgs)/\S+")


def coordinates(text: str) -> str | None:
    """"lat,lon" found in what was pasted from Google Maps: the numbers copied there ("21.0285, 105.8542") or the
    address of a place; None when there are none (a place name, or a short link, which has to be opened first)."""
    text = text.strip()
    found = COORDS.fullmatch(text)
    if not found and text.lower().startswith("http"):
        found = next((m for m in (pattern.search(text) for pattern in IN_LINK) if m), None)
    if not found:
        return None
    lat, lon = float(found[1]), float(found[2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return f"{round(lat, 5)},{round(lon, 5)}"


def _opened(link: str) -> str:
    """The long Google Maps address a short link leads to."""
    request = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0 FaceAttendance"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.geturl()
# WMO weather interpretation codes, as Open-Meteo gives them
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "light rain showers", 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def _get(url: str, params: dict, tries: int = TRIES) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "FaceAttendance"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return json.load(response)
        except OSError:  # no answer, timeout: once more; an answer that says no (HTTPError) is one too, on the last try
            if attempt == tries - 1:
                raise
            time.sleep(1.5)


def place(location: str, fetch=_get) -> tuple[float, float, str]:
    """(latitude, longitude, name) of "21.03,105.85" or of a place name such as "Hà Nội"."""
    found = coordinates(location)
    if not found and SHORT_LINK.fullmatch(location.strip()):
        found = coordinates(_opened(location.strip()))
        if not found:
            raise LookupError("that Google Maps link has no coordinates; copy the numbers of the place instead")
    if found:
        lat, lon = (float(x) for x in found.split(","))
        return lat, lon, "the office's area"
    if location.strip().lower().startswith("http"):
        raise ValueError("no coordinates in that address; in Google Maps right-click the place and click the numbers")
    results = fetch(GEOCODE, {"name": location.strip(), "count": 1, "language": "vi", "format": "json"}).get("results") or []
    if not results:
        raise LookupError(f"no place called {location}")
    r = results[0]
    return float(r["latitude"]), float(r["longitude"]), r.get("name") or location.strip()


def forecast(lat: float, lon: float, name: str, day: dt.date, hour: int, fetch=_get) -> str:
    """The weather on `day`, around `hour` (when people arrive) and over the day: from Open-Meteo, and from
    wttr.in when Open-Meteo does not answer."""
    try:
        return _open_meteo(lat, lon, name, day, hour, fetch)
    except Exception as exc:
        log.warning("Open-Meteo gave no weather (%s: %s); asking wttr.in", type(exc).__name__, str(exc)[:120])
        return _second(lat, lon, name, day, hour, fetch)


def _second(lat: float, lon: float, name: str, day: dt.date, hour: int, fetch=_get) -> str:
    data = fetch(SECOND.format(lat=lat, lon=lon), {"format": "j1"})
    today = next(d for d in data["weather"] if d.get("date") == day.isoformat())
    slot = min(today["hourly"], key=lambda h: abs(int(h["time"]) // 100 - hour))
    said = (slot.get("weatherDesc") or [{}])[0].get("value", "unknown weather").strip().lower()
    text = (f"Weather in {name} today: {said} around {hour:02d}:00, {round(float(slot['tempC']))}°C; "
            f"{round(float(today['mintempC']))}-{round(float(today['maxtempC']))}°C over the day")
    rain = max((int(h.get("chanceofrain") or 0) for h in today["hourly"]), default=None)
    return text + (f", {rain}% chance of rain." if rain is not None else ".")


def _open_meteo(lat: float, lon: float, name: str, day: dt.date, hour: int, fetch=_get) -> str:
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
