"""Read repository membership directly from the live search index."""

import os

from app.elasticsearch.client import es


async def get_index_status() -> dict:
    counts = {}
    after = None
    total = 0
    while True:
        composite = {
            "size": 100,
            "sources": [{"repo": {"terms": {"field": "ogm_repo.keyword"}}}],
        }
        if after is not None:
            composite["after"] = after
        response = await es.search(
            index=os.getenv("ELASTICSEARCH_INDEX", "opengeometadata_api"),
            size=0,
            track_total_hits=True,
            allow_partial_search_results=False,
            aggregations={"repos": {"composite": composite}},
        )
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise RuntimeError("Search index returned incomplete repository counts")
        total = response["hits"]["total"]["value"]
        page = response["aggregations"]["repos"]
        for bucket in page["buckets"]:
            counts[bucket["key"]["repo"]] = bucket["doc_count"]
        after = page.get("after_key")
        if not page["buckets"] or after is None:
            break
    return {"record_count": total, "repo_counts": counts}
