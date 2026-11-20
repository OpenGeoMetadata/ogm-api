# Optional native static maps

The API and its Celery worker retain the existing Debian base image. An internal
Ubuntu 24.04 service renders PNGs using MapLibre Native, Xvfb, Mesa llvmpipe, and
DejaVu fonts. No GPU is required. Celery sends render specifications to this
service; only the API owns Redis, PostgreSQL storage, and asset HTTP delivery.

## Enable

```sh
docker compose -f docker-compose.yml -f docker-compose.native-maps.yml up -d --build
```

The override sets `STATIC_MAP_RENDERER_URL=http://map_renderer:8080` on `api` and
`celery_worker` and adds the private `map_renderer` service. Do not publish its
port externally. It accepts one render at a time, rejects overload with 503,
and terminates a render subprocess after 65 seconds. The API client has bounded
connect/read timeouts. Campus jobs retry failures up to three times through
Celery. A crashed worker cannot retain a WebGL context across jobs: rendering is
performed in a separate process for each image.

`STATIC_MAP_RENDER_REVISION` defaults to `openfreemap-liberty-native-v1`. Change it
when changing styles, datasets, fonts, or renderer behavior. Both API and Celery
must use the same value. Dimensions and input geometry/camera also participate
in signatures. Native assets use separate variants from legacy CARTO assets.

`STATIC_MAP_STYLE_URL` defaults to OpenFreeMap Liberty. Currently only
`tiles.openfreemap.org` styles are supported because the burned-in attribution
and Discovery's companion credit links are OpenFreeMap-specific. A contracted
or institution-owned provider requires adapting the renderer's allowed style
host and attribution together. The Maptoolkit community endpoint must not be
used for backend generation. Establish appropriate provider arrangements before
large collection backfills; do not infer bulk-production authorization from the
small integration test. There is no persistent tile cache.

## Campus requests and caching

`GET /api/v1/static-maps/institutions/{slug}/native?lat=39.1702&lon=-86.5235&zoom=15`

- Requires the native renderer configuration; never falls back to CARTO.
- Validates camera ranges, then checks the versioned image cache.
- Enqueues `generate_campus_static_map` on a miss, deduplicated by Redis lock.
- Waits up to eight seconds for initial generation; otherwise returns 503 with
  `Retry-After: 5` and `Cache-Control: no-store`. Discovery retries up to eight
  times and keeps the school logo/branded background visible while waiting.
- The task validates the PNG and dimensions before publishing. Failure leaves
  prior cached bytes intact. A pending lock expires after five minutes if a
  worker is killed; regular retry exhaustion releases it immediately.
- Campus bytes use the existing durable content-addressed asset table plus
  Redis. No resource row is fabricated. The camera-to-image cache lives in Redis;
  losing that cache regenerates the campus image. The durable hash URL remains
  available. No resource-link schema changes are introduced.

Legacy campus URLs also select native rendering when the integration is enabled.
The separate `/native` URL protects Discovery against older APIs that would
otherwise ignore a new query parameter and serve CARTO.

## Resource maps

The existing `generate_static_map`, batch, and collection Celery tasks select the
native renderer through `StaticMapService` when configured. Existing resource
geometry normalization is reused; GeoJSON polygons retain their rings. Rendered
PNGs use the existing resource links, Redis, durable table, and
`/api/v1/static-map-assets/{hash}` endpoint. Existing synchronous compatibility
routes also use the selected renderer; this change does not convert every
legacy resource HTTP cache miss into a queued job.

Keep the renderer disabled to retain legacy behavior. Disabling it does not
serve native campus URLs from legacy caches. No asset tables are dropped or
rewritten, and the production Dockerfile is unchanged by this integration.

## Verification (2026-10-23)

- Ubuntu ARM64 and amd64 both rendered campus and resource examples with
  `Accelerated: no`. amd64 ran under Docker CPU emulation on an ARM Mac; this is
  binary/runtime compatibility evidence, not a native-server performance test.
  Initial amd64 renders took 3.489 s and 3.268 s respectively.
- Real Celery worker in the API's Debian image called the amd64 renderer.
  A cold native campus HTTP request queued a job and returned HTTP 200/PNG.
- Existing resource-generation task returned success; the image was retrieved
  through the API hash URL and matched its durable stored bytes.
- Earlier asset integration checks verified HTTP 304 and recovery after Redis
  eviction. The unchanged asset router is mounted in a minimal FastAPI harness;
  production authentication/middleware and a live production deploy were not
  part of this test.
- 41 backend static-map tests passed, including configuration signatures,
  invalid cameras, failed render rejection, and no legacy fallback.
