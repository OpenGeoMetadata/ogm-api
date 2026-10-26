"""Copy durable image bodies to S3/R2 without deleting local data or changing URLs."""

import argparse
import hashlib
from threading import Lock
import json
import random
import time
from botocore.exceptions import ClientError
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from sqlalchemy import select, update

from app.services.visual_asset_object_store import read_visual_asset, upload_visual_asset
from db.models import generated_visual_assets as assets
from db.session import sync_engine


class VerifiedCopies:
    """Share one verified upload among rows with identical bytes and MIME type."""

    def __init__(self):
        self.lock = Lock()
        self.copies = {}

    def ensure(self, body, content_type):
        identity = (hashlib.sha256(body).hexdigest(), content_type)
        with self.lock:
            future = self.copies.get(identity)
            owner = future is None
            if owner:
                future = self.copies[identity] = Future()
        if owner:
            try:
                for attempt in range(6):
                    try:
                        key, digest = upload_visual_asset(body, content_type)
                        break
                    except ClientError as exc:
                        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if status not in (429, 500, 502, 503, 504) or attempt == 5:
                            raise
                        time.sleep(2**attempt + random.random())
                read_visual_asset(key, digest, len(body))
                future.set_result((key, digest))
            except Exception as exc:
                future.set_exception(exc)
                raise
        return future.result()


def copy_row(row, copies):
    body = bytes(row["body"])
    key, digest = copies.ensure(body, row["content_type"])
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
    copied = skipped = scanned = completed = reported = 0
    cursor = after
    pending = set()
    copies = VerifiedCopies()
    exhausted = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or not exhausted:
            if not exhausted and len(pending) < workers * 2:
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
                    exhausted = True
                else:
                    for row in rows:
                        if apply and workers > 1:
                            pending.add(pool.submit(copy_row, row, copies))
                        elif apply:
                            changed = copy_row(row, copies)
                            copied += changed
                            skipped += 1 - changed
                            completed += 1
                        else:
                            completed += 1
                    scanned += len(rows)
                    cursor = rows[-1]["asset_hash"]
                    exhausted = scanned >= limit
            if pending and (exhausted or len(pending) >= workers * 2):
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    changed = future.result()
                    copied += changed
                    skipped += 1 - changed
                    completed += 1
            if progress and completed - reported >= 500:
                progress(
                    {
                        "scanned": scanned,
                        "copied": copied,
                        "concurrently_changed": skipped,
                        "in_flight": len(pending),
                    }
                )
                reported = completed
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
    if not 1 <= args.workers <= 128:
        parser.error("--workers must be between 1 and 128")
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
