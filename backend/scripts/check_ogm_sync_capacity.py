#!/usr/bin/env python3
"""Read-only preflight for the shared OGM host, streamed over SSH by CI."""

import argparse
import json
import shutil
import sys
from urllib.parse import quote
from urllib.request import urlopen


def get_json(url):
    with urlopen(url, timeout=15) as response:
        return json.load(response)


def check_capacity(path, elasticsearch_url, index, min_free_percent=15.0):
    disk = shutil.disk_usage(path)
    free_percent = 100 * disk.free / disk.total
    problems = []
    report = {
        "path": path,
        "free_gib": round(disk.free / 1024**3, 2),
        "free_percent": round(free_percent, 2),
        "min_free_percent": min_free_percent,
        "index": index,
    }
    if free_percent < min_free_percent:
        problems.append(
            f"Host disk has {free_percent:.2f}% free; at least {min_free_percent}% is required. "
            "Reclaim verified inactive files before harvesting; do not disable disk watermarks."
        )
    base = elasticsearch_url.rstrip("/")
    try:
        nodes = get_json(f"{base}/_nodes/stats/fs?filter_path=nodes.*.name,nodes.*.fs.total")
        if not nodes.get("nodes"):
            raise ValueError("No Elasticsearch filesystem statistics returned")
        for node in nodes["nodes"].values():
            fs = node["fs"]["total"]
            available_percent = 100 * fs["available_in_bytes"] / fs["total_in_bytes"]
            if available_percent < min_free_percent:
                problems.append(
                    f"Elasticsearch node {node['name']} has {available_percent:.2f}% "
                    f"disk available; at least {min_free_percent}% is required."
                )
        settings = get_json(
            f"{base}/{quote(index, safe='')}/_settings?filter_path=*.settings.index.blocks"
        )
        for name, value in settings.items():
            blocks = value.get("settings", {}).get("index", {}).get("blocks", {})
            for flag in ("read_only_allow_delete", "read_only", "write", "metadata"):
                if str(blocks.get(flag, False)).lower() == "true":
                    problems.append(f"Elasticsearch index {name} has index.blocks.{flag}=true")
    except Exception as exc:
        # Fail closed without echoing potentially credential-bearing URLs.
        problems.append(f"Cannot verify Elasticsearch write readiness ({type(exc).__name__})")
    report["problems"] = problems
    report["ready"] = not problems
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="/")
    parser.add_argument("--elasticsearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--index", default="opengeometadata_api")
    parser.add_argument("--min-free-percent", type=float, default=15.0)
    args = parser.parse_args()
    if not 0 < args.min_free_percent < 100:
        parser.error("--min-free-percent must be between 0 and 100")
    report = check_capacity(args.path, args.elasticsearch_url, args.index, args.min_free_percent)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
