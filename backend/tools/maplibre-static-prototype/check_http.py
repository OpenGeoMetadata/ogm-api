"""Check real HTTP delivery, ETags, and durable-store recovery after cache eviction."""
import hashlib
import json
import urllib.request
import urllib.error
from pathlib import Path

from app.services.static_map_service import StaticMapService

service = StaticMapService()
for asset in json.loads(Path('/opt/map-prototype/output/assets.json').read_text()):
    url = 'http://127.0.0.1:8000' + asset['url']
    for mode in ('redis', 'durable-recovery'):
        if mode == 'durable-recovery':
            service.map_cache.delete(service._asset_key(asset['sha256']))
        with urllib.request.urlopen(url) as response:
            assert response.status == 200
            assert response.headers['Content-Type'] == 'image/png'
            assert hashlib.sha256(response.read()).hexdigest() == asset['sha256']
            etag = response.headers['ETag']
            print(asset['name'], mode, response.status, response.headers['Cache-Control'], etag)
        assert service.map_cache.exists(service._asset_key(asset['sha256']))
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={'If-None-Match': etag}))
    except urllib.error.HTTPError as response:
        assert response.code == 304
        print(asset['name'], 'conditional GET', response.code)
    else:
        raise AssertionError('Expected HTTP 304')
