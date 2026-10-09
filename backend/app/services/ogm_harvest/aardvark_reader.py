from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Generator, Iterable

from app.services.ogm_harvest.layout import metadata_root


@dataclass(frozen=True)
class AardvarkRecordRef:
    record: Dict
    source_path: str  # repo-relative path


def iter_aardvark_json_files(repo_dir: Path) -> Iterable[Path]:
    """Yield JSON candidates from the repository's supported metadata root."""
    base = repo_dir / metadata_root(repo_dir.name)
    if not base.exists():
        return []
    return (
        p
        for p in base.rglob("*.json")
        if p.is_file()
        and p.name != "layers.json"
        and not any(part.startswith(".") for part in p.relative_to(base).parts)
    )


def read_aardvark_records(repo_dir: Path) -> Generator[AardvarkRecordRef, None, None]:
    """
    Read Aardvark JSON records from a repo checkout.

    Supports files that contain:
    - a single JSON object (one record)
    - a JSON array of objects (many records)
    """
    for path in iter_aardvark_json_files(repo_dir):
        try:
            raw = path.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except Exception:
            continue

        records: Iterable[Dict]
        if isinstance(doc, dict):
            records = [doc]
        elif isinstance(doc, list):
            records = [r for r in doc if isinstance(r, dict)]
        else:
            continue

        rel = str(path.relative_to(repo_dir))
        for record in records:
            # Root layouts can contain unrelated JSON or older schema exports.
            if metadata_root(repo_dir.name) == "." and record.get("gbl_mdVersion_s") != "Aardvark":
                continue
            yield AardvarkRecordRef(record=record, source_path=rel)


def extract_record_id(record: Dict) -> str | None:
    return (
        record.get("id")
        or record.get("layer_slug_s")
        or record.get("dc_identifier_s")
        or record.get("dct_identifier_s")
    )
