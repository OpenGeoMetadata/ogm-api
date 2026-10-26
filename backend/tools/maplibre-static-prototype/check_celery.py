import hashlib
import json
import urllib.request
from pathlib import Path

from sqlalchemy import update
from app.tasks.worker import celery_app
from app.services.static_map_service import StaticMapService
from app.services.visual_asset_cache import get_durable_visual_asset
from db.models import resources
from db.session import sync_engine

assert sync_engine.url.database == 'native_render_test'
record = json.loads(Path('/opt/map-prototype/resource.json').read_text())
with sync_engine.begin() as conn:
    conn.execute(update(resources).where(resources.c.id == record['id']).values(locn_geometry=record['sourceGeometry']))
task = celery_app.send_task('generate_static_map', args=[record['id']])
result = task.get(timeout=100)
assert result['success'], result
service = StaticMapService()
sig = service.geometry_signature(record['sourceGeometry'])
digest = service.get_asset_hash_sync(record['id'], variant=service.geometry_variant(), source_signature=sig)
assert digest
url = 'http://127.0.0.1:8000/api/v1/static-map-assets/' + digest
with urllib.request.urlopen(url) as response:
    data = response.read()
    assert hashlib.sha256(data).hexdigest() == digest
    assert get_durable_visual_asset(digest) == (data,'image/png')
    assert response.headers['Content-Type'] == 'image/png'
print(json.dumps({'task': task.id, 'result': result, 'asset': digest, 'bytes': len(data)}))
Path('/tmp/celery-resource.png').write_bytes(data)
