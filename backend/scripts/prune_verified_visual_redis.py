"""Remove only Redis DB 1 image bytes matching a verified R2 manifest."""

import argparse
import hashlib
import json
import os
import re

import redis

VISUAL_PREFIX = re.compile(r"^(image|static_map_asset|static_map_v[0-9]+|static_basemap_v[0-9]+):")
COMPARE_UNLINK = (
    "if redis.call('GET',KEYS[1]) == ARGV[1] then "
    "return redis.call('UNLINK',KEYS[1]) else return 0 end"
)


def is_verified_image(key, value, verified):
    return bool(
        VISUAL_PREFIX.match(key.decode("utf-8"))
        and value
        and verified.get(hashlib.sha256(value).hexdigest()) == len(value)
    )


def prune(manifest, *, apply=False):
    verified = {}
    with open(manifest, encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            verified[record["object_sha256"]] = record["byte_size"]
    if not verified:
        raise ValueError("Verification manifest is empty")
    cache = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=1,
        socket_timeout=30,
        socket_connect_timeout=5,
    )
    scanned = matched = removed = retained = released_bytes = 0
    pending = []

    def batch(keys):
        nonlocal scanned, matched, removed, retained, released_bytes
        values = cache.mget(keys)
        selected = []
        for key, value in zip(keys, values):
            scanned += 1
            if is_verified_image(key, value, verified):
                matched += 1
                selected.append((key, value))
            else:
                retained += 1
        if apply and selected:
            pipe = cache.pipeline(transaction=False)
            for key, value in selected:
                pipe.eval(COMPARE_UNLINK, 1, key, value)
            for changed, (_, value) in zip(pipe.execute(), selected):
                removed += changed
                released_bytes += changed * len(value)
        if scanned % 500 == 0:
            print(
                json.dumps(
                    dict(
                        scanned=scanned,
                        matched=matched,
                        removed=removed,
                        retained=retained,
                        released_bytes=released_bytes,
                    )
                ),
                flush=True,
            )

    for key in cache.scan_iter(count=500):
        if not VISUAL_PREFIX.match(key.decode("utf-8")):
            continue
        pending.append(key)
        if len(pending) >= 25:
            batch(pending)
            pending = []
    if pending:
        batch(pending)
    result = dict(
        scanned=scanned,
        matched=matched,
        removed=removed,
        retained=retained,
        released_bytes=released_bytes,
        dry_run=not apply,
        redis_db=1,
    )
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    prune(args.manifest, apply=args.apply)


if __name__ == "__main__":
    main()
