from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ogm_harvest import search_index


@pytest.mark.asyncio
async def test_sync_repo_search_uses_argument_list_and_reads_result(monkeypatch):
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(
        return_value=(b'log\nSEARCH_SYNC_RESULT {"indexed": 42}\n', None)
    )
    start = AsyncMock(return_value=process)
    monkeypatch.setattr(search_index.asyncio, "create_subprocess_exec", start)
    assert await search_index.sync_repo_search("repo with spaces") == {"indexed": 42}
    assert start.await_args.args[-2:] == ("--repo", "repo with spaces")


@pytest.mark.asyncio
async def test_sync_repo_search_propagates_index_failure(monkeypatch):
    process = MagicMock(returncode=1)
    process.communicate = AsyncMock(return_value=(b"index failed", None))
    monkeypatch.setattr(
        search_index.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(RuntimeError, match="index failed"):
        await search_index.sync_repo_search("repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("index_fails", [False, True])
async def test_harvest_requires_search_sync_before_reporting_success(monkeypatch, index_fails):
    from types import SimpleNamespace

    from app.services.ogm_harvest import harvest

    repo = MagicMock()
    for name in (
        "ensure_repo",
        "mark_repo_harvest_started",
        "cancel_other_running_runs",
        "update_harvest_run",
        "finalize_harvest_run",
        "mark_repo_harvest_completed",
    ):
        setattr(repo, name, AsyncMock())
    repo.get_repo = AsyncMock(return_value={"ogm_enabled": True})
    repo.create_harvest_run = AsyncMock(return_value=1)
    importer = MagicMock(changed_thumbnail_resource_ids=set())
    importer.upsert_stream = AsyncMock(return_value={"imported": 1, "errors": 0})
    syncer = MagicMock()
    syncer.ensure_repo.return_value = SimpleNamespace(
        head_sha="abc", action="pull", repo_dir="/tmp"
    )
    dump = MagicMock()
    dump.finalize.return_value = SimpleNamespace(run_dir="/tmp/run")
    search = AsyncMock(return_value={"indexed": 1, "errors": 0})
    if index_fails:
        search.side_effect = RuntimeError("index unavailable")
    monkeypatch.setattr(harvest, "OGMHarvestRepository", lambda: repo)
    monkeypatch.setattr(harvest, "OGMResourceImporter", lambda **kw: importer)
    monkeypatch.setattr(harvest, "OGMRepoSync", lambda **kw: syncer)
    monkeypatch.setattr(harvest, "OGMHarvestDumpWriter", lambda **kw: dump)
    monkeypatch.setattr(harvest, "read_aardvark_records", lambda _: [])
    monkeypatch.setattr(harvest, "sync_repo_search", search)
    if index_fails:
        with pytest.raises(RuntimeError, match="index unavailable"):
            await harvest.harvest_repo("repo")
        assert repo.finalize_harvest_run.await_args.kwargs["ogm_status"] == "failed"
    else:
        result = await harvest.harvest_repo("repo")
        assert result["stats"]["search_index"] == {"indexed": 1, "errors": 0}
        assert repo.finalize_harvest_run.await_args.kwargs["ogm_status"] == "success"
    search.assert_awaited_once_with("repo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"ogm_enabled": False},
        {"ogm_enabled": True, "ogm_tags": {"ogm_archived": True}},
    ],
)
async def test_harvest_skips_disabled_or_archived_repo(monkeypatch, settings):
    from app.services.ogm_harvest import harvest

    repo = MagicMock()
    repo.ensure_repo = AsyncMock()
    repo.get_repo = AsyncMock(return_value=settings)
    syncer = MagicMock()
    monkeypatch.setattr(harvest, "OGMHarvestRepository", lambda: repo)
    monkeypatch.setattr(harvest, "OGMRepoSync", syncer)
    result = await harvest.harvest_repo("edu.umn", trigger="push")
    assert result["status"] == "skipped"
    syncer.assert_not_called()
    repo.upsert_repo.assert_not_called()
    repo.mark_repo_harvest_started.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_repo_does_not_overwrite_catalog_settings(monkeypatch):
    from sqlalchemy.dialects import postgresql

    from app.services.ogm_harvest import repository

    execute = AsyncMock()
    monkeypatch.setattr(repository.database, "execute", execute)
    await repository.OGMHarvestRepository().ensure_repo("geobtaa")
    statement = str(execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (ogm_repo_name) DO NOTHING" in statement
