"""Harvest the public UW–Madison BTAA API collection into OGM's existing source."""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from sqlalchemy import select

if __file__ != "<stdin>":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ogm_harvest.dumps import OGMHarvestDumpWriter  # noqa: E402
from app.services.ogm_harvest.importer import OGMResourceImporter  # noqa: E402
from app.services.ogm_harvest.repository import OGMHarvestRepository  # noqa: E402
from app.services.ogm_harvest.search_index import sync_repo_search  # noqa: E402
from app.services.thumbnail_refresh_service import (  # noqa: E402
    refresh_thumbnail_cache_for_changed_resources,
)
from db.database import database  # noqa: E402
from db.models import ogm_resource_state, resources  # noqa: E402

SOURCE = "btaa-api.uwmadison"
PROVIDER = "University of Wisconsin-Madison"
BASE = "https://geo.btaa.org/api/v1"
SCOPE = {"include_filters[schema_provider_s][]": PROVIDER}
SOURCE_URL = BASE + "/search?" + urlencode(SCOPE, quote_via=quote)
PAGE_SIZE = 100
RESULT_WINDOW = 10_000
FACETS = ("gbl_resourceClass_sm", "gbl_resourceType_sm", "gbl_indexYear_im", "dct_spatial_sm")


def retry_delay(header, attempt):
    if not header:
        return min(60, 2 ** (attempt + 1))
    if header.isdigit():
        seconds = int(header)
        # BTAA can return a Unix reset timestamp as well as a delay in seconds.
        return max(1, seconds - time.time() + 1 if seconds >= 1_000_000_000 else seconds)
    return max(1, parsedate_to_datetime(header).timestamp() - time.time())


def fetch(session, path, params):
    for attempt in range(7):
        try:
            response = session.get(BASE + path, params={**SCOPE, **params}, timeout=(15, 90))
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            response = getattr(exc, "response", None)
            if attempt == 6 or (
                response is not None and response.status_code not in (429, 500, 502, 503, 504)
            ):
                raise
            header = response.headers.get("Retry-After") if response is not None else None
            delay = retry_delay(header, attempt)
            print(f"Retrying BTAA request in {delay:.0f}s", flush=True)
            time.sleep(delay)


def public_record(item):
    attributes = item["attributes"]
    record = dict(attributes["ogm"])
    record.pop("b1g_adminTags_sm", None)
    for key, value in attributes.get("b1g", {}).items():
        if (key.startswith("b1g_") and key != "b1g_adminTags_sm") or key in (
            "date_created_dtsi",
            "date_modified_dtsi",
            "publication_state",
        ):
            record[key] = value
    if (
        not item["id"]
        or record["id"] != item["id"]
        or record["schema_provider_s"] != PROVIDER
        or record["gbl_mdVersion_s"].lower() != "aardvark"
        or any(
            (record.get(k) or "published").lower() != "published"
            for k in ("b1g_publication_state_s", "publication_state")
        )
    ):
        raise ValueError(f"Unexpected record in Madison collection: {item['id']}")
    if not record.get("gbl_mdModified_dt") and record.get("date_modified_dtsi"):
        date = datetime.fromisoformat(record["date_modified_dtsi"])
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        record["gbl_mdModified_dt"] = date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not all(
        record.get(k)
        for k in ("dct_title_s", "dct_accessRights_s", "gbl_resourceClass_sm", "gbl_mdModified_dt")
    ):
        raise ValueError(f"Incomplete Aardvark metadata for {record['id']}")
    return record


def collect(get):
    """Split large result sets by membership in a facet, including multivalued facets."""
    records = {}

    def count(filters):
        return get("/search", {**filters, "per_page": 1, "meta": "false"})["meta"]["totalCount"]

    def visit(filters, expected, facets):
        if expected > RESULT_WINDOW:
            for index, field in enumerate(facets):
                buckets = get("/search/facets/" + field, filters)["data"]
                choices = [
                    b["attributes"] for b in buckets if 0 < b["attributes"]["hits"] < expected
                ]
                if choices:
                    pivot = min(choices, key=lambda b: abs(expected / 2 - b["hits"]))
                    for mode, size in (
                        ("include", pivot["hits"]),
                        ("exclude", expected - pivot["hits"]),
                    ):
                        subset = {**filters, f"{mode}_filters[{field}][]": pivot["value"]}
                        visit(subset, size, facets[index + 1 :])
                    return
            raise ValueError(f"No facet split for {expected} records within BTAA's result window")
        for offset in range(0, expected, PAGE_SIZE):
            page = get(
                "/search",
                {
                    **filters,
                    "page": offset // PAGE_SIZE + 1,
                    "per_page": PAGE_SIZE,
                    "meta": "false",
                },
            )
            if page["meta"]["totalCount"] != expected or len(page["data"]) != min(
                PAGE_SIZE, expected - offset
            ):
                raise ValueError(
                    "BTAA collection changed during pagination or returned a short page"
                )
            for item in page["data"]:
                record = public_record(item)
                if record["id"] in records:
                    raise ValueError(f"Duplicate BTAA record {record['id']}")
                records[record["id"]] = record
        print(f"Collected {len(records)} Madison records", flush=True)

    total = count({})
    if total <= 0:
        raise ValueError("Empty UW–Madison collection")
    visit({}, total, FACETS)
    if len(records) != total or count({}) != total:
        raise ValueError("Incomplete UW–Madison collection")
    return records


