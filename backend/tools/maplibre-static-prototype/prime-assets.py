"""Warm native asset URLs before deploy: python prime-assets.py urls.json.

urls.json is an array of full campus or resource thumbnail URLs. Uses only
public GET requests; no direct database access or tile caching.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def prime(url):
    started = time.monotonic()
    while time.monotonic() - started < 180:
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                body = response.read()
                if not body.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("Expected PNG: " + url)
                print(json.dumps({"url": url, "bytes": len(body), "seconds": round(time.monotonic() - started, 3)}), flush=True)
                return
        except urllib.error.HTTPError as error:
            if error.code not in (429, 503):
                raise
            delay = error.headers.get("Retry-After", "2")
            time.sleep(min(10, max(1, int(delay) if delay.isdigit() else 2)))
    raise TimeoutError(url)


if __name__ == "__main__":
    with open(sys.argv[1]) as source:
        urls = json.load(source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(prime, dict.fromkeys(urls)))
