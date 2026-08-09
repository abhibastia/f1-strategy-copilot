"""Harvest race reports and race-day weather to local JSON. No Databricks compute."""

import argparse, datetime, json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvest import races as races_mod, weather as weather_mod, wikipedia as wiki_mod

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("harvest")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="data", help="output directory")
    p.add_argument("--limit", type=int, help="only harvest the first N races (smoke test)")
    p.add_argument("--skip-wikipedia", action="store_true")
    p.add_argument("--skip-weather", action="store_true")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    all_races = races_mod.load_races()
    today = datetime.date.today().isoformat()
    run = races_mod.completed_races(all_races, today)
    if args.limit:
        run = run[:args.limit]

    log.info("Races known: %d  |  already run: %d  |  harvesting: %d",
             len(all_races), len(races_mod.completed_races(all_races, today)), len(run))
    json.dump([r.to_dict() for r in all_races],
              open(os.path.join(args.out, "races.json"), "w"), indent=2)

    if not args.skip_wikipedia:
        log.info("\n--- Wikipedia race reports ---")
        # Resume: keep what a previous run already fetched. Wikipedia throttles,
        # so a partial run is normal and re-requesting successful articles is
        # both wasteful and the fastest way to get throttled again.
        out_path = os.path.join(args.out, "race_reports.json")
        existing = json.load(open(out_path)) if os.path.exists(out_path) else []
        have = {(r["season"], r["round"]) for r in existing}
        todo = [r for r in run if (r.season, r.round) not in have]
        log.info("already have %d, fetching %d", len(existing), len(todo))

        fetched, failures = wiki_mod.fetch_many(todo)
        reports = existing + fetched
        reports.sort(key=lambda r: (r["season"], r["round"]))
        json.dump(reports, open(os.path.join(args.out, "race_reports.json"), "w"), indent=2)
        json.dump(failures, open(os.path.join(args.out, "race_reports_failures.json"), "w"), indent=2)
        log.info("reports=%d failures=%d sections=%d chars=%d",
                 len(reports), len(failures),
                 sum(len(r["sections"]) for r in reports),
                 sum(r["total_chars"] for r in reports))

    if not args.skip_weather:
        log.info("\n--- Race-day weather ---")
        obs, skipped = weather_mod.fetch_many(run)
        json.dump(obs, open(os.path.join(args.out, "race_weather.json"), "w"), indent=2)
        json.dump(skipped, open(os.path.join(args.out, "race_weather_skipped.json"), "w"), indent=2)
        wet = sum(1 for o in obs if o["was_wet"])
        log.info("observations=%d skipped=%d wet_races=%d", len(obs), len(skipped), wet)


if __name__ == "__main__":
    main()