async def owned_ids(ids):
    existing = set()
    for start in range(0, len(ids), 1000):
        batch = ids[start : start + 1000]
        rows = await database.fetch_all(
            select(resources.c.id, resources.c.b1g_adminTags_sm).where(resources.c.id.in_(batch))
        )
        for row in rows:
            if "ogm_repo:" + SOURCE not in (row["b1g_adminTags_sm"] or []):
                raise ValueError(f"Madison ID belongs to another source: {row['id']}")
            existing.add(row["id"])
        foreign = await database.fetch_one(
            select(ogm_resource_state.c.ogm_resource_id)
            .where(
                ogm_resource_state.c.ogm_resource_id.in_(batch),
                ogm_resource_state.c.ogm_repo_name != SOURCE,
            )
            .limit(1)
        )
        if foreign:
            raise ValueError(f"Madison ID tracked by another source: {foreign['ogm_resource_id']}")
    return existing


def snapshot_diff(records, previous_dump):
    changed = set(records)
    missing = 0
    if previous_dump:
        snapshot = Path(previous_dump) / "dataset.ndjson"
        if snapshot.is_file():
            with snapshot.open(encoding="utf-8") as stream:
                for line in stream:
                    old = json.loads(line)
                    if old["id"] not in records:
                        missing += 1
                    elif records[old["id"]] == old:
                        changed.remove(old["id"])
    return changed, missing


async def harvest():
    repo = OGMHarvestRepository()
    configured = await repo.get_repo(SOURCE)
    if configured and (configured.get("ogm_tags") or {}).get("source_url") != SOURCE_URL:
        raise ValueError(f"{SOURCE} is registered for a different feed")
    if not configured:
        await repo.upsert_repo(
            SOURCE,
            ogm_enabled=False,
            ogm_watch_mode="manual",
            ogm_notes="Public UW–Madison API feed harvested by Nightly OGM Sync.",
            ogm_tags={
                "source_type": "api",
                "source_url": SOURCE_URL,
                "provider": PROVIDER,
                "ogm_has_aardvark": True,
            },
        )
    previous = await repo.list_harvest_runs(SOURCE, ogm_status="success", limit=1)
    dump = previous[0]["ogm_dump_dir"] if previous else None
    run_id = await repo.create_harvest_run(SOURCE, "nightly")
    stats = {"source": SOURCE_URL, "stage": "collect"}
    try:
        await repo.mark_repo_harvest_started(SOURCE)
        await repo.cancel_other_running_runs(
            SOURCE, keep_ogm_id=run_id, reason=f"superseded by run {run_id}"
        )
        with requests.Session() as session:
            session.headers["User-Agent"] = "OpenGeoMetadata UW-Madison nightly harvest"
            records = collect(lambda path, params: fetch(session, path, params))
        changed, missing = snapshot_diff(records, dump)
        ids = list(records)
        importer = OGMResourceImporter(repo=repo)
        stats.update(stage="import", source_count=len(ids), absent_from_feed=missing)
        async with database.transaction():
            existing = await owned_ids(ids)
            changed.update(records.keys() - existing)
            stats.update(added=len(ids) - len(existing), updated=len(changed & existing))
            # Import the complete snapshot so the importer's last-seen tracking remains accurate.
            stream = (
                (row, f"{BASE}/resources/{quote(rid, safe='')}/metadata/ogm")
                for rid, row in records.items()
            )
            stats.update(
                await importer.upsert_stream(
                    repo_name=SOURCE, record_stream=stream, batch_size=200, ogm_run_id=run_id
                )
            )
            if stats["skipped"] or stats["errors"] or stats["imported"] != len(ids):
                raise RuntimeError(f"Incomplete Madison import: {stats}")
        stats["stage"] = "search_index"
        await repo.update_harvest_run(ogm_id=run_id, ogm_stats_json=stats)
        stats["search_index"] = await sync_repo_search(SOURCE)
        if stats["search_index"]["errors"] or stats["search_index"]["indexed"] < len(ids):
            raise RuntimeError(f"Incomplete Madison indexing: {stats['search_index']}")
        refresh = changed | importer.changed_thumbnail_resource_ids
        if refresh:
            stats["stage"] = "refresh"
            stats["cache_refresh"] = await refresh_thumbnail_cache_for_changed_resources(
                sorted(refresh)
            )
        stats["stage"] = "complete"
        if changed or missing:
            writer = OGMHarvestDumpWriter(SOURCE, run_id)
            for row in records.values():
                writer.write_record(row)
            dump = str(
                writer.finalize(repo_name=SOURCE, run_id=run_id, head_sha=None, stats=stats).run_dir
            )
        async with database.transaction():
            await repo.finalize_harvest_run(run_id, "success", stats, ogm_dump_dir=dump)
            await repo.mark_repo_harvest_completed(SOURCE, "success")
        print(json.dumps({"run": run_id, **stats}), flush=True)
    except Exception as exc:
        await repo.finalize_harvest_run(run_id, "failed", stats, ogm_error=str(exc)[:1500])
        await repo.mark_repo_harvest_completed(SOURCE, "failed")
        raise


async def main():
    async with database:
        await harvest()


if __name__ == "__main__":
    asyncio.run(main())
