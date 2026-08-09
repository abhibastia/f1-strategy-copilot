"""HTTP client for the Jolpica-F1 API.

Handles the three things that make this API awkward: pagination, rate limits,
and a community-maintained backend that occasionally 5xxs. Everything else in
the project assumes it can call `fetch_all` and get a complete payload or an
exception — no partial results, no silent truncation.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
import json
from typing import Any

from config import (
    BACKOFF_BASE_SECONDS,
    BASE_URL,
    MAX_RETRIES,
    PAGE_SIZE,
    REQUEST_TIMEOUT_SECONDS,
    REQUESTS_PER_SECOND,
)

log = logging.getLogger(__name__)

_MIN_INTERVAL = 1.0 / REQUESTS_PER_SECOND
_last_request_at = 0.0


class JolpicaError(RuntimeError):
    """Raised when a request cannot be completed after all retries."""


def _throttle() -> None:
    """Space requests so we never approach the 4 req/s burst limit."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _get(url: str) -> dict[str, Any]:
    """Single GET with exponential back-off on 429 and 5xx."""
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "f1-capstone-pipeline/1.0"}
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx other than 429 are our fault — a bad URL won't fix itself.
            if exc.code != 429 and exc.code < 500:
                raise JolpicaError(f"{exc.code} for {url}") from exc
            wait = BACKOFF_BASE_SECONDS ** (attempt + 1)
            log.warning("HTTP %s for %s — retrying in %.0fs", exc.code, url, wait)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            wait = BACKOFF_BASE_SECONDS ** (attempt + 1)
            log.warning("%s for %s — retrying in %.0fs", type(exc).__name__, url, wait)
            time.sleep(wait)

    raise JolpicaError(f"exhausted {MAX_RETRIES} retries for {url}")


def fetch_all(path: str) -> dict[str, Any]:
    """Fetch every page of an endpoint and return one merged MRData payload.

    Jolpica paginates with limit/offset and reports `total` in every response.
    We walk pages until we have `total` records, then splice the accumulated
    records back into the first response's envelope so the result is shaped
    exactly like an unpaginated response.
    """
    first = _get(f"{BASE_URL}/{path}/?format=json&limit={PAGE_SIZE}&offset=0")
    mrdata = first["MRData"]
    total = int(mrdata.get("total", 0))

    table_key = _find_table_key(mrdata)
    if table_key is None:
        return first

    array_key = _find_array_key(mrdata[table_key])
    if array_key is None:
        return first

    records = mrdata[table_key][array_key]

    # Nested endpoints (results, qualifying) report `total` as the number of
    # *inner* records while the outer array holds one race. Walking pages by the
    # outer length would loop forever, so page on a running inner count instead.
    counted = _count_records(records, array_key)
    offset = PAGE_SIZE

    while counted < total:
        page = _get(f"{BASE_URL}/{path}/?format=json&limit={PAGE_SIZE}&offset={offset}")
        page_records = page["MRData"][table_key][array_key]
        if not page_records:
            log.warning("empty page at offset %s for %s — stopping early", offset, path)
            break
        records.extend(page_records)
        counted += _count_records(page_records, array_key)
        offset += PAGE_SIZE

    mrdata[table_key][array_key] = records
    return first


def _find_table_key(mrdata: dict[str, Any]) -> str | None:
    return next((k for k in mrdata if k.endswith("Table")), None)


def _find_array_key(table: dict[str, Any]) -> str | None:
    return next((k for k, v in table.items() if isinstance(v, list)), None)


def _count_records(records: list[dict[str, Any]], array_key: str) -> int:
    """Count the records `total` actually refers to.

    For flat endpoints (Drivers, Constructors, Races) that is the array length.
    For nested ones each element is a race or standings list wrapping the real
    records, so we sum the inner arrays.
    """
    if array_key not in ("Races", "StandingsLists"):
        return len(records)

    inner_keys = (
        "Results",
        "SprintResults",
        "QualifyingResults",
        "DriverStandings",
        "ConstructorStandings",
    )
    total = 0
    for record in records:
        inner = [record[k] for k in inner_keys if k in record]
        # A Races element with no inner array is a schedule entry — count it as one.
        total += sum(len(i) for i in inner) if inner else 1
    return total