- All 17 BTAA campus images were generated through Celery and returned HTTP 200.
  Browser inspection confirmed the complete grid of school logos over campus
  backgrounds and visible provider credits.
- Discovery's two retry/cleanup tests passed, and its subpath production build passed.

Repeatable renderer Dockerfiles and test harnesses are under
`backend/tools/maplibre-static-prototype/`. `check_celery.py` requires the isolated
`native_render_test` database and an API on port 8000 in its container. It updates
only that test resource and invokes the actual Celery task. Never point a test
harness at production data.


## Resource geography migration

Discovery requests `/api/v1/static-maps/{id}/geometry/native` for generated
resource geography images. This route requires the renderer configuration,
looks up the current geometry and native revision signature, and queues
`generate_native_resource_map` on a cache miss. Redis pending locks deduplicate
concurrent requests; pending responses are 503 with Retry-After and no-store.
The task uses the existing durable asset store and resource links. It does not
replace immutable legacy hashes, which may contain baked-in CARTO error tiles.
Deploy this route and the native worker before the matching Discovery build.
Custom external static-map URLs are preserved by Discovery.

Local resource-thumbnail verification (2026-10-23): 20 real first-page search
records were seeded into the isolated test database and rendered by the amd64
Ubuntu service through Celery. All 20 returned PNGs; their durable bytes,
resource links, SHA-256 hashes, and immutable HTTP asset responses matched.
The Hamilton County output was visually inspected. Backend checks: 42 tests
passed. Browser screenshot automation failed, so final in-page visual inspection
remains outstanding. No production records or assets were changed.

## Loading performance

Native cache misses return 503 / Retry-After: 2 immediately after enqueueing;
they do not occupy a connection with an eight-second polling loop. Successful
campus and native resource responses allow one hour of browser caching.
`?size=thumbnail` requests a 384×288 resource map with its own geometry/dimension
signature and compact, two-line attribution. The full-size route is unchanged.
Discovery fetches near-viewport images and retries only 503/429 responses without
cache-busting query parameters; CORS must allow the frontend origin.

Warm deployment-critical URLs with:
`python backend/tools/maplibre-static-prototype/prime-assets.py urls.json`
where the JSON file is an array of full native campus/thumbnail URLs. This uses
the actual API/Celery path and existing asset storage. It does not cache tiles.
Cold render latency remains dependent on provider/network and worker capacity.

Measured locally on the amd64-emulated worker: existing cold jobs took 2–3s each.
After warming, 17 campus assets with six concurrent HTTP clients had median 14ms,
maximum 56ms. One resource's full PNG was 475,026 bytes; the first compact version
was 131,382 bytes (72% smaller) and served in 5ms warm. These are local API
measurements, not production/network or browser-paint benchmarks. The preview
harness now hydrates missing public records into native_render_test only, so
browsing beyond the original 20 fixtures no longer produces repeated 404s.


## Pre-generation and geometry reuse

With `STATIC_MAP_RENDERER_URL` configured, the existing static-map cache primer
(including bridge-sync cache refresh) and Celery resource generation prepare the
384×288 search thumbnail before the larger maps. This happens even when the
larger maps already exist. Existing records need a backfill; deploying the code
alone does not make every first visit warm.

From `backend/`, with the normal database, Redis and renderer configuration:

```sh
python scripts/prime_static_map_cache.py --concurrency 1 --limit 250
```

Remove `--limit` for the full catalog, or pass explicit resource IDs. Start with
one concurrent job for the single-slot renderer. The primer also prepares the
existing full-size variants. For thumbnail-only batches, enqueue the registered
Celery task `pregenerate_native_thumbnails` with one argument: a list of at most
100 resource IDs. Complete each batch before submitting the next to avoid a large
queue. The task retries transient failures and reports prepared and missing counts.

Matching normalized geometries reuse a durable PNG across resource IDs. Reuse
also checks dimensions, render revision, and overlay kind, so thumbnails cannot
replace full-size maps or plain basemaps. Each resource receives its own link in
the existing asset system; no new storage service or schema is required. A Redis
lock keyed by the render signature prevents simultaneous duplicate rendering.
The native resource endpoint also checks for reusable assets before enqueueing.

Local verification: an unvisited geology search on page 9 contained 20 records
with 13 distinct geometry signatures. Celery prepared the batch in 32.66 seconds
on the emulated amd64 worker. On the subsequent first browser visit, the first
three thumbnails appeared together at 1.304 seconds after navigation, all HTTP
200 with no generation retries. The earlier unprepared search took 5.630–9.698
seconds for its first three maps. These are local, unthrottled browser timings,
not a production benchmark. A separate new record reused a matching PNG with
renderer HTTP calls deliberately blocked, confirming durable cross-record reuse.
