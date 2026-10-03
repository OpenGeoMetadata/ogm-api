# OpenGeoMetadata Harvester

The standalone harvester now lives in `backend/scripts/ogm_harvester.py`. It mirrors the GeoCombine-style workflow for cloning OpenGeoMetadata repositories, pulling updates, and iterating over Aardvark records for indexing.

## Overview

`OGMHarvester` provides:

- repository discovery from the OpenGeoMetadata GitHub organization
- clone and pull support for metadata repositories
- recursive Aardvark/GeoBlacklight JSON harvesting
- schema-version filtering
- a generator interface for downstream indexing work

## CLI Usage

Run it from the backend directory:

```bash
cd backend

# List repositories
python scripts/ogm_harvester.py --action list

# Clone all harvestable repositories
python scripts/ogm_harvester.py --action clone

# Pull updates from existing clones
python scripts/ogm_harvester.py --action pull

# Clone or pull, then stream harvestable records
python scripts/ogm_harvester.py --action harvest

# Override defaults
python scripts/ogm_harvester.py --ogm-path data/opengeometadata --schema-version Aardvark
```

## Python Usage

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend" / "scripts"))

from ogm_harvester import OGMHarvester

harvester = OGMHarvester(
    ogm_path="backend/data/opengeometadata",
    schema_version="Aardvark",
)

for record, path in harvester.docs_to_index():
    record_id = record.get("id") or record.get("layer_slug_s")
    print(record_id, path)
```

## Defaults

- default clone location: `data/opengeometadata` relative to the backend working directory
- default schema version: `Aardvark`

The harvester denylist automatically excludes non-metadata repositories such as `GeoCombine`, `aardvark`, and other utility repos in the org.

## Related Backend Flows

For the full application workflow, these backend scripts usually matter more than the standalone harvester:

- `scripts/populate_ogm_repos.py`
- `scripts/trigger_ogm_nightly_sync.py`
- `scripts/run_index.py`

Those scripts populate the database tables used by the API and enqueue the nightly ingest pipeline, while `ogm_harvester.py` remains useful for direct inspection and lower-level debugging.

After a successful repository upsert, the application detects new records and
changes to thumbnail-bearing fields (references, image fields, access rights,
resource class, service identifier, and geometry). It re-primes only those
records into OGM-owned visual storage and invalidates generated resource
representations that may contain an older asset URL. This refresh can be disabled
with `OGM_THUMBNAIL_REFRESH_ENABLED=false`; a refresh failure is recorded in the
harvest statistics and does not turn an otherwise successful metadata harvest
into a failure.

Repository discovery always reconciles archived GitHub repositories into the
catalog as disabled with manual watch mode, including repositories that were
previously enabled. The legacy `--include-archived` flag is accepted for
compatibility but is no longer needed. Archived BTAA repositories are replaced
by the active `geobtaa` repository. Harvest jobs check the saved enabled/archive
status before pulling and preserve catalog settings when starting a run.
The standalone harvester also excludes archived checkouts when streaming records.
Existing records are retained until refreshed from their current source; disabling
a repository does not delete its previously harvested records.
