# Linux container verification — 2026-10-23

## Result

**Not compatible as a drop-in renderer in the current API image.**
The repository Dockerfile uses `python:3.11-slim-bookworm` (Debian 12), not Ubuntu.
The MapLibre Native 6.4.1 Linux ARM64 binary fails to load there: it needs
GLIBC_2.38, GLIBCXX_3.4.31/32, libjpeg.so.8, and ICU 74. Bookworm supplies older
ABIs. Adding Mesa/Xvfb does not resolve this. Do not transplant Ubuntu system
libraries into the API image.

**A separate Ubuntu 24.04 render worker works without a GPU.**
`Dockerfile.worker` runs the same renderer with Mesa llvmpipe and a virtual X
display. `glxinfo -B` reported `Accelerated: no`. No GPU devices were passed.
Installed `fonts-dejavu-core` after visual QA caught missing attribution glyphs;
rerendered and visually verified both corrected images.
Test limits: 2 CPUs, 1 GiB memory; architecture: Linux ARM64 on Docker Desktop.
Production server CPU architecture remains unknown. A subsequent amd64 test
passed under Docker emulation; see ../../../docs/native-static-maps.md (from the
repository root: docs/native-static-maps.md) for the Celery integration results.

| Example | Size | Time | Requests | Linux PNG SHA-256 |
| --- | --- | --- | --- | --- |
| Indiana campus | 640×320 | 1.488 s | 8 | 93d6b7b452665297dfdeaa046c345b82855b042f1cbcba5ad8d4ea734f6f50af |
| Resource footprint | 800×600 | 0.949 s | 22 | 51eda75c73db69272e76c5e96b9e0ce95cf67ce7d8eda52b5eb39815e2017b5a |

These are single-run observations, not load-test benchmarks. The images were
rendered inside Linux, not copied from the earlier macOS demonstration.

## Existing asset system: passed

Built the current repository Dockerfile without modifying it. Used that image
with disposable PostgreSQL 16 and Redis 7.4.6 containers. The test harness mounts
the **unchanged production static-map router** in a minimal FastAPI application;
it does not exercise full application middleware, authentication, Elasticsearch,
or Celery dispatch.

For both PNGs:

- Persisted bytes in existing `generated_visual_assets` storage and Redis.
- GET `/api/v1/static-map-assets/{sha256}` returned HTTP 200, `image/png`, and
  byte-for-byte identical SHA-256.
- Cache-Control was `public, max-age=31536000, immutable`.
- Deleted the Redis asset key; GET recovered the PNG from PostgreSQL and
  repopulated Redis with identical bytes.
- Conditional GET with the returned ETag returned HTTP 304.

The resource also passed `StaticMapService.materialize_asset_sync` and persisted
its existing resource/variant/signature link. Campus IDs are not resource IDs:
the links table has a resource foreign key. The campus test therefore uses the
existing durable content-addressed store and Redis asset methods directly, with
no fabricated resource row. Campus alias integration is a remaining production
change; this test does not claim that campus resource links already work.

## Reproduce

From the repository root (requires Docker; isolated test names must be free):

```sh
docker build -t ogm-native-api-base:prototype .
docker build -f backend/tools/maplibre-static-prototype/Dockerfile.linux -t ogm-native-linux:prototype .
# This preflight demonstrates the incompatibility without waiting on Xvfb:
docker run --rm ogm-native-linux:prototype node -e 'require("@maplibre/maplibre-gl-native")'

docker build -f backend/tools/maplibre-static-prototype/Dockerfile.worker -t ogm-native-worker:prototype .
docker run --name ogm-native-worker --memory=1g --cpus=2 ogm-native-worker:prototype
mkdir -p /tmp/ogm-native-linux-output
docker cp ogm-native-worker:/opt/map-prototype/output/. /tmp/ogm-native-linux-output/

docker network create ogm-native-test
docker run -d --name ogm-native-postgres --network ogm-native-test -e POSTGRES_PASSWORD=prototype -e POSTGRES_DB=native_render_test postgres:16
docker run -d --name ogm-native-redis --network ogm-native-test redis:7.4.6-alpine
# Wait for pg_isready before starting the API harness.
docker exec ogm-native-postgres pg_isready -U postgres -d native_render_test
docker run -d --name ogm-native-assets --network ogm-native-test -p 127.0.0.1:3006:8000 -e DATABASE_URL=postgresql+asyncpg://postgres:prototype@ogm-native-postgres:5432/native_render_test -e REDIS_HOST=ogm-native-redis -e REDIS_PASSWORD= -e VISUAL_ASSET_DURABLE_STORE=database -e PYTHONPATH=/app/backend:/opt/map-prototype -w /app/backend ogm-native-api-base:prototype sleep infinity
docker exec ogm-native-assets mkdir -p /opt/map-prototype/output
docker cp /tmp/ogm-native-linux-output/. ogm-native-assets:/opt/map-prototype/output/
docker cp backend/tools/maplibre-static-prototype/verify_assets.py ogm-native-assets:/opt/map-prototype/
docker cp backend/tools/maplibre-static-prototype/check_http.py ogm-native-assets:/opt/map-prototype/
docker exec -d ogm-native-assets sh -c 'uvicorn verify_assets:app --host 0.0.0.0 --port 8000 > /tmp/asset-server.log 2>&1'
# Once Uvicorn is ready:
docker exec ogm-native-assets python /opt/map-prototype/check_http.py
```

Local review remains available on port 3006. To remove only this experiment:

```sh
docker rm -f -v ogm-native-assets ogm-native-postgres ogm-native-redis ogm-native-worker ogm-native-worker-fonts ogm-native-render
docker network rm ogm-native-test
```

No production Dockerfile, API route, cache schema, or Celery task was changed.
Recommendation: keep rendering in a dedicated compatible worker image if this
approach proceeds. Before adoption, verify deployment CPU architecture, workload
concurrency/memory, provider authorization for bulk generation, and campus alias
handling. A whole-API OS upgrade is not justified by this prototype alone.
