import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import sync_uwmadison as sync


def item(rid):
    return {
        "id": rid,
        "attributes": {
            "ogm": {
                "id": rid,
                "schema_provider_s": sync.PROVIDER,
                "gbl_mdVersion_s": "Aardvark",
                "dct_title_s": f"Map {rid}",
                "gbl_resourceClass_sm": ["Maps"],
                "dct_accessRights_s": "Public",
            },
            "b1g": {
                "date_modified_dtsi": "2026-11-19T22:00:00-06:00",
                "b1g_adminTags_sm": ["upstream-only"],
            },
        },
    }


def test_collect_partitions_and_paginates(monkeypatch):
    monkeypatch.setattr(sync, "RESULT_WINDOW", 2)
    monkeypatch.setattr(sync, "PAGE_SIZE", 1)

    def get(path, params):
        if path.startswith("/search/facets"):
            return {"data": [{"attributes": {"value": "Maps", "hits": 2}}]}
        ids = ["a", "b", "c"]
        if "include_filters[gbl_resourceClass_sm][]" in params:
            ids = ids[:2]
        if "exclude_filters[gbl_resourceClass_sm][]" in params:
            ids = ids[2:]
        return {"meta": {"totalCount": len(ids)}, "data": [item(ids[params.get("page", 1) - 1])]}

    rows = sync.collect(get)
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"]["gbl_mdModified_dt"] == "2026-11-20T04:00:00Z"
    assert "b1g_adminTags_sm" not in rows["b"]


@pytest.mark.parametrize("condition", ["duplicate", "short", "scope", "unpublished", "count"])
def test_reject_inconsistent_feed(condition):
    rows = [item("a"), item("b")]
    if condition == "duplicate":
        rows[1] = rows[0]
    if condition == "short":
        rows.pop()
    if condition == "scope":
        rows[0]["attributes"]["ogm"]["schema_provider_s"] = "Another institution"
    if condition == "unpublished":
        rows[1]["attributes"]["b1g"]["publication_state"] = "draft"
    count = 2

    def get(path, params):
        nonlocal count
        response = {"meta": {"totalCount": count}, "data": rows}
        if condition == "count" and "page" in params:
            count += 1
        return response

    with pytest.raises(ValueError):
        sync.collect(get)


@pytest.mark.asyncio
async def test_added_and_edited_records_are_indexed_and_cached(monkeypatch, tmp_path):
    old = [sync.public_record(item(rid)) for rid in ("edited", "same", "absent")]
    (tmp_path / "dataset.ndjson").write_text("\n".join(json.dumps(row) for row in old))
    rows = {rid: sync.public_record(item(rid)) for rid in ("edited", "same", "new")}
    rows["edited"]["dct_title_s"] = "Revised map title"
    repo = AsyncMock()
    repo.get_repo.return_value = {"ogm_tags": {"source_url": sync.SOURCE_URL}}
    repo.list_harvest_runs.return_value = [{"ogm_dump_dir": str(tmp_path)}]
    repo.create_harvest_run.return_value = 1
    importer = MagicMock(changed_thumbnail_resource_ids=set())

    async def upsert(**kwargs):
        assert kwargs["repo_name"] == sync.SOURCE
        imported = {row["id"]: row for row, _ in kwargs["record_stream"]}
        assert imported == rows
        return {"imported": len(imported), "errors": 0, "skipped": 0}

    importer.upsert_stream = upsert
    index = AsyncMock(return_value={"errors": 0, "indexed": 3})
    refresh = AsyncMock(return_value={})
    monkeypatch.setattr(sync, "database", MagicMock())
    monkeypatch.setattr(sync, "OGMHarvestRepository", lambda: repo)
    monkeypatch.setattr(sync, "OGMResourceImporter", lambda repo: importer)
    monkeypatch.setattr(sync, "collect", lambda get: rows)
    monkeypatch.setattr(sync, "owned_ids", AsyncMock(return_value={"same", "edited"}))
    monkeypatch.setattr(sync, "sync_repo_search", index)
    monkeypatch.setattr(sync, "refresh_thumbnail_cache_for_changed_resources", refresh)
    monkeypatch.setenv("OGM_DUMP_BASE_DIR", str(tmp_path))

    await sync.harvest()

    index.assert_awaited_once_with(sync.SOURCE)
    refresh.assert_awaited_once_with(["edited", "new"])
    _, status, stats = repo.finalize_harvest_run.await_args.args
    assert status == "success"
    assert (stats["added"], stats["updated"], stats["absent_from_feed"]) == (1, 1, 1)
