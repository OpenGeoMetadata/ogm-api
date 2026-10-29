"""Local-only preview harness: hydrate missing public records into the isolated DB."""
import asyncio
import json
import urllib.request
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.dialects.postgresql import insert
from app.api.v1.endpoint_modules import static_maps
from db.models import resources
from db.session import sync_engine

assert sync_engine.url.database == "native_render_test", "Preview harness requires isolated database"
original_fetch = static_maps._fetch_resource_dict


def hydrate_record(resource_id):
    url = "https://ogm.geo4lib.app/api/v1/resources/" + quote(resource_id, safe="")
    with urllib.request.urlopen(url, timeout=15) as response:
        record = json.load(response)["data"]
    ogm = record["attributes"]["ogm"]
    values = {key: ogm[key] for key in ("dct_title_s", "locn_geometry", "dcat_bbox") if key in ogm}
    with sync_engine.begin() as connection:
        connection.execute(insert(resources).values(id=resource_id, **values).on_conflict_do_nothing())


async def fetch_preview_record(resource_id):
    record = await original_fetch(resource_id)
    if record is None:
        try:
            await asyncio.to_thread(hydrate_record, resource_id)
        except Exception:
            return None
        record = await original_fetch(resource_id)
    return record


static_maps._fetch_resource_dict = fetch_preview_record
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(127\.0\.0\.1|localhost):[0-9]+", allow_methods=["GET"], expose_headers=["Retry-After"])
app.include_router(static_maps.router, prefix="/api/v1")
