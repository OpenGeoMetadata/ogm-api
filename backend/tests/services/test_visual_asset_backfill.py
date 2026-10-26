"""Exercise copy-only migration against a real, isolated SQL table."""

import hashlib
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, update

from db.models import generated_visual_assets as assets
from scripts import backfill_visual_assets_to_s3 as migration


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    assets.create(engine)
    with engine.begin() as conn:
        conn.execute(
            assets.insert().values(
                asset_hash="a" * 64,
                asset_kind="thumbnail",
                content_type="image/png",
                body=b"original",
                byte_size=8,
            )
        )
    monkeypatch.setattr(migration, "sync_engine", engine)
    yield engine
    engine.dispose()


def row(engine):
    with engine.connect() as conn:
        return conn.execute(select(assets)).mappings().one()


def test_dry_run_never_uploads_or_changes_body(database):
    with patch.object(migration, "upload_visual_asset") as upload:
        result = migration.backfill()
    assert result["scanned"] == 1
    assert result["copied"] == 0
    assert row(database)["body"] == b"original"
    assert row(database)["object_key"] is None
    upload.assert_not_called()


def test_copy_verifies_bytes_retains_body_and_is_resumable(database):
    digest = hashlib.sha256(b"original").hexdigest()
    with (
        patch.object(migration, "upload_visual_asset", return_value=("assets/" + digest, digest)),
        patch.object(migration, "read_visual_asset", return_value=b"original") as verify,
    ):
        assert migration.backfill(apply=True)["copied"] == 1
        assert migration.backfill(apply=True)["scanned"] == 0
    verify.assert_called_once_with("assets/" + digest, digest, 8)
    assert row(database)["body"] == b"original"
    assert row(database)["object_sha256"] == digest


def test_failed_verification_never_commits_pointer(database):
    with (
        patch.object(migration, "upload_visual_asset", return_value=("assets/hash", "d" * 64)),
        patch.object(migration, "read_visual_asset", side_effect=RuntimeError("corrupted")),
        pytest.raises(RuntimeError, match="corrupted"),
    ):
        migration.backfill(apply=True)
    assert row(database)["object_key"] is None
    assert row(database)["body"] == b"original"


def test_concurrent_render_is_not_replaced_with_stale_pointer(database):
    def render(*args):
        with database.begin() as conn:
            conn.execute(update(assets).values(body=b"new image", byte_size=9))
        return "assets/hash", "d" * 64

    with (
        patch.object(migration, "upload_visual_asset", side_effect=render),
        patch.object(migration, "read_visual_asset", return_value=b"original"),
    ):
        result = migration.backfill(apply=True)
    assert result["concurrently_changed"] == 1
    assert result["copied"] == 0
    assert row(database)["object_key"] is None
    assert row(database)["body"] == b"new image"


def test_parallel_copy_keeps_all_rows_and_commits_each_pointer(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "assets.sqlite"))
    assets.create(engine)
    with engine.begin() as conn:
        conn.execute(
            assets.insert(),
            [
                dict(
                    asset_hash=f"{n:064x}",
                    asset_kind="thumbnail",
                    content_type="image/png",
                    body=b"image",
                    byte_size=5,
                )
                for n in range(80)
            ],
        )
    monkeypatch.setattr(migration, "sync_engine", engine)
    with (
        patch.object(
            migration, "upload_visual_asset", return_value=("assets/digest", "d" * 64)
        ) as upload,
        patch.object(migration, "read_visual_asset", return_value=b"image"),
    ):
        result = migration.backfill(apply=True, limit=100, workers=4)
    upload.assert_called_once()
    assert result["copied"] == 80
    assert result["scanned"] == 80
    with engine.connect() as conn:
        rows = conn.execute(select(assets)).mappings().all()
        assert all(r["object_key"] == "assets/digest" and r["body"] == b"image" for r in rows)
    engine.dispose()
