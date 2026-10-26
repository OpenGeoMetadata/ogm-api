import hashlib
import io
from unittest.mock import MagicMock, patch

import pytest

from app.services import visual_asset_cache as cache
from app.services import visual_asset_object_store as objects


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setenv("VISUAL_ASSET_DURABLE_STORE", "s3")
    monkeypatch.setenv("VISUAL_ASSET_S3_BUCKET", "test-images")
    monkeypatch.setenv("VISUAL_ASSET_S3_PREFIX", "assets")
    monkeypatch.setenv("OGM_MODE", "primary")
    monkeypatch.delenv("VISUAL_ASSET_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("VISUAL_ASSET_REDIS_BODY_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("VISUAL_ASSET_PUBLIC_BASE_URL", raising=False)


def test_upload_uses_actual_bytes_and_verifies_before_returning():
    body = b"generated image"
    digest = hashlib.sha256(body).hexdigest()
    s3 = MagicMock()
    s3.head_object.return_value = {
        "ContentLength": len(body),
        "ContentType": "image/png",
        "Metadata": {"sha256": digest},
    }
    with patch.object(objects, "client", return_value=s3):
        assert objects.upload_visual_asset(body, "image/png") == (f"assets/{digest}", digest)
    args = s3.put_object.call_args.kwargs
    assert args["Body"] == body
    assert args["ContentMD5"]
    assert args["CacheControl"] == "public, max-age=31536000, immutable"
    s3.head_object.assert_called_once_with(Bucket="test-images", Key=f"assets/{digest}")


def test_incomplete_upload_is_not_published():
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": 0}
    with patch.object(objects, "client", return_value=s3):
        with pytest.raises(RuntimeError, match="verification"):
            objects.upload_visual_asset(b"image", "image/png")


def test_read_checks_bytes_and_closes_stream_on_corruption():
    stream = io.BytesIO(b"corrupted")
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": stream}
    with patch.object(objects, "client", return_value=s3):
        with pytest.raises(RuntimeError, match="checksum"):
            objects.read_visual_asset("assets/hash", "0" * 64, 9)
    assert stream.closed


def test_cache_disabled_for_bodies_but_aliases_expire():
    redis = MagicMock()
    assert cache.cache_visual_asset(redis, "image:hash", b"image")
    redis.set.assert_not_called()
    redis.setex.assert_not_called()
    cache.cache_visual_asset(redis, "static_map_alias:id", "hash")
    redis.setex.assert_called_once_with("static_map_alias:id", 86400, "hash")


def test_database_mode_preserves_legacy_cache_default(monkeypatch):
    monkeypatch.setenv("VISUAL_ASSET_DURABLE_STORE", "database")
    redis = MagicMock()
    cache.cache_visual_asset(redis, "image:hash", b"image")
    redis.set.assert_called_once_with("image:hash", b"image")


def test_failed_upload_never_writes_database_pointer():
    with (
        patch.object(cache, "upload_visual_asset", side_effect=OSError("offline")),
        patch.object(cache, "_sync_engine") as engine,
    ):
        assert not cache.store_durable_visual_asset(
            "a" * 64, asset_kind="thumbnail", content_type="image/png", body=b"image"
        )
    engine.assert_not_called()


def test_s3_only_write_keeps_metadata_without_body(monkeypatch):
    monkeypatch.setenv("VISUAL_ASSET_S3_RETAIN_DATABASE_BODY", "false")
    engine = MagicMock()
    with (
        patch.object(cache, "upload_visual_asset", return_value=("assets/digest", "d" * 64)),
        patch.object(cache, "_sync_engine", return_value=engine),
    ):
        assert cache.store_durable_visual_asset(
            "a" * 64, asset_kind="thumbnail", content_type="image/png", body=b"image"
        )
    stmt = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    values = stmt.compile().params
    assert values["body"] is None
    assert values["byte_size"] == 5
    assert values["object_key"] == "assets/digest"


def test_redirect_requires_committed_pointer_and_configured_domain(monkeypatch):
    monkeypatch.setenv("VISUAL_ASSET_PUBLIC_BASE_URL", "https://assets.example.org")
    with patch.object(cache, "_visual_asset_metadata", return_value=None):
        assert cache.get_visual_asset_public_url("a" * 64) is None
    with patch.object(
        cache,
        "_visual_asset_metadata",
        return_value={"object_key": "assets/hash", "object_sha256": "d" * 64},
    ):
        assert (
            cache.get_visual_asset_public_url("a" * 64) == "https://assets.example.org/assets/hash"
        )


def test_object_failure_falls_back_to_existing_database_bytes():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value.mappings.return_value.first.return_value = {
        "body": b"old-image",
        "content_type": "image/png",
    }
    with (
        patch.object(
            cache,
            "_visual_asset_metadata",
            return_value={
                "object_key": "assets/hash",
                "object_sha256": "d" * 64,
                "byte_size": 9,
                "content_type": "image/png",
            },
        ),
        patch.object(cache, "read_visual_asset", side_effect=OSError("offline")),
        patch.object(cache, "_sync_engine", return_value=engine),
    ):
        assert cache.get_durable_visual_asset("a" * 64) == (b"old-image", "image/png")


@pytest.mark.asyncio
async def test_redis_hydration_failure_does_not_hide_durable_thumbnail():
    from app.services.image_service import ImageService

    service = ImageService.__new__(ImageService)
    service.image_cache = MagicMock()
    service.image_cache.get.return_value = None
    service.logger = MagicMock()
    with (
        patch(
            "app.services.image_service.get_durable_visual_asset",
            return_value=(b"image", "image/png"),
        ),
        patch("app.services.image_service.cache_visual_asset", side_effect=OSError("Redis full")),
    ):
        assert await service.get_cached_image("a" * 64) == b"image"


def test_static_map_does_not_publish_alias_after_failed_upload():
    from app.services.static_map_service import StaticMapService

    service = StaticMapService.__new__(StaticMapService)
    with (
        patch("app.services.static_map_service.store_durable_visual_asset", return_value=False),
        patch.object(service, "set_asset_hash_sync") as alias,
        patch.object(service, "cache_asset_sync") as hot_cache,
    ):
        assert (
            service.materialize_asset_sync("resource", variant="static_map_v9", map_bytes=b"image")
            is None
        )
    alias.assert_not_called()
    hot_cache.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["thumbnail", "map"])
async def test_asset_endpoints_redirect_before_loading_bytes(kind):
    from starlette.requests import Request

    from app.api.v1.endpoint_modules import static_maps, thumbnails

    module = thumbnails if kind == "thumbnail" else static_maps
    endpoint = module.get_thumbnail if kind == "thumbnail" else module.get_static_map_asset
    service_name = "ImageService" if kind == "thumbnail" else "StaticMapService"
    with (
        patch.object(
            module,
            "get_visual_asset_public_url",
            return_value="https://assets.example.org/assets/hash",
        ),
        patch.object(module, service_name) as service,
    ):
        response = await endpoint("a" * 64, Request({"type": "http", "headers": []}))
    assert response.status_code == 302
    assert response.headers["location"] == "https://assets.example.org/assets/hash"
    service.assert_not_called()
