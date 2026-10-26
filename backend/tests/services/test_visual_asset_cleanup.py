import hashlib
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, update

from db.models import generated_visual_assets as assets
from scripts import verify_visual_assets_r2 as cleanup


def sample():
    return dict(
        asset_hash="a" * 64,
        object_key="assets/test",
        object_sha256=hashlib.sha256(b"image").hexdigest(),
        body=b"image",
        byte_size=5,
        asset_kind="thumbnail",
        content_type="image/png",
    )


def test_wrong_remote_copy_cannot_be_verified():
    with patch.object(cleanup, "read_visual_asset", return_value=b"wrong"):
        with pytest.raises(RuntimeError, match="bytes differ"):
            cleanup.verified_record(sample())


def test_missing_pointer_cannot_be_verified():
    row = sample()
    row["object_key"] = None
    with pytest.raises(RuntimeError, match="no durable"):
        cleanup.verified_record(row)


def test_clear_preserves_metadata_and_concurrent_new_body():
    engine = create_engine("sqlite://")
    assets.create(engine)
    row = sample()
    with engine.begin() as conn:
        conn.execute(assets.insert().values(**row))
        conn.execute(update(assets).values(body=b"newer"))
        assert cleanup.clear_verified_body(conn, row) == 0
        assert conn.execute(select(assets.c.body)).scalar_one() == b"newer"
        conn.execute(update(assets).values(body=b"image"))
        assert cleanup.clear_verified_body(conn, row) == 1
        stored = conn.execute(select(assets)).mappings().one()
        assert stored["body"] is None
        assert stored["object_key"] == row["object_key"]
        assert stored["byte_size"] == 5
    engine.dispose()


def test_redis_cleanup_requires_known_namespace_and_verified_exact_bytes():
    from scripts.prune_verified_visual_redis import is_verified_image

    manifest = {hashlib.sha256(b"image").hexdigest(): 5}
    assert is_verified_image(b"image:hash", b"image", manifest)
    assert is_verified_image(b"static_basemap_v7:resource", b"image", manifest)
    assert not is_verified_image(b"celery-task-meta-id", b"image", manifest)
    assert not is_verified_image(b"image_type:hash", b"image", manifest)
    assert not is_verified_image(b"image:hash", b"other", manifest)
