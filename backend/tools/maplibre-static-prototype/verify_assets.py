"""Isolated integration harness: real API router, Redis and PostgreSQL, no mocks."""
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.v1.endpoint_modules.static_maps import router
from app.services.static_map_service import StaticMapService
from app.services.visual_asset_cache import get_durable_visual_asset, store_durable_visual_asset
from db.models import resources, generated_visual_assets, generated_visual_asset_links
from db.session import sync_engine

# Test-only database. Never run this against a production DATABASE_URL.
assert sync_engine.url.database == 'native_render_test'
for table in (resources, generated_visual_assets, generated_visual_asset_links):
    table.create(sync_engine, checkfirst=True)

service = StaticMapService()
results = []
for name in ('campus', 'resource'):
    path = Path('/opt/map-prototype/output') / f'{name}.png'
    body = path.read_bytes()
    manifest = json.loads(path.with_suffix('.png.json').read_text())
    digest = hashlib.sha256(body).hexdigest()
    assert digest == manifest['sha256']
    variant = 'maplibre_native_prototype_v1'
    if name == 'resource':
        with sync_engine.begin() as connection:
            connection.execute(insert(resources).values(id=manifest['spec']['id'], dct_title_s=manifest['spec']['title']).on_conflict_do_nothing(index_elements=['id']))
        assert service.materialize_asset_sync(manifest['spec']['id'], variant=variant, map_bytes=body, source_signature=manifest['sourceSignature']) == digest
        with sync_engine.connect() as connection:
            assert connection.execute(select(generated_visual_asset_links).where(generated_visual_asset_links.c.asset_hash == digest)).first()
    else:
        # Campus IDs are not resource IDs: avoid violating the resource-link FK.
        # Use the existing content-addressed asset store, not a fabricated resource.
        assert store_durable_visual_asset(digest, asset_kind=variant, content_type='image/png', body=body)
        service.cache_asset_sync(digest, body)
    assert get_durable_visual_asset(digest) == (body, 'image/png')
    assert service.map_cache.get(service._asset_key(digest)) == body
    results.append({'name': name, 'sha256': digest, 'bytes': len(body), 'url': f'/api/v1/static-map-assets/{digest}'})
Path('/opt/map-prototype/output/assets.json').write_text(json.dumps(results, indent=2))

app = FastAPI()
app.include_router(router, prefix='/api/v1')
