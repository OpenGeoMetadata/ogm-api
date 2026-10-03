from unittest.mock import AsyncMock, patch

import pytest

from app.services.ogm_harvest import index_status


@pytest.mark.asyncio
async def test_index_counts_follow_all_composite_pages_and_exact_total():
    def page(buckets, after=None):
        return {
            "hits": {"total": {"value": 30}},
            "aggregations": {
                "repos": {
                    "buckets": [
                        {"key": {"repo": name}, "doc_count": count} for name, count in buckets
                    ],
                    "after_key": after,
                }
            },
        }

    search = AsyncMock(side_effect=[page([("a", 10)], {"repo": "a"}), page([("b", 12)])])
    with patch.object(index_status.es, "search", search):
        result = await index_status.get_index_status()
    assert result == {"record_count": 30, "repo_counts": {"a": 10, "b": 12}}
    assert search.call_args_list[0].kwargs["track_total_hits"] is True
    assert search.call_args_list[0].kwargs["allow_partial_search_results"] is False
    assert search.call_args_list[1].kwargs["aggregations"]["repos"]["composite"]["after"] == {
        "repo": "a"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"timed_out": True}, {"_shards": {"failed": 1}}])
async def test_partial_counts_are_rejected(response):
    with patch.object(index_status.es, "search", AsyncMock(return_value=response)):
        with pytest.raises(RuntimeError, match="incomplete"):
            await index_status.get_index_status()
