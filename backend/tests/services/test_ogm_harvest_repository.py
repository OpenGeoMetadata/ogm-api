import pytest

from app.services.ogm_harvest import repository as ogm_repository
from app.services.ogm_harvest.repository import OGMHarvestRepository
from db.database import database


def _compile_like_databases(query) -> str:
    compiled = query.compile(
        dialect=database._backend._dialect,
        compile_kwargs={"render_postcompile": True},
    )
    compiled_params = sorted(compiled.params.items())
    mapping = {key: f"${i}" for i, (key, _) in enumerate(compiled_params, start=1)}
    return compiled.string % mapping


@pytest.mark.asyncio
async def test_public_repo_summaries_query_casts_static_string_parameters(monkeypatch):
    captured = {}

    async def fake_fetch_all(query):
        captured["query"] = query
        return []

    monkeypatch.setattr(ogm_repository.database, "fetch_all", fake_fetch_all)

    summaries = await OGMHarvestRepository().list_public_repo_summaries()

    assert summaries == []
    sql = _compile_like_databases(captured["query"])
    assert "concat(CAST(" in sql
    assert "nullif(resources.b1g_publication_state_s, CAST(" in sql
    assert "nullif(resources.publication_state, CAST(" in sql
    assert ") = CAST(" in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_repo_counts_preserve_tag_and_publication_semantics(
    monkeypatch, db_connection
):
    """Exercise real PostgreSQL array/count behavior using connection-local tables."""
    import os

    import asyncpg
    from sqlalchemy.schema import CreateTable

    from db.models import ogm_harvest_runs, ogm_repos, ogm_resource_state

    url = os.getenv("OGM_QUERY_TEST_DATABASE_URL")
    if not url and db_connection is not None:
        url = str(db_connection.url).replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        pytest.skip("Set OGM_QUERY_TEST_DATABASE_URL to run isolated PostgreSQL query tests")
    connection = await asyncpg.connect(url)
    try:
        for table in (ogm_repos, ogm_harvest_runs, ogm_resource_state):
            ddl = str(CreateTable(table).compile(dialect=database._backend._dialect))
            await connection.execute(ddl.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
        await connection.execute("""
            CREATE TEMP TABLE resources (
                id text PRIMARY KEY, "b1g_adminTags_sm" varchar[],
                b1g_publication_state_s varchar, publication_state varchar,
                gbl_suppressed_b boolean
            );
            INSERT INTO ogm_repos (ogm_repo_name, ogm_enabled) VALUES
                ('alpha', true), ('beta', false), ('empty', true);
            INSERT INTO ogm_resource_state
                (ogm_repo_name, ogm_resource_id, ogm_missing_since) VALUES
                ('alpha', 'active', NULL), ('alpha', 'missing', now()),
                ('beta', 'active', NULL);
            INSERT INTO ogm_harvest_runs
                (ogm_id, ogm_repo_name, ogm_trigger, ogm_status) VALUES
                (1, 'alpha', 'manual', 'failed'), (2, 'alpha', 'manual', 'success');
        """)
        await connection.executemany(
            """INSERT INTO resources VALUES ($1, $2, $3, $4, $5)""",
            [
                ("duplicate", ["ogm_repo:alpha", "ogm_repo:alpha"], None, None, None),
                ("shared", ["ogm_repo:alpha", "ogm_repo:beta"], "PUBLISHED", "draft", True),
                ("fallback", ["ogm_repo:alpha"], "", "Published", False),
                ("unpublished", ["ogm_repo:alpha"], "Draft", "published", True),
                ("legacy", ["ogm_repo:alpha"], None, "draft", None),
                ("blank", ["ogm_repo:beta"], "", "", False),
                ("null-tags", None, None, None, True),
                ("empty-tags", [], None, None, True),
                ("other-tags", [None, "ogmXrepo:alpha", "ogm_repo:unknown"], None, None, True),
            ],
        )

        async def fetch_all(query):
            compiled = query.compile(
                dialect=database._backend._dialect,
                compile_kwargs={"render_postcompile": True},
            )
            parameters = [value for _, value in sorted(compiled.params.items())]
            return await connection.fetch(_compile_like_databases(query), *parameters)

        monkeypatch.setattr(ogm_repository.database, "fetch_all", fetch_all)
        summaries = await OGMHarvestRepository().list_public_repo_summaries()
        assert [row["ogm_repo_name"] for row in summaries] == ["alpha", "beta", "empty"]
        counts = {
            row["ogm_repo_name"]: (
                row["harvested_record_count"],
                row["available_record_count"],
                row["suppressed_record_count"],
                row["unpublished_record_count"],
            )
            for row in summaries
        }
        assert counts == {"alpha": (1, 3, 2, 2), "beta": (1, 2, 1, 0), "empty": (0, 0, 0, 0)}
        assert summaries[0]["last_run_id"] == 2
        assert summaries[0]["last_crawl_status"] == "success"
        assert summaries[1]["ogm_enabled"] is False
        assert summaries[2]["last_run_id"] is None
    finally:
        await connection.close()
