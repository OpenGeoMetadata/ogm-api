from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import sync_ogm_search_index as sync


@pytest.mark.asyncio
async def test_sync_indexes_every_batch_deletes_unpublished_and_invalidates_search(monkeypatch):
    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    db.fetch_all = AsyncMock(
        side_effect=[
            [
                {"id": "a", "publication_state": "published"},
                {"id": "b", "publication_state": "draft"},
            ],
            [{"id": "c", "publication_state": "published"}],
            [],
        ]
    )
    es = MagicMock()
    es.indices.exists = AsyncMock(return_value=True)
    es.indices.refresh = AsyncMock()
    es.bulk = AsyncMock(return_value={"items": [{"delete": {"status": 200}}]})
    es.close = AsyncMock()
    prepare = AsyncMock(side_effect=[[("a", {})], [("c", {})]])
    index = AsyncMock(return_value={"indexed": 1})
    cache = MagicMock()
    cache.invalidate_tags = AsyncMock()
    monkeypatch.setattr(sync.bulk, "database", db)
    monkeypatch.setattr(sync.bulk, "es", es)
    monkeypatch.setattr(sync.bulk, "_prepare_documents_for_chunk", prepare)
    monkeypatch.setattr(sync.bulk, "_bulk_index_documents", index)
    monkeypatch.setattr(sync, "CacheService", lambda: cache)
    monkeypatch.setenv("ELASTICSEARCH_INDEX", "test-index")

    result = await sync.sync_search("repo", batch_size=2)

    assert result == {"processed": 3, "indexed": 2, "deleted": 1, "errors": 0}
    assert db.fetch_all.await_args_list[1].args[1] == {
        "limit": 2,
        "repo_name": "repo",
        "last_id": "b",
    }
    assert [call.args[0] for call in prepare.await_args_list] == [["a"], ["c"]]
    es.bulk.assert_awaited_once_with(
        operations=[{"delete": {"_index": "test-index", "_id": "b"}}], refresh=False
    )
    es.indices.refresh.assert_awaited_once_with(index="test-index")
    cache.invalidate_tags.assert_awaited_once_with({"search", "suggest"})
    db.disconnect.assert_awaited_once()
    es.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_refuses_to_report_success_when_documents_are_missing(monkeypatch):
    db = MagicMock(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        fetch_all=AsyncMock(
            side_effect=[
                [{"id": "a", "publication_state": "published"}],
                [],
            ]
        ),
    )
    es = MagicMock(close=AsyncMock())
    es.indices.exists = AsyncMock(return_value=True)
    es.indices.refresh = AsyncMock()
    monkeypatch.setattr(sync.bulk, "database", db)
    monkeypatch.setattr(sync.bulk, "es", es)
    monkeypatch.setattr(sync.bulk, "_prepare_documents_for_chunk", AsyncMock(return_value=[]))
    monkeypatch.setattr(sync.bulk, "_bulk_index_documents", AsyncMock(return_value={"indexed": 0}))
    cache = MagicMock(invalidate_tags=AsyncMock())
    monkeypatch.setattr(sync, "CacheService", lambda: cache)
    with pytest.raises(RuntimeError, match="incomplete"):
        await sync.sync_search("repo")
    cache.invalidate_tags.assert_awaited_once()
    es.close.assert_awaited_once()
