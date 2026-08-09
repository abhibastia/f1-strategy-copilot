"""Entry point for F1 raw ingestion.

Runs identically as a Databricks Job (writing straight to the UC Volume) and on
a laptop (writing to a local staging directory that is then uploaded). Modes:

  backfill    — every configured season, including the live one
  incremental — the live season only; what the scheduled Job runs weekly

Usage:
    python ingest.py --mode backfill --root /Volumes/f1/raw/landing
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass, field

# A Databricks job does not guarantee the task file's directory is on sys.path,
# so the sibling imports below would fail. Add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    ALL_ENDPOINTS,
    BACKFILL_SEASONS,
    BASE_URL,
    LIVE_SEASON,
    ROUND_ENDPOINTS,
    SEASON_ENDPOINTS,
    VOLUME_ROOT,
)
from jolpica_client import JolpicaError, fetch_all
from landing_writer import should_write, write_payload

log = logging.getLogger("ingest")


@dataclass
class RunSummary:
    files_written: int = 0
    partitions_skipped: int = 0
    requests_made: int = 0
    failures: list[str] = field(default_factory=list)

    def report(self) -> None:
        log.info("─" * 60)
        log.info("files written      : %s", self.files_written)
        log.info("partitions skipped : %s", self.partitions_skipped)
        log.info("API requests       : %s", self.requests_made)
        log.info("failures           : %s", len(self.failures))
        for failure in self.failures:
            log.error("  %s", failure)
        log.info("─" * 60)


def parse_race_calendar(races_payload: dict) -> dict[int, dt.date]:
    """Map round number → race date from a season's race list."""
    races = races_payload["MRData"]["RaceTable"]["Races"]
    calendar: dict[int, dt.date] = {}
    for race in races:
        try:
            calendar[int(race["round"])] = dt.date.fromisoformat(race["date"])
        except (KeyError, ValueError):
            log.warning("unparseable race entry: %s", race.get("round"))
    return calendar


def ingest_season(season: int, root: str, summary: RunSummary) -> None:
    log.info("=== season %s ===", season)
    today = dt.date.today()

    # The race calendar drives everything: which rounds exist, and whether each
    # one is closed. Always fetched first, and always re-fetched for the live
    # season because the calendar itself can shift.
    try:
        races = fetch_all(SEASON_ENDPOINTS["races"].format(season=season))
        summary.requests_made += 1
    except JolpicaError as exc:
        summary.failures.append(f"season {season} calendar: {exc}")
        return

    calendar = parse_race_calendar(races)
    if not calendar:
        log.warning("season %s has no races — skipping", season)
        return

    # Season-level endpoints. Treated as closed once the season's last race is
    # well past, using that date as the season's own "race date".
    last_race_date = max(calendar.values())
    for endpoint, template in SEASON_ENDPOINTS.items():
        payload = races if endpoint == "races" else None
        if not should_write(root, endpoint, season, None, last_race_date):
            summary.partitions_skipped += 1
            continue
        try:
            if payload is None:
                payload = fetch_all(template.format(season=season))
                summary.requests_made += 1
            write_payload(
                root, endpoint, season, None, payload,
                f"{BASE_URL}/{template.format(season=season)}/",
            )
            summary.files_written += 1
        except JolpicaError as exc:
            summary.failures.append(f"{endpoint} {season}: {exc}")

    # Round-level endpoints. Future rounds have no data yet — pulling them just
    # burns rate limit and lands empty payloads.
    for round_no in sorted(calendar):
        race_date = calendar[round_no]
        if race_date > today:
            continue

        for endpoint, template in ROUND_ENDPOINTS.items():
            if not should_write(root, endpoint, season, round_no, race_date):
                summary.partitions_skipped += 1
                continue
            path = template.format(season=season, round=round_no)
            try:
                payload = fetch_all(path)
                summary.requests_made += 1
                write_payload(
                    root, endpoint, season, round_no, payload, f"{BASE_URL}/{path}/"
                )
                summary.files_written += 1
            except JolpicaError as exc:
                summary.failures.append(f"{endpoint} {season} r{round_no}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Jolpica F1 data")
    parser.add_argument("--mode", choices=["backfill", "incremental"], default="incremental")
    parser.add_argument("--root", default=VOLUME_ROOT, help="landing zone root path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    seasons = (
        [*BACKFILL_SEASONS, LIVE_SEASON] if args.mode == "backfill" else [LIVE_SEASON]
    )
    log.info("mode=%s seasons=%s root=%s", args.mode, seasons, args.root)
    log.info("endpoints: %s", ", ".join(sorted(ALL_ENDPOINTS)))

    summary = RunSummary()
    for season in seasons:
        ingest_season(season, args.root, summary)
    summary.report()

    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
