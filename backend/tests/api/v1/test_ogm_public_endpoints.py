from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoint_modules import ogm

app = FastAPI()
app.include_router(ogm.router, prefix="/api/v1")

client = TestClient(app)


def test_public_ogm_repos_endpoint_returns_repo_summaries():
    sample_repos = [
        {
            "ogm_repo_name": "edu.utexas",
            "ogm_enabled": True,
            "ogm_watch_mode": "weekly",
            "last_crawl_started_at": "2026-02-25T14:57:16.327086",
            "last_crawl_completed_at": "2026-02-25T14:58:11.946192",
            "last_crawl_status": "success",
            "last_run_id": 142,
            "harvested_success_count": 876,
            "harvested_failure_count": 1800,
        }
    ]

    with patch(
        "app.api.v1.endpoint_modules.ogm.ogm_repo.list_public_repo_summaries",
        new_callable=AsyncMock,
    ) as mock_summaries:
        mock_summaries.return_value = sample_repos
        response = client.get("/api/v1/ogm/repos")

    assert response.status_code == 200
    data = response.json()
    assert "repos" in data
    assert data["repos"] == sample_repos


def test_public_ogm_repo_dashboard_renders_html_monitor():
    sample_repos = [
        {
            "ogm_repo_name": "edu.utexas",
            "ogm_repo_full_name": "OpenGeoMetadata/edu.utexas",
            "ogm_github_url": "https://github.com/OpenGeoMetadata/edu.utexas",
            "ogm_search_url": "/api/v1/search?ogm_repo=edu.utexas",
            "ogm_enabled": True,
            "ogm_watch_mode": "nightly",
            "ogm_has_aardvark": True,
            "last_commit_at": "2026-02-26T03:15:16Z",
            "last_commit_sha": "abc123def4567890",
            "last_crawl_started_at": "2026-02-25T14:57:16.327086",
            "last_crawl_completed_at": "2026-02-25T14:58:11.946192",
            "last_crawl_status": "success",
            "last_run_id": 142,
            "harvested_success_count": 876,
            "harvested_failure_count": 0,
            "harvested_record_count": 904,
            "available_record_count": 901,
            "unpublished_record_count": 3,
        }
    ]

    with (
        patch.object(
            ogm.ogm_repo, "list_public_repo_summaries", AsyncMock(return_value=sample_repos)
        ),
        patch.object(
            ogm,
            "get_index_status",
            AsyncMock(return_value={"record_count": 1234, "repo_counts": {"edu.utexas": 899}}),
        ),
    ):
        response = client.get("/api/v1/ogm/repos/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "OGM Repository Monitor" in response.text
    assert "edu.utexas" in response.text
    assert "OpenGeoMetadata/edu.utexas" in response.text
    assert "/api/v1/ogm/repos" in response.text
    assert "899" in response.text
    assert "1,234" in response.text
    assert "Never Harvested Here" not in response.text
    assert 'href="https://github.com/OpenGeoMetadata/edu.utexas"' in response.text
    assert 'href="/api/v1/search?ogm_repo=edu.utexas"' in response.text


def test_public_ogm_failures_endpoint_lists_failed_runs():
    sample_failures = [
        {
            "ogm_id": 140,
            "ogm_repo_name": "edu.utexas",
            "ogm_status": "success",
            "ogm_error": None,
            "ogm_stats_json": {
                "imported": 876,
                "errors": 1800,
                "error_samples": [{"stage": "bulk_upsert_single", "error": "bad geometry"}],
                "error_signatures": [{"signature": "DataError: bad geometry", "count": 1800}],
            },
        }
    ]

    with patch(
        "app.api.v1.endpoint_modules.ogm.ogm_repo.list_harvest_runs",
        new_callable=AsyncMock,
    ) as mock_runs:
        mock_runs.return_value = sample_failures
        response = client.get("/api/v1/ogm/harvest/failures?limit=25&offset=0")

    assert response.status_code == 200
    data = response.json()
    assert data["repo_name"] is None
    assert data["include_with_errors"] is True
    assert len(data["failures"]) == 1
    assert data["failures"][0]["import_error_count"] == 1800
    assert data["failures"][0]["imported_count"] == 876
    assert data["failures"][0]["failure_reason"] == "One or more records failed during import"
    assert len(data["failures"][0]["error_samples"]) == 1
    assert len(data["failures"][0]["error_signatures"]) == 1
    mock_runs.assert_awaited_once_with(
        ogm_repo_name=None,
        ogm_status=None,
        limit=225,
        offset=0,
    )


def test_public_ogm_failures_endpoint_filters_single_repo():
    with patch(
        "app.api.v1.endpoint_modules.ogm.ogm_repo.list_harvest_runs",
        new_callable=AsyncMock,
    ) as mock_runs:
        mock_runs.return_value = []
        response = client.get("/api/v1/ogm/harvest/failures?repo_name=edu.utexas")

    assert response.status_code == 200
    data = response.json()
    assert data["repo_name"] == "edu.utexas"
    assert data["failures"] == []
    mock_runs.assert_awaited_once_with(
        ogm_repo_name="edu.utexas",
        ogm_status=None,
        limit=250,
        offset=0,
    )


def test_public_ogm_failures_endpoint_can_limit_to_hard_failures_only():
    sample_failures = [{"ogm_id": 140, "ogm_repo_name": "edu.utexas", "ogm_status": "failed"}]

    with patch(
        "app.api.v1.endpoint_modules.ogm.ogm_repo.list_harvest_runs",
        new_callable=AsyncMock,
    ) as mock_runs:
        mock_runs.return_value = sample_failures
        response = client.get("/api/v1/ogm/harvest/failures?include_with_errors=false")

    assert response.status_code == 200
    data = response.json()
    assert data["include_with_errors"] is False
    assert len(data["failures"]) == 1
    mock_runs.assert_awaited_once_with(
        ogm_repo_name=None,
        ogm_status="failed",
        limit=50,
        offset=0,
    )


def test_dashboard_only_shows_indexed_sources_even_when_database_disagrees():
    repos = [
        {"ogm_repo_name": "retired", "ogm_enabled": False, "available_record_count": 100},
        {"ogm_repo_name": "empty", "ogm_enabled": True, "available_record_count": 10},
        {
            "ogm_repo_name": "geobtaa",
            "ogm_enabled": True,
            "ogm_watch_mode": "nightly",
            "available_record_count": 1,
        },
    ]
    with (
        patch.object(ogm.ogm_repo, "list_public_repo_summaries", AsyncMock(return_value=repos)),
        patch.object(
            ogm,
            "get_index_status",
            AsyncMock(
                return_value={
                    "record_count": 318408,
                    "repo_counts": {"geobtaa": 40703, "uncataloged": 2},
                }
            ),
        ),
    ):
        response = client.get("/api/v1/ogm/repos/dashboard")
    assert response.status_code == 200
    for expected in ("318,408", "40,703", "geobtaa", "uncataloged", "Scheduled Nightly"):
        assert expected in response.text
    for absent in ("retired", "Never Harvested Here", "Database", "also in active sources"):
        assert absent not in response.text
    assert 'href="https://github.com/OpenGeoMetadata/uncataloged"' in response.text


def test_dashboard_reports_index_unavailable_instead_of_misleading_empty_counts():
    with patch.object(ogm, "get_index_status", AsyncMock(side_effect=RuntimeError("offline"))):
        response = client.get("/api/v1/ogm/repos/dashboard")
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_dashboard_empty_index():
    with (
        patch.object(ogm.ogm_repo, "list_public_repo_summaries", AsyncMock(return_value=[])),
        patch.object(
            ogm, "get_index_status", AsyncMock(return_value={"record_count": 0, "repo_counts": {}})
        ),
    ):
        response = client.get("/api/v1/ogm/repos/dashboard")
    assert response.status_code == 200
    assert "No repositories currently have indexed records" in response.text
