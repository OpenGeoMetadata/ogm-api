import hmac
import json
import logging
import os
from asyncio import to_thread
from hashlib import sha256
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, HTTPException, Request

from app.services.ogm_harvest.repository import OGMHarvestRepository
from app.tasks.ogm_harvest import ogm_harvest_repo
from scripts.populate_ogm_repos import build_repo_row, repo_has_metadata_aardvark

logger = logging.getLogger(__name__)

router = APIRouter()

EVENT_DRIVEN_WATCH_MODES = {"webhook", "both", "nightly"}
REPOSITORY_DISCOVERY_ACTIONS = {
    "created",
    "publicized",
    "renamed",
    "transferred",
    "archived",
    "unarchived",
}


def _verify_github_signature(body: bytes, signature_header: Optional[str], secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    their_sig = signature_header.split("=", 1)[1].strip()
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=sha256)
    our_sig = mac.hexdigest()
    return hmac.compare_digest(our_sig, their_sig)


def _repo_identity(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    repo_info = payload.get("repository") or {}
    repo_full = repo_info.get("full_name")
    if isinstance(repo_full, str) and "/" in repo_full:
        return tuple(repo_full.split("/", 1))  # type: ignore[return-value]

    org_info = payload.get("organization") or {}
    org = org_info.get("login")
    repo_name = repo_info.get("name")
    if isinstance(org, str) and isinstance(repo_name, str):
        return org, repo_name
    return None, None


def _changed_paths(commits: Iterable[Dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for commit in commits:
        for key in ("added", "modified", "removed"):
            values = commit.get(key) or []
            if isinstance(values, list):
                paths.update(str(value) for value in values if value)
    return paths


def _push_touches_aardvark(payload: Dict[str, Any]) -> bool:
    if payload.get("deleted") is True:
        return False

    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        # GitHub can truncate or omit commit file lists. When unsure, harvest so the
        # local state catches up instead of silently missing new metadata.
        return True

    paths = _changed_paths([commit for commit in commits if isinstance(commit, dict)])
    if not paths:
        return True
    return any(
        path == "metadata-aardvark" or path.startswith("metadata-aardvark/") for path in paths
    )


async def _repo_has_aardvark(repo_info: Dict[str, Any]) -> bool:
    name = repo_info.get("name")
    owner = (repo_info.get("owner") or {}).get("login")
    full_name = repo_info.get("full_name")
    if isinstance(full_name, str) and "/" in full_name:
        owner, name = full_name.split("/", 1)
    if not isinstance(owner, str) or not isinstance(name, str):
        return False
    return await to_thread(
        repo_has_metadata_aardvark,
        owner,
        name,
        repo_info.get("default_branch"),
        os.getenv("GITHUB_TOKEN"),
    )


async def _discover_repo_from_payload(
    payload: Dict[str, Any], repo: OGMHarvestRepository
) -> Dict[str, Any]:
    repo_info = dict(payload.get("repository") or {})
    if not repo_info:
        return {"discovered": False, "enabled": False, "has_aardvark": False}

    has_aardvark = await _repo_has_aardvark(repo_info)
    row = build_repo_row(repo_info, has_aardvark=has_aardvark)
    await repo.upsert_repo(
        ogm_repo_name=row["ogm_repo_name"],
        ogm_enabled=row["ogm_enabled"],
        ogm_watch_mode=row["ogm_watch_mode"],
        ogm_notes=row["ogm_notes"],
        ogm_tags=row["ogm_tags"],
    )
    return {
        "discovered": True,
        "repo_name": row["ogm_repo_name"],
        "enabled": bool(row["ogm_enabled"]),
        "watch_mode": row["ogm_watch_mode"],
        "has_aardvark": has_aardvark,
    }


def _queue_harvest(repo_name: str, trigger: str) -> Dict[str, Any]:
    task = ogm_harvest_repo.delay(repo_name=repo_name, trigger=trigger)
    return {"queued": repo_name, "task_id": task.id}


@router.post("/ogm/webhook")
async def ogm_webhook(request: Request):
    """
    GitHub webhook receiver for OpenGeoMetadata repository and push events.

    Security: verifies X-Hub-Signature-256 using OGM_WEBHOOK_SECRET.
    """
    secret = os.getenv("OGM_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="OGM_WEBHOOK_SECRET is not configured")

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not _verify_github_signature(body=body, signature_header=sig, secret=secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True}

    try:
        payload: Dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    org, repo_name = _repo_identity(payload)
    if not org or not repo_name:
        raise HTTPException(status_code=400, detail="Missing repository.full_name")

    if org.lower() != "opengeometadata":
        return {"ok": True, "ignored": True, "reason": "not_opengeometadata_org"}

    repo = OGMHarvestRepository()

    if event == "repository":
        action = str(payload.get("action") or "").lower()
        if action not in REPOSITORY_DISCOVERY_ACTIONS:
            return {"ok": True, "ignored": True, "reason": f"repository_action={action}"}

        discovered = await _discover_repo_from_payload(payload, repo)
        if not discovered.get("enabled"):
            return {"ok": True, "ignored": True, "reason": "repo_without_aardvark", **discovered}
        return {"ok": True, **discovered, **_queue_harvest(repo_name, trigger="repository")}

    if event == "push":
        if not _push_touches_aardvark(payload):
            return {"ok": True, "ignored": True, "reason": "push_without_aardvark_changes"}

        row = await repo.get_repo(repo_name)
        if not row:
            discovered = await _discover_repo_from_payload(payload, repo)
            if not discovered.get("enabled"):
                return {
                    "ok": True,
                    "ignored": True,
                    "reason": "repo_without_aardvark",
                    **discovered,
                }
            return {"ok": True, **discovered, **_queue_harvest(repo_name, trigger="push")}

        if not row.get("ogm_enabled", True):
            return {"ok": True, "ignored": True, "reason": "repo_not_enabled"}

        watch_mode = str(row.get("ogm_watch_mode") or "").lower()
        if watch_mode not in EVENT_DRIVEN_WATCH_MODES:
            return {"ok": True, "ignored": True, "reason": f"watch_mode={watch_mode}"}

        return {"ok": True, **_queue_harvest(repo_name, trigger="push")}

    if event == "public":
        discovered = await _discover_repo_from_payload(payload, repo)
        if not discovered.get("enabled"):
            return {"ok": True, "ignored": True, "reason": "repo_without_aardvark", **discovered}
        return {"ok": True, **discovered, **_queue_harvest(repo_name, trigger="public")}

    return {"ok": True, "ignored": True, "reason": f"event={event}"}
