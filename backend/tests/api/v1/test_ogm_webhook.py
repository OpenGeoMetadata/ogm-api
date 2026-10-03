import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoint_modules import ogm_webhook

SECRET = "test-ogm-webhook-secret"

app = FastAPI()
app.include_router(ogm_webhook.router, prefix="/api/v1/admin")


@pytest.fixture
async def setup_test_database():
    yield None


@pytest.fixture
async def db_connection():
    yield None


@pytest.fixture
async def db_transaction():
    yield None


def _signed_headers(payload: dict, event: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }


class FakeRepo:
    rows: dict[str, dict] = {}
    upserts: list[dict] = []

    async def get_repo(self, repo_name: str):
        return self.rows.get(repo_name)

    async def upsert_repo(self, **kwargs):
        self.upserts.append(kwargs)
        self.rows[kwargs["ogm_repo_name"]] = kwargs


def _repo_payload(name: str = "edu.example", **overrides):
    repo = {
        "name": name,
        "full_name": f"OpenGeoMetadata/{name}",
        "default_branch": "main",
        "archived": False,
        "size": 10,
        "pushed_at": "2026-05-12T22:00:00Z",
        "updated_at": "2026-05-12T22:00:00Z",
        "owner": {"login": "OpenGeoMetadata"},
    }
    repo.update(overrides)
    return repo


def _post_webhook(payload: dict, event: str):
    with TestClient(app) as client:
        return client.post(
            "/api/v1/admin/ogm/webhook",
            content=json.dumps(payload),
            headers=_signed_headers(payload, event),
        )


def setup_function():
    FakeRepo.rows = {}
    FakeRepo.upserts = []


def test_repository_created_discovers_aardvark_repo_and_queues(monkeypatch):
    queued = []

    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(ogm_webhook, "repo_has_metadata_aardvark", lambda *args: True)
    monkeypatch.setattr(
        ogm_webhook.ogm_harvest_repo,
        "delay",
        lambda **kwargs: queued.append(kwargs) or SimpleNamespace(id="task-1"),
    )

    payload = {
        "action": "created",
        "organization": {"login": "OpenGeoMetadata"},
        "repository": _repo_payload(),
    }
    response = _post_webhook(payload, "repository")

    assert response.status_code == 200
    assert response.json()["queued"] == "edu.example"
    assert queued == [{"repo_name": "edu.example", "trigger": "repository"}]
    assert FakeRepo.upserts[0]["ogm_enabled"] is True
    assert FakeRepo.upserts[0]["ogm_watch_mode"] == "both"


def test_repository_created_without_aardvark_is_tracked_but_not_queued(monkeypatch):
    queued = []

    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(ogm_webhook, "repo_has_metadata_aardvark", lambda *args: False)
    monkeypatch.setattr(
        ogm_webhook.ogm_harvest_repo,
        "delay",
        lambda **kwargs: queued.append(kwargs) or SimpleNamespace(id="task-1"),
    )

    payload = {
        "action": "created",
        "organization": {"login": "OpenGeoMetadata"},
        "repository": _repo_payload("docs-only"),
    }
    response = _post_webhook(payload, "repository")

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert response.json()["reason"] == "repo_without_aardvark"
    assert queued == []
    assert FakeRepo.upserts[0]["ogm_enabled"] is False
    assert FakeRepo.upserts[0]["ogm_watch_mode"] == "manual"


def test_push_to_known_repo_queues_when_metadata_aardvark_changes(monkeypatch):
    queued = []
    FakeRepo.rows = {
        "edu.example": {
            "ogm_repo_name": "edu.example",
            "ogm_enabled": True,
            "ogm_watch_mode": "both",
        }
    }

    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(
        ogm_webhook.ogm_harvest_repo,
        "delay",
        lambda **kwargs: queued.append(kwargs) or SimpleNamespace(id="task-1"),
    )

    payload = {
        "organization": {"login": "OpenGeoMetadata"},
        "repository": _repo_payload(),
        "commits": [{"added": ["metadata-aardvark/records/new.json"], "modified": []}],
    }
    response = _post_webhook(payload, "push")

    assert response.status_code == 200
    assert response.json()["queued"] == "edu.example"
    assert queued == [{"repo_name": "edu.example", "trigger": "push"}]


def test_push_ignores_known_repo_without_metadata_changes(monkeypatch):
    queued = []
    FakeRepo.rows = {
        "edu.example": {
            "ogm_repo_name": "edu.example",
            "ogm_enabled": True,
            "ogm_watch_mode": "both",
        }
    }

    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(
        ogm_webhook.ogm_harvest_repo,
        "delay",
        lambda **kwargs: queued.append(kwargs) or SimpleNamespace(id="task-1"),
    )

    payload = {
        "organization": {"login": "OpenGeoMetadata"},
        "repository": _repo_payload(),
        "commits": [{"modified": ["README.md"], "added": [], "removed": []}],
    }
    response = _post_webhook(payload, "push")

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert response.json()["reason"] == "push_without_aardvark_changes"
    assert queued == []


def test_push_to_unknown_aardvark_repo_discovers_and_queues(monkeypatch):
    queued = []

    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(ogm_webhook, "repo_has_metadata_aardvark", lambda *args: True)
    monkeypatch.setattr(
        ogm_webhook.ogm_harvest_repo,
        "delay",
        lambda **kwargs: queued.append(kwargs) or SimpleNamespace(id="task-1"),
    )

    payload = {
        "organization": {"login": "OpenGeoMetadata"},
        "repository": _repo_payload("edu.new"),
        "commits": [{"modified": ["metadata-aardvark/records/changed.json"]}],
    }
    response = _post_webhook(payload, "push")

    assert response.status_code == 200
    assert response.json()["queued"] == "edu.new"
    assert queued == [{"repo_name": "edu.new", "trigger": "push"}]
    assert FakeRepo.upserts[0]["ogm_repo_name"] == "edu.new"


def test_repository_archived_disables_existing_repo_without_queuing(monkeypatch):
    queued = []
    FakeRepo.rows = {"edu.umn": {"ogm_enabled": True, "ogm_watch_mode": "both"}}
    monkeypatch.setenv("OGM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(ogm_webhook, "OGMHarvestRepository", FakeRepo)
    monkeypatch.setattr(ogm_webhook, "repo_has_metadata_aardvark", lambda *args: True)
    monkeypatch.setattr(ogm_webhook.ogm_harvest_repo, "delay", lambda **kw: queued.append(kw))
    response = _post_webhook(
        {"action": "archived", "repository": _repo_payload("edu.umn", archived=True)}, "repository"
    )
    assert response.status_code == 200
    assert FakeRepo.rows["edu.umn"]["ogm_enabled"] is False
    assert FakeRepo.rows["edu.umn"]["ogm_tags"]["ogm_archived"] is True
    assert queued == []
