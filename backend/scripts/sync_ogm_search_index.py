#!/usr/bin/env python3
"""Synchronize harvested database records into the live index without replacing it.

Run in a separate process: the bulk preparation helper temporarily substitutes
prefetched enrichment lookups, so it must not share an event loop with API work.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.cache_service import CacheService  # noqa: E402
from scripts import reindex_atomic as bulk  # noqa: E402

logger = logging.getLogger(__name__)


async def sync_search(repo_name: str | None = None, batch_size: int = 1000) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    index_name = os.getenv("ELASTICSEARCH_INDEX", "opengeometadata_api")
    stats = {"processed": 0, "indexed": 0, "deleted": 0, "errors": 0}
    last_id = None
    await bulk.database.connect()
    try:
        # Do not create an empty index accidentally when a hostname/index is misconfigured.
        if not await bulk.es.indices.exists(index=index_name):
            raise RuntimeError(f"Search index does not exist: {index_name}")
        while True:
            conditions = []
            params = {"limit": batch_size}
            if repo_name is not None:
                conditions.append("""EXISTS (
                    SELECT 1 FROM ogm_resource_state s
                    WHERE s.ogm_resource_id = r.id AND s.ogm_repo_name = :repo_name
                )""")
                params["repo_name"] = repo_name
            if last_id is not None:
                conditions.append("r.id > :last_id")
                params["last_id"] = last_id
            where = " AND ".join(conditions) or "TRUE"
            rows = await bulk.database.fetch_all(
                f"""SELECT r.id, lower(coalesce(nullif(r.b1g_publication_state_s, ''),
                    nullif(r.publication_state, ''), 'published')) AS publication_state
                    FROM resources r WHERE {where} ORDER BY r.id LIMIT :limit""",
                params,
            )
            if not rows:
                break
            last_id = str(rows[-1]["id"])
            published = [str(r["id"]) for r in rows if r["publication_state"] == "published"]
            unpublished = [str(r["id"]) for r in rows if r["publication_state"] != "published"]
            documents = await bulk._prepare_documents_for_chunk(published)
            result = await bulk._bulk_index_documents(index_name, documents, batch_size, 2)
            stats["processed"] += len(rows)
            stats["indexed"] += result["indexed"]
            stats["errors"] += len(published) - result["indexed"]
            if unpublished:
                result = await bulk.es.bulk(
                    operations=[
                        {"delete": {"_index": index_name, "_id": rid}} for rid in unpublished
                    ],
                    refresh=False,
                )
                stats["errors"] += max(0, len(unpublished) - len(result["items"]))
                for item in result["items"]:
                    status = item["delete"]["status"]
                    if status in (200, 404):
                        stats["deleted"] += int(status == 200)
                    else:
                        stats["errors"] += 1
            logger.info("Search synchronization repo=%s stats=%s", repo_name, stats)
        await bulk.es.indices.refresh(index=index_name)
        await CacheService().invalidate_tags({"search", "suggest"})
        if stats["errors"]:
            raise RuntimeError(f"Search synchronization incomplete: {stats}")
        return stats
    finally:
        await bulk.database.disconnect()
        await bulk.es.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    stats = asyncio.run(sync_search(args.repo, args.batch_size))
    print("SEARCH_SYNC_RESULT " + json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
