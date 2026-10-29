import logging
import os
import time
from typing import Any, Callable, TypeVar

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.services.visual_asset_object_store import (
    public_visual_asset_url,
    read_visual_asset,
    s3_visual_assets_enabled,
    upload_visual_asset,
)
from db.models import generated_visual_asset_links, generated_visual_assets
from db.session import sync_engine as app_sync_engine

logger = logging.getLogger(__name__)
T = TypeVar("T")


def visual_asset_ttl_seconds() -> int:
    """Bound hot-cache retention in object-storage mode; preserve legacy defaults."""
    default = "86400" if s3_visual_assets_enabled() else "0"
    return int(os.getenv("VISUAL_ASSET_CACHE_TTL_SECONDS", default))


def redis_visual_body_cache_enabled() -> bool:
    default = "false" if s3_visual_assets_enabled() else "true"
    return os.getenv("VISUAL_ASSET_REDIS_BODY_CACHE_ENABLED", default).lower() == "true"


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default=%s", name, value, default)
        return default


def redis_loading_max_wait_seconds() -> float:
    """How long visual-cache operations should wait while Redis is loading."""
    return max(0.0, _env_float("VISUAL_ASSET_REDIS_LOADING_MAX_WAIT_SECONDS", 0.0))


def redis_loading_retry_seconds() -> float:
    """Retry cadence while Redis reports it is loading its persisted dataset."""
    return max(0.05, _env_float("VISUAL_ASSET_REDIS_LOADING_RETRY_SECONDS", 5.0))


def is_redis_loading_error(exc: BaseException) -> bool:
    """Return True when Redis rejected the command because it is still loading."""
    if exc.__class__.__name__ == "BusyLoadingError":
        return True
    return "redis is loading the dataset in memory" in str(exc).lower()


def redis_operation_with_loading_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
) -> T:
    """
    Run a Redis operation, optionally waiting through Redis LOADING states.

    Priming jobs set VISUAL_ASSET_REDIS_LOADING_MAX_WAIT_SECONDS so a Redis
    restart/load does not become thousands of false per-resource failures.
    Normal request handling keeps the default zero wait and falls back quickly.
    """
    deadline = time.monotonic() + redis_loading_max_wait_seconds()
    warned = False

    while True:
        try:
            return operation()
        except Exception as exc:
            if not is_redis_loading_error(exc) or time.monotonic() >= deadline:
                raise

            remaining = max(0.0, deadline - time.monotonic())
            if not warned:
                logger.warning(
                    "%s blocked while Redis loads its dataset; waiting up to %.0fs",
                    operation_name,
                    remaining,
                )
                warned = True
            time.sleep(min(redis_loading_retry_seconds(), remaining))


def cache_visual_asset(cache: Any, key: str, value: bytes | str) -> bool:
    # A disabled optional binary cache is a successful no-op, including renderer callers.
    # Alias strings still receive a finite TTL in S3 mode.
    if isinstance(value, bytes) and not redis_visual_body_cache_enabled():
        return True
    ttl = visual_asset_ttl_seconds()
    if ttl > 0:
        return bool(
            redis_operation_with_loading_retry(
                lambda: cache.setex(key, ttl, value),
                operation_name=f"cache visual asset {key}",
            )
        )
    return bool(
        redis_operation_with_loading_retry(
            lambda: cache.set(key, value),
            operation_name=f"cache visual asset {key}",
        )
    )


def durable_visual_asset_enabled() -> bool:
    return os.getenv("VISUAL_ASSET_DURABLE_STORE", "database").lower() in {"database", "s3", "r2"}


def _sync_engine():
    return app_sync_engine


def store_durable_visual_asset(
    asset_hash: str,
    *,
    asset_kind: str,
    content_type: str,
    body: bytes,
) -> bool:
    if not durable_visual_asset_enabled() or not asset_hash or not body:
        return False

    values = {
        "asset_hash": asset_hash,
        "asset_kind": asset_kind,
        "content_type": content_type,
        "body": body,
        "byte_size": len(body),
    }
    if s3_visual_assets_enabled():
        try:
            key, digest = upload_visual_asset(body, content_type)
        except Exception as exc:
            logger.warning(
                "Visual asset upload failed for %s: %s", asset_hash[:12], type(exc).__name__
            )
            return False
        values.update(object_key=key, object_sha256=digest)
        # Dual-write is the default for a reversible pilot. Disabling it affects NEW writes only.
        if os.getenv("VISUAL_ASSET_S3_RETAIN_DATABASE_BODY", "true").lower() != "true":
            values["body"] = None
    else:
        # Database-only writes must never retain a stale pointer to previous bytes.
        values.update(object_key=None, object_sha256=None)
    stmt = pg_insert(generated_visual_assets).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[generated_visual_assets.c.asset_hash],
        set_={
            "asset_kind": stmt.excluded.asset_kind,
            "content_type": stmt.excluded.content_type,
            "body": stmt.excluded.body,
            "byte_size": stmt.excluded.byte_size,
            "object_key": stmt.excluded.object_key,
            "object_sha256": stmt.excluded.object_sha256,
        },
    )
    try:
        with _sync_engine().begin() as conn:
            conn.execute(stmt)
        return True
    except Exception as exc:
        logger.warning("Failed to persist visual asset %s: %s", asset_hash[:12], exc)
        return False


