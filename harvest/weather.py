"""
Race-day weather — the third data source, and the one that grounds the narrative.

A race report says "a chaotic wet race". This says 12.4 mm of rain fell at
Interlagos that day. One is an adjective; the other is a measurement, and the
difference is what lets the agent answer "which races were actually wet?"
without trusting an encyclopaedia's choice of words.

WHY THE ARCHIVE AND NOT THE FORECAST
------------------------------------
Open-Meteo serves forecasts and history from different hosts backed by different
datasets. Past races need the ERA5 reanalysis archive; the forecast endpoint
will not serve a 2024 date at all. The archive trails real time by roughly five
days, so very recent races have no observations yet - those are skipped rather
than recorded as zero rainfall, because "no data" and "no rain" are different
claims and conflating them would quietly corrupt every wet-race query.

No API key, no signup. Coordinates come from `dim_race.circuit_lat/long`, which
the pipeline already carries for all 71 races, so there is no geocoding step and
no chance of resolving the wrong circuit.
"""

import datetime
import logging
import time

import requests

logger = logging.getLogger("weather")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 30
COURTESY_DELAY = 0.2
ARCHIVE_LAG_DAYS = 5

# Above this, a race is "wet" for the purposes of the agent's summary. 1.0 mm
# over a day is roughly the point where rain stops being a passing shower and
# starts affecting a race - tyre choice, safety cars, grip. Named here, quoted
# in the agent's tool docstring, so the threshold is explainable rather than
# arbitrary.
WET_RACE_MM = 1.0

DAILY_FIELDS = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "rain_sum",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
]

# WMO interpretation codes - the archive returns an integer, and an agent
# answering "what were conditions like" needs words.
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


class WeatherError(RuntimeError):
    """The archive could not be queried, or held no observations for that date."""


def describe(code) -> str:
    try:
        return WMO_CODES.get(int(code), f"unknown conditions (WMO {code})")
    except (TypeError, ValueError):
        return "unknown conditions"


def archive_cutoff(today: datetime.date | None = None) -> datetime.date:
    """Latest date the archive can be expected to have observations for."""
    today = today or datetime.date.today()
    return today - datetime.timedelta(days=ARCHIVE_LAG_DAYS)


def fetch_race_weather(race, session: requests.Session | None = None) -> dict:
    """Fetch observed weather for one race's date at its circuit."""
    session = session or requests.Session()
    try:
        response = session.get(
            ARCHIVE_URL,
            params={
                "latitude": race.circuit_lat,
                "longitude": race.circuit_long,
                "start_date": race.race_date,
                "end_date": race.race_date,
                "daily": ",".join(DAILY_FIELDS),
                "timezone": "auto",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise WeatherError(f"Archive request failed: {exc}") from exc
    except ValueError as exc:
        raise WeatherError("Archive returned non-JSON") from exc

    daily = payload.get("daily") or {}
    units = payload.get("daily_units") or {}
    if not (daily.get("time") or []):
        raise WeatherError(f"No observations for {race.race_date}")

    def first(field):
        values = daily.get(field) or []
        return values[0] if values else None

    precipitation = first("precipitation_sum")
    rainfall = float(precipitation) if precipitation is not None else 0.0

    return {
        "season": race.season,
        "round": race.round,
        "race_name": race.race_name,
        "race_date": race.race_date,
        "circuit_id": race.circuit_id,
        "circuit_name": race.circuit_name,
        "latitude": race.circuit_lat,
        "longitude": race.circuit_long,
        "conditions": describe(first("weather_code")),
        "temp_max": first("temperature_2m_max"),
        "temp_min": first("temperature_2m_min"),
        "temp_mean": first("temperature_2m_mean"),
        "precipitation_mm": precipitation,
        "rain_mm": first("rain_sum"),
        "wind_speed_max": first("wind_speed_10m_max"),
        "wind_gusts_max": first("wind_gusts_10m_max"),
        # Derived once, here, so every consumer agrees on what "wet" means.
        "was_wet": rainfall >= WET_RACE_MM,
        "wet_threshold_mm": WET_RACE_MM,
        "units": {
            "temperature": units.get("temperature_2m_max", "°C"),
            "precipitation": units.get("precipitation_sum", "mm"),
            "wind_speed": units.get("wind_speed_10m_max", "km/h"),
        },
        "source": "open-meteo-archive",
    }


def fetch_many(races, sleep: float = COURTESY_DELAY) -> tuple[list[dict], list[dict]]:
    """Fetch weather for many races. Returns (observations, skipped)."""
    cutoff = archive_cutoff()
    session = requests.Session()
    observations, skipped = [], []

    for i, race in enumerate(races, 1):
        if not race.race_date or race.race_date > cutoff.isoformat():
            skipped.append({
                "season": race.season, "round": race.round,
                "race_name": race.race_name, "race_date": race.race_date,
                "reason": f"after archive cutoff {cutoff.isoformat()} "
                          f"(ERA5 lags ~{ARCHIVE_LAG_DAYS} days)",
            })
            continue
        try:
            observation = fetch_race_weather(race, session=session)
            observations.append(observation)
            logger.info(
                "[%d/%d] %s %s — %s, %s°C, %s mm%s",
                i, len(races), race.season, race.race_name,
                observation["conditions"], observation["temp_max"],
                observation["precipitation_mm"],
                "  [WET]" if observation["was_wet"] else "",
            )
        except WeatherError as exc:
            skipped.append({"season": race.season, "round": race.round,
                            "race_name": race.race_name,
                            "race_date": race.race_date, "reason": str(exc)})
            logger.warning("[%d/%d] %s %s — SKIPPED: %s",
                           i, len(races), race.season, race.race_name, exc)
        time.sleep(sleep)

    return observations, skipped
