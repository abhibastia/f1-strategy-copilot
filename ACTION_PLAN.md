# AI Formula 1 Race Companion — Action Plan

**Deadline:** 2026-08-10 08:00 CEST · **Written:** 2026-08-09 03:20 CEST
**Platform:** Databricks Free Edition (serverless only)
**Companion project:** `formula1-capstone-project` — the data-engineering capstone whose
medallion pipeline this builds on. That repo stays untouched.

---

## 1. What this is

An AI companion over Formula 1. A user tracks drivers and constructors, asks questions in
plain language — *"how wet was the 2024 Brazilian Grand Prix?"*, *"compare Ferrari and
McLaren's 2025 form"*, *"which races were decided by rain?"* — and an agent answers from
real race data, real race-day weather, and the narrative of the race reports. It can also
**write**: save a prediction, add a driver to a watchlist, record a note against a race.

## 2. Capstone requirements → where each is satisfied

The capstone README mandates exactly five things. There is **no Change Data Feed
requirement** — `CDF` and `Change Data Feed` appear nowhere in it. Anything suggesting a
sixth CDF requirement is wrong, and building one would be unpaid scope.

| # | Requirement | Satisfied by | State |
|---|---|---|---|
| 1 | A data pipeline in Spark | `formula1-capstone-project` medallion: Bronze → Silver → Gold, SCD-2 dims, expectations | ✅ built, populated (1200 / 1251 rows in Gold marts) |
| 2 | Integration with ≥1 third-party API | Jolpica-F1 (races, results, standings) · **Open-Meteo archive** (race-day weather) · **Wikimedia** (race reports) | 1 done, 2 to add |
| 3 | Processing of unstructured data | Wikipedia race reports → chunked → embedded → Lakebase `pgvector`, semantic search | to build |
| 4 | A Databricks App with a frontend | Flask app: chat, watchlist, predictions, notes | to build |
| 5 | AI agent with read **and write** tools | FastMCP server registered in Unity AI Gateway | to build |

Three third-party APIs, not one. The weather API is what turns "the race report says it
rained" into "12.4 mm fell at Interlagos that day" — a claim grounded in measurement.

## 3. Decisions locked before any code

Each of these is expensive to reverse. They are chosen from evidence, not preference.

### 3.1 Vector store: Lakebase `pgvector`, not Databricks Vector Search

Free Edition allows **one** AI Search endpoint with one search unit, and Direct Vector
Access is unsupported. Against that, `vector-weather-retrieval-service` already proves an
end-to-end pgvector path on this exact Lakebase instance: `VECTOR(384)`, HNSW with
`vector_cosine_ops`, `%s::vector` binding. Reuse beats novelty with 29 hours on the clock.

### 3.2 Embeddings: `all-MiniLM-L6-v2` (384-dim), computed **locally**

Day 2 established that a Free Edition serverless notebook is memory-killed while loading
`sentence-transformers`/`torch` — the process dies before embedding starts. Embedding
therefore runs on the laptop and writes to Lakebase over the network, exactly as
`ingest.py` does today. **This spends zero Databricks compute**, which is the scarcest
resource in this build.

### 3.3 Corpus: Wikipedia race reports, addressed from `dim_race.wikipedia_url`

`f1.silver.dim_race` already carries `wikipedia_url` for **71 of 71 races**. No search, no
title construction, no disambiguation — the corpus is pre-addressed by the pipeline. Fetch
plain-text extracts via the MediaWiki API (`prop=extracts&explaintext`), free and keyless.

### 3.4 Weather: Open-Meteo **archive** (ERA5), keyed on `circuit_lat/long + race_date`

`dim_race` carries `circuit_lat` and `circuit_long` for 71 of 71 races, so no geocoding is
needed. 59 races are more than five days old and therefore archive-eligible; the remaining
12 are future 2026 rounds with no observations to fetch. `weather-mcp-agent`'s broker
already implements this call and its date guards.

### 3.5 Agent: FastMCP over streamable HTTP, deployed as a Databricks App

`weather-mcp-agent` scored 100/100 with this exact shape, including the non-obvious part:
Databricks appends `/mcp` to an endpoint that already ends in `/mcp`, so the server must
answer on **both** `/mcp` and `/mcp/mcp` or tool listing fails with
`Unrecognized token 'Not' ... Response: Not Found`.

### 3.6 Scope explicitly **out**

FIA decision PDFs, press-conference transcripts, OpenF1 team-radio audio, telemetry, lap
and pit-stop data, Change Data Feed. Each is defensible; none is required; all cost hours
this build does not have.

---

## 4. Data model

### 4.1 Lakebase (operational — the agent's read/write surface)

