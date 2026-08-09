"""Shared configuration for F1 ingestion.

Every name that appears in more than one place lives here. The catalog is the one
value that may change (Free Edition may force a fallback catalog), so nothing
downstream hard-codes it.
"""

CATALOG = "f1"
RAW_SCHEMA = "raw"
VOLUME = "landing"

VOLUME_ROOT = f"/Volumes/{CATALOG}/{RAW_SCHEMA}/{VOLUME}"

BASE_URL = "https://api.jolpi.ca/ergast/f1"

# Backfill seasons are complete and immutable; the live season is re-polled.
BACKFILL_SEASONS = [2024, 2025]
LIVE_SEASON = 2026

# Jolpica limits: 4 req/s burst, 500 req/hr sustained. We stay well inside both.
REQUESTS_PER_SECOND = 2.0
PAGE_SIZE = 100
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30

# Season-level endpoints: one call per season, no round segment.
# `sprint` is season-level on purpose: the per-round form would need a call for
# every round to find the ~6 that have a sprint, while the season form returns
# them all in two pages. Sprint points count toward the championship, so without
# this endpoint the marts understate points on every sprint weekend.
SEASON_ENDPOINTS = {
    "races": "{season}/races",
    "drivers": "{season}/drivers",
    "constructors": "{season}/constructors",
    "sprint": "{season}/sprint",
}

# Round-level endpoints: one call per (season, round).
ROUND_ENDPOINTS = {
    "results": "{season}/{round}/results",
    "qualifying": "{season}/{round}/qualifying",
    "driver_standings": "{season}/{round}/driverStandings",
    "constructor_standings": "{season}/{round}/constructorStandings",
}

ALL_ENDPOINTS = {**SEASON_ENDPOINTS, **ROUND_ENDPOINTS}

# The MRData sub-table each endpoint's records live under, and the array key
# holding the records. Used by the Silver layer; kept here so the contract is
# declared in exactly one place.
ENDPOINT_SHAPE = {
    "races": ("RaceTable", "Races"),
    "drivers": ("DriverTable", "Drivers"),
    "constructors": ("ConstructorTable", "Constructors"),
    "sprint": ("RaceTable", "Races"),
    "results": ("RaceTable", "Races"),
    "qualifying": ("RaceTable", "Races"),
    "driver_standings": ("StandingsTable", "StandingsLists"),
    "constructor_standings": ("StandingsTable", "StandingsLists"),
}