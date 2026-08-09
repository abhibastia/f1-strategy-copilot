# The Spark medallion pipeline

This is the data-engineering half of the project: the Spark pipeline that turns
raw Jolpica-F1 JSON into the governed Delta marts everything else reads.

It was built first, as a standalone data-engineering capstone, and is included
here in full because the AI capstone is built directly on top of it — the Gold
marts are seeded into Lakebase for the agent to serve, and `dim_race` supplies
the `wikipedia_url` and circuit coordinates that address the race-report and
weather harvests.

## Flow

```
Jolpica-F1 REST API
      ↓  ingestion/ — rate-limited, retried, idempotent
UC Volume  f1.raw.landing            raw JSON, partitioned by endpoint/season/round
      ↓  Auto Loader (cloudFiles)
Bronze  8 streaming tables           raw capture, warn-level expectations
      ↓
Silver  6 facts + 2 SCD-2 dims       flattened, typed, deduplicated, quarantined
      ↓
Gold    2 marts + DQ event log       business-ready, dims joined as-of race date
```

## Layout

| Path | What |
|---|---|
| `ingestion/` | Jolpica API client, landing writer, job entry point |
| `medallion/` | The Lakeflow pipeline: `01_bronze` → `04_gold` |
| `resources/` | Asset Bundle: pipeline, ingest job, dashboard |
| `sql/` | Correctness checks and data-quality metrics from the event log |
| `scripts/` | Catalog provisioning, landing upload, pipeline run/poll |
| `dashboards/` | AI/BI dashboard definition |

## Points worth a reviewer's attention

**Change data capture.** `medallion/03_silver_dims.py` uses
`dp.create_auto_cdc_flow(..., stored_as_scd_type=2)` to maintain SCD Type 2
driver and constructor dimensions, so a mid-season team change produces a new
version rather than overwriting history. The Gold marts join **as of the race
date**, which is the reason the SCD-2 dimension exists at all — a
current-row-only join would attribute every result to a driver's latest team.

**Sprint points are not optional.** Reconciling landed 2024 data showed that
summing race points alone leaves 13 of 24 drivers short of their official
season total. Adding the sprint endpoint brings mismatches to zero.

**Idempotent ingestion.** A round is *closed* once a later round exists and its
results payload is non-empty. Closed rounds are written once and skipped
thereafter, so a re-run makes ~8 API calls rather than ~260.

**Free Edition constraints discovered during the build.** Catalogs cannot be
created over the API — the catalog must be made once by hand in the UI. There is
a daily compute quota that applies to warehouse and pipeline compute alike, which
is why the API backfill runs locally and uploads via the Files API, and why
Bronze reads files as `text` so schema inference cannot fail a first run.

## Running it

```bash
# once, by hand — Free Edition cannot create catalogs over the API
#   Catalog → Create catalog → name `f1` → Default storage

./scripts/create_catalog.sh                       # schemas + landing volume
python3 ingestion/ingest.py --mode backfill --root ./landing
./scripts/upload_landing.sh                       # Files API, no compute

databricks bundle validate --strict -t dev --profile <profile>
databricks bundle deploy -t dev --profile <profile>
databricks bundle run f1_medallion_pipeline -t dev --profile <profile>
```