```
f1_documents      race-report text, one row per (season, round, section)
f1_embeddings     chunk-level, VECTOR(384), HNSW vector_cosine_ops
f1_race_weather   measured race-day weather, one row per (season, round)
f1_watchlist      user's tracked drivers/constructors      ← agent WRITE
f1_predictions    pre-race predictions, resolvable later   ← agent WRITE
f1_race_notes     free-text analyst notes tied to a race   ← agent WRITE
```

Every table carries `season` / `round` so retrieval joins straight to the Gold marts. That
shared key is what makes the structured and unstructured tracks one queryable thing rather
than two disconnected stores.

### 4.2 Delta (analytical — existing, unchanged)

`f1.gold.driver_performance`, `f1.gold.championship_progression`, `f1.silver.dim_race`,
`f1.silver.fact_result`, `f1.silver.dim_driver`.

---

## 5. Agent tools

**Read**

| Tool | Backed by |
|---|---|
| `get_driver_season(driver, season)` | Gold `driver_performance` |
| `compare_constructors(a, b, season)` | Gold `championship_progression` |
| `search_race_reports(query, season?, round?)` | Lakebase pgvector, cosine `<=>` |
| `get_race_weather(season, round)` | Lakebase `f1_race_weather` |
| `get_championship_standings(season, round?)` | Gold |

**Write** — the requirement most submissions miss

| Tool | Effect |
|---|---|
| `add_to_watchlist(entity_type, entity_ref)` | upsert into `f1_watchlist` |
| `log_prediction(season, round, prediction, confidence)` | insert into `f1_predictions` |
| `save_race_note(season, round, note)` | insert into `f1_race_notes` |

Every write returns what it wrote, so the agent can confirm rather than assert. Every read
returns the identifiers it resolved, so a wrong match is visible in the answer — the
`resolved_location` lesson from day 3, applied to drivers and races.

---

## 6. Build order

Sequenced so that **everything requiring Databricks compute happens last**. The daily quota
is unrecoverable until the next day, and Phases 1–5 need none of it.

| Phase | Work | Compute | Acceptance |
|---|---|---|---|
| **0** | Verify identity. Delete `massive-lakebase-sync` and `weather-mcp-dashboard` to free 2 app slots. Confirm Lakebase reachable. | none | 2 free slots; Lakebase answers |
| **1** | Harvest: 71 Wikipedia race reports + 59 weather observations → local JSON | none | 71 reports, 59 weather rows on disk |
| **2** | Lakebase schema + load: 6 tables, HNSW index, chunk + embed | none | `SELECT count(*)` non-zero on all; HNSW present |
| **3** | MCP server: 8 tools (5 read, 3 write), local test through a real MCP client | none | all 8 callable; writes visible in Lakebase |
| **4** | Deploy MCP server as App #1; register in Unity AI Gateway; wire agent in Playground | app only | `/mcp` 200; tools listed; agent answers |
| **5** | Flask frontend as App #2: chat, watchlist, predictions, notes | app only | renders live Lakebase data |
| **6** | Live in-platform Jolpica call → current standings into Gold | **pipeline** | one successful run |
| **7** | README, RESULTS.md with agent transcripts, screenshots, zip | none | submission built |

**Phase 6 is the only optional phase.** Requirement 2 is already met by the committed,
reproducible Jolpica ingestion; Phase 6 makes the API call visible *inside* the platform.
If quota is exhausted, skip it and say so plainly in the README — day 2 proved that
documenting a platform limit honestly scores better than hiding it.

---

## 7. Known risks

| Risk | Mitigation |
|---|---|
| Daily compute quota exhausted | Phases 1–5 need none. Phase 6 is optional and last. |
| App creation fails (four failures on 2026-08-08) | Create shells from the **Agents → MCP server template** in the UI, which is what worked. Never delete a healthy app shell. |
| Apps auto-stop after 24h | Restart both immediately before recording the demo and before submitting. |
| Outbound egress blocked pre-verification | All harvesting runs locally. Verification is Phase 0 belt-and-braces. |
| Wikipedia/Open-Meteo rate limits | 71 and 59 calls respectively, sequential, with a courtesy delay. Trivial volume. |
| Time | Phases 1–5 deliver all five requirements. Phases 6–7 are polish and packaging. |

---

## 8. Definition of done

- [ ] All five capstone requirements demonstrably satisfied, each traceable to a file
- [ ] Agent performs at least one **write** in a captured transcript
- [ ] ≥5 agent transcripts: reasoning, tool call, raw output, final answer
- [ ] Both apps deployed, running, and reachable
- [ ] No credential in the repo or in git history
- [ ] README with architecture diagram, setup, APIs and auth, limitations
- [ ] Repository URL and both App URLs stated in the README
- [ ] Submission zip: code + docs + screenshots
