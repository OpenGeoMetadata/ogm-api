"""Verify every remote image before optionally releasing its PostgreSQL body.

The fsynced JSONL manifest is written before each small cleanup transaction.
An optimistic body/pointer comparison protects concurrent generation.
"""

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import Text, cast, func, literal_column, or_, select, update

from app.services.visual_asset_object_store import client, read_visual_asset
from db.models import generated_visual_assets as assets
from db.session import sync_engine


def verified_record(row):
    if not row["object_key"] or not row["object_sha256"]:
        raise RuntimeError("Asset has no durable object pointer: " + row["asset_hash"])
    remote = read_visual_asset(row["object_key"], row["object_sha256"], row["byte_size"])
    if row["body"] is not None and bytes(row["body"]) != remote:
        raise RuntimeError("Local/remote bytes differ: " + row["asset_hash"])
    return {
        "asset_hash": row["asset_hash"],
        "object_key": row["object_key"],
        "object_sha256": hashlib.sha256(remote).hexdigest(),
        "byte_size": len(remote),
        "content_type": row["content_type"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def clear_verified_body(conn, row):
    if row["body"] is None:
        return 0
    unchanged = (
        cast(literal_column("xmin"), Text) == row["_row_version"]
        if "_row_version" in row
        else assets.c.body == bytes(row["body"])
    )
    return conn.execute(
        update(assets)
        .where(
            assets.c.asset_hash == row["asset_hash"],
            assets.c.object_key == row["object_key"],
            assets.c.object_sha256 == row["object_sha256"],
            assets.c.byte_size == row["byte_size"],
            assets.c.body.is_not(None),
            unchanged,
        )
        .values(body=None)
    ).rowcount


def verify(*, manifest, prune=False, workers=8, limit=1000000, after=""):
    if prune:
        with sync_engine.connect() as conn:
            missing = conn.execute(
                select(func.count())
                .select_from(assets)
                .where(or_(assets.c.object_key.is_(None), assets.c.object_sha256.is_(None)))
            ).scalar_one()
        if missing:
            raise RuntimeError(f"Refusing cleanup: {missing} assets lack object pointers")
    client()  # Construct the SDK client before starting verification threads.
    verified = cleared = released_bytes = reported = 0
    cursor = after
    columns = [assets]
    if sync_engine.dialect.name == "postgresql":
        # xmin changes on every concurrent UPDATE, including body-only updates.
        # Avoid shipping every large image back in the cleanup WHERE clause.
        columns.append(cast(literal_column("xmin"), Text).label("_row_version"))
    os.umask(0o077)
    with (
        open(manifest, "a", encoding="utf-8") as audit,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        while verified < limit:
            with sync_engine.connect() as conn:
                rows = (
                    conn.execute(
                        select(*columns)
                        .where(assets.c.asset_hash > cursor)
                        .order_by(assets.c.asset_hash)
                        .limit(min(max(25, workers * 2), limit - verified))
                    )
                    .mappings()
                    .all()
                )
            if not rows:
                break
            records = list(pool.map(verified_record, rows))
            for record in records:
                audit.write(json.dumps(record) + "\n")
            audit.flush()
            os.fsync(audit.fileno())
            if prune:
                with sync_engine.begin() as conn:
                    for row in rows:
                        changed = clear_verified_body(conn, row)
                        cleared += changed
                        released_bytes += changed * row["byte_size"]
            verified += len(rows)
            cursor = rows[-1]["asset_hash"]
            if verified - reported >= 500:
                reported = verified
                print(
                    json.dumps(
                        {
                            "verified": verified,
                            "cleared": cleared,
                            "released_bytes": released_bytes,
                            "last_hash": cursor,
                        }
                    ),
                    flush=True,
                )
    result = {
        "verified": verified,
        "cleared": cleared,
        "released_bytes": released_bytes,
        "last_hash": cursor,
        "manifest": manifest,
        "complete": verified < limit,
    }
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--prune-postgres", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--after", default="")
    args = parser.parse_args()
    if not 1 <= args.workers <= 128 or args.limit < 1:
        parser.error("workers must be 1..128 and limit positive")
    verify(
        manifest=args.manifest,
        prune=args.prune_postgres,
        workers=args.workers,
        limit=args.limit,
        after=args.after,
    )


if __name__ == "__main__":
    main()
