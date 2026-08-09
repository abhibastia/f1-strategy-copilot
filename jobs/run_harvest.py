"""Databricks task: harvest race reports, weather and pit stops into Lakebase.

Runs the SAME modules as the local CLI. The only difference is where the race
spine comes from: locally it reads the medallion project's landing directory,
and on Databricks it reads the Unity Catalog Volume the ingestion job writes.
The layouts are identical, so F1_LANDING_DIR is the whole adaptation.

__file__ is not bound under a serverless spark_python_task (the file is run
through exec), so the repo root is located without it.
"""
import datetime, json, logging, os, sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("harvest-job")

_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
ROOT = os.path.dirname(_here)
sys.path.insert(0, ROOT)

os.environ.setdefault("F1_LANDING_DIR", "/Volumes/f1/raw/landing")
OUT = os.environ.get("F1_DATA_DIR", "/tmp/f1data")
os.makedirs(OUT, exist_ok=True)

from harvest import races as R, weather as WX, wikipedia as WK, laps as LP
from f1lake import schema, load as L, load_strategy as LS

schema.ensure_schema()

all_races = R.load_races()
run = R.completed_races(all_races, datetime.date.today().isoformat())
log.info("races known=%d, already run=%d", len(all_races), len(run))
json.dump([r.to_dict() for r in all_races], open(f"{OUT}/races.json", "w"))

reports, wiki_fail = WK.fetch_many(run)
json.dump(reports, open(f"{OUT}/race_reports.json", "w"))
log.info("race reports: %d ok, %d failed", len(reports), len(wiki_fail))

obs, skipped = WX.fetch_many(run)
json.dump(obs, open(f"{OUT}/race_weather.json", "w"))
log.info("weather: %d observations, %d skipped", len(obs), len(skipped))

log.info("pit stops: %s", LP.harvest(run, "pitstops", f"{OUT}/pitstops"))

log.info("loaded races=%d", L.load_races(f"{OUT}/races.json"))
log.info("loaded weather=%d", L.load_weather(f"{OUT}/race_weather.json"))
log.info("loaded documents=%s", L.load_documents(f"{OUT}/race_reports.json"))
log.info("loaded pit stops=%d", LS.load_pit_stops(OUT))
log.info("derived stints=%d", LS.build_stints())
