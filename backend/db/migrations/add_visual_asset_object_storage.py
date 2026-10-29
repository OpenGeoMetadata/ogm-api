"""Add object-storage pointers while preserving all existing visual asset bytes."""

from sqlalchemy import text


def add_visual_asset_object_storage(engine=None):
    if engine is None:
        from db.session import sync_engine

        engine = sync_engine
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(
            text(
                "ALTER TABLE generated_visual_assets "
                "ADD COLUMN IF NOT EXISTS object_key VARCHAR(1024)"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE generated_visual_assets "
                "ADD COLUMN IF NOT EXISTS object_sha256 VARCHAR(64)"
            )
        )
        conn.execute(text("ALTER TABLE generated_visual_assets ALTER COLUMN body DROP NOT NULL"))


if __name__ == "__main__":
    add_visual_asset_object_storage()
