"""Copy durable image bodies to S3/R2 without deleting local data or changing URLs."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select, update

from app.services.visual_asset_object_store import read_visual_asset, upload_visual_asset
from db.models import generated_visual_assets as assets
from db.session import sync_engine


def copy_row(row):
    body = bytes(row["body"])
    key, digest = upload_visual_asset(body, row["content_type"])
    read_visual_asset(key, digest, len(body))
    with sync_engine.begin() as conn:
        result = conn.execute(
            update(assets)
            .where(
                assets.c.asset_hash == row["asset_hash"],
                assets.c.body == body,
                assets.c.content_type == row["content_type"],
                assets.c.object_key.is_(None),
            )
            .values(object_key=key, object_sha256=digest)
        )
        return result.rowcount


def backfill(*, apply=False, limit=100, after="", workers=1, progress=None):
    copied = skipped = scanned = 0
    cursor = after
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while scanned < limit:
            stmt = (
                select(assets.c.asset_hash, assets.c.body, assets.c.content_type)
                .where(
                    assets.c.asset_hash > cursor,
                    assets.c.body.is_not(None),
                    assets.c.object_key.is_(None),
                )
                .order_by(assets.c.asset_hash)
                .limit(min(25, limit - scanned))
            )
            with sync_engine.connect() as conn:
                rows = conn.execute(stmt).mappings().all()
            if not rows:
                break
            if apply:
                # Serial default keeps embedded-database tests and small pilots simple.
                results = map(copy_row, rows) if workers == 1 else pool.map(copy_row, rows)
                for changed in results:
                    copied += changed
                    skipped += 1 - changed
            scanned += len(rows)
            cursor = rows[-1]["asset_hash"]
            if progress and scanned % 500 == 0:
                progress(
                    {
                        "scanned": scanned,
                        "copied": copied,
                        "concurrently_changed": skipped,
                        "last_hash": cursor,
                    }
                )
    return {
        "scanned": scanned,
        "copied": copied,
        "concurrently_changed": skipped,
        "last_hash": cursor,
        "dry_run": not apply,
        "local_bodies_deleted": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after", default="")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    print(
        json.dumps(
            backfill(
                apply=args.apply,
                limit=args.limit,
                after=args.after,
                workers=args.workers,
                progress=lambda data: print(json.dumps(data), flush=True),
            )
        )
    )


if __name__ == "__main__":
    main()