def store_durable_visual_asset_link(
    resource_id: str,
    *,
    asset_hash: str,
    asset_kind: str,
    source_signature: str | None = None,
) -> bool:
    if not durable_visual_asset_enabled() or not resource_id or not asset_hash:
        return False

    values = {
        "resource_id": resource_id,
        "asset_hash": asset_hash,
        "asset_kind": asset_kind,
        "source_signature": source_signature or "",
    }
    stmt = pg_insert(generated_visual_asset_links).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_generated_visual_asset_links_resource_kind_signature",
        set_={"asset_hash": stmt.excluded.asset_hash},
    )
    try:
        with _sync_engine().begin() as conn:
            conn.execute(stmt)
        return True
    except Exception as exc:
        logger.warning(
            "Failed to persist visual asset link %s %s %s: %s",
            resource_id,
            asset_kind,
            asset_hash[:12],
            exc,
        )
        return False


def get_durable_visual_asset_hash_for_resource(
    resource_id: str,
    *,
    asset_kind: str,
    source_signature: str | None = None,
) -> str | None:
    if not durable_visual_asset_enabled() or not resource_id:
        return None

    stmt = select(generated_visual_asset_links.c.asset_hash).where(
        generated_visual_asset_links.c.resource_id == resource_id,
        generated_visual_asset_links.c.asset_kind == asset_kind,
        generated_visual_asset_links.c.source_signature == (source_signature or ""),
    )
    try:
        with _sync_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
    except Exception as exc:
        logger.warning(
            "Failed to load durable visual asset link %s %s: %s",
            resource_id,
            asset_kind,
            exc,
        )
        return None
    if not row:
        return None
    return str(row._mapping["asset_hash"])


def _visual_asset_metadata(asset_hash: str):
    if not durable_visual_asset_enabled() or not asset_hash:
        return None
    stmt = select(
        generated_visual_assets.c.content_type,
        generated_visual_assets.c.byte_size,
        generated_visual_assets.c.object_key,
        generated_visual_assets.c.object_sha256,
        generated_visual_assets.c.body.is_not(None).label("has_body"),
    ).where(generated_visual_assets.c.asset_hash == asset_hash)
    with _sync_engine().connect() as conn:
        return conn.execute(stmt).mappings().first()


def get_visual_asset_public_url(asset_hash: str) -> str | None:
    """Only expose a CDN URL after the upload and database pointer have committed."""
    if not s3_visual_assets_enabled():
        return None
    try:
        row = _visual_asset_metadata(asset_hash)
        if row and row["object_key"] and row["object_sha256"]:
            return public_visual_asset_url(row["object_key"])
    except Exception as exc:
        logger.warning("Unable to resolve visual asset URL: %s", type(exc).__name__)
    return None


def has_durable_visual_asset(asset_hash: str) -> bool:
    """Check committed metadata without downloading an image on every alias lookup."""
    if not durable_visual_asset_enabled() or not asset_hash:
        return False
    stmt = select(generated_visual_assets.c.asset_hash).where(
        generated_visual_assets.c.asset_hash == asset_hash,
        or_(
            generated_visual_assets.c.body.is_not(None),
            generated_visual_assets.c.object_key.is_not(None)
            & generated_visual_assets.c.object_sha256.is_not(None),
        ),
    )
    try:
        with _sync_engine().connect() as conn:
            return conn.execute(stmt).first() is not None
    except Exception as exc:
        logger.warning("Unable to check durable visual asset: %s", type(exc).__name__)
        return False


def get_durable_visual_asset(asset_hash: str) -> tuple[bytes, str] | None:
    if not durable_visual_asset_enabled() or not asset_hash:
        return None
    try:
        row = _visual_asset_metadata(asset_hash)
        if not row:
            return None
        if row["object_key"] and row["object_sha256"]:
            try:
                body = read_visual_asset(row["object_key"], row["object_sha256"], row["byte_size"])
                return body, str(row["content_type"])
            except Exception as exc:
                logger.warning("Object read failed; trying database body: %s", type(exc).__name__)
        stmt = select(generated_visual_assets.c.body, generated_visual_assets.c.content_type).where(
            generated_visual_assets.c.asset_hash == asset_hash
        )
        with _sync_engine().connect() as conn:
            local = conn.execute(stmt).mappings().first()
        if local and local["body"]:
            return bytes(local["body"]), str(local["content_type"])
    except Exception as exc:
        logger.warning("Failed to load visual asset %s: %s", asset_hash[:12], type(exc).__name__)
    return None


def get_durable_visual_asset_hash_for_signature(
    asset_kind: str, source_signature: str
) -> str | None:
    """Find reusable render bytes without tying identity to a catalog record."""
    if not durable_visual_asset_enabled() or not source_signature:
        return None
    stmt = (
        select(generated_visual_asset_links.c.asset_hash)
        .where(
            generated_visual_asset_links.c.asset_kind == asset_kind,
            generated_visual_asset_links.c.source_signature == source_signature,
        )
        .limit(1)
    )
    with _sync_engine().connect() as conn:
        return conn.execute(stmt).scalar_one_or_none()
