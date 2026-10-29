For the opt-in API/Celery integration and amd64 results, see
[deployment guidance](../../../docs/native-static-maps.md).

# MapLibre Native static-map prototype

Isolated renderer experiment; existing API routes, Celery tasks, Redis, durable
visual asset tables, and production static-map variants are unchanged.
Requires Node 22 and the native runtime supported by @maplibre/maplibre-gl-native.
Verified on macOS and in a separate Ubuntu 24.04 ARM64 software-rendering worker.
The current Debian API image cannot load the native binary. See
[Linux verification results](LINUX-RESULTS.md) for evidence and asset round-trip tests.

```sh
cd backend/tools/maplibre-static-prototype
npm ci
npm test
mkdir -p output
npm run demo
python3 -m http.server 3005 --bind 127.0.0.1 --directory output
```

Open http://127.0.0.1:3005/. Copy `preview.html` to `output/index.html` if
recreating the preview from a clean checkout. Generated files are ignored.

## Examples

- `campus.json`: Indiana University, Discovery's configured center and zoom,
  640×320. Converts legacy 256-pixel zoom 15 to MapLibre zoom 14.
- `resource.json`: real API record `0efeae5d-6e6c-513c-ab45-01c5fc0cb16c`,
  “Madison Structure.” Its ENVELOPE geometry is normalized to a GeoJSON polygon.
  Output is 800×600 with a 48px fit margin. This is the metadata footprint,
  not a rendering of the underlying resource's scientific data.

OpenFreeMap Liberty is used for this bounded experiment, with a unique user
agent, at most four concurrent requests, and per-image request/byte budgets.
There is no persistent tile cache. Do not point this at Maptoolkit's free service:
its community terms exclude backend-only processing. Production bulk generation
needs institution-owned tiles or a provider agreement appropriate to the workload.
The attribution footer is currently specific to OpenFreeMap; changing providers
requires changing credits too. The preview supplies clickable credit links.

Each output has a JSON manifest containing the complete input, camera, render
version, style/input signature, image SHA-256, request count, bytes, and timing.
Remote errors prevent writing a new PNG. A prior successful PNG is left intact.
The style signature includes the resolved style JSON, but not remote tile, glyph,
or sprite contents; use a provider dataset revision before production caching.

## Existing asset-system handoff

`render.mjs` produces PNG bytes; it does not introduce a storage service. A future
Celery adapter can invoke it in a bounded subprocess, validate successful exit
and PNG dimensions/hash, then pass the bytes to the existing
`StaticMapService.materialize_asset_sync(resource_id, variant=..., map_bytes=...,
source_signature=...)`. Use a NEW versioned variant, never the existing CARTO
variants, and the manifest's sourceSignature. This preserves Redis, durable
storage, aliases, and `/static-map-assets/{hash}` delivery. The demo command does not write to databases. The separate Linux integration
harness writes only to disposable test PostgreSQL and Redis containers.

## Before production

- Verify Linux image/runtime, bounded worker concurrency, cancellation, and RSS.
- Add provider-specific attribution, provenance/revision, fallback policy, and
  request allowlisting before accepting untrusted style URLs.
- Exercise true polygon holes/multipolygons, points/lines, invalid geometry,
  dateline normalization, retina output, and missing geometry. Crossing/polar
  bounds deliberately fail in this prototype rather than silently misframe.
- Validate sprite/glyph/tile failure behavior and publish assets atomically.
- Keep final images/credits readable at the intended campus tile display size.
- Wire a versioned production variant and regenerate through existing jobs only
  after visual parity and provider suitability are established.
