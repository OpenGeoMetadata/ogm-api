import json

import pytest

from scripts import populate_ogm_repos


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def setup_function():
    populate_ogm_repos._BAD_GITHUB_TOKEN_WARNING_SHOWN = False
    populate_ogm_repos._REJECTED_GITHUB_TOKENS.clear()


@pytest.mark.parametrize("name,suffix", [("ca.lunaris", ""), ("edu.example", "/metadata-aardvark")])
def test_discovery_uses_supported_metadata_root(monkeypatch, name, suffix):
    def fake_get(url, token, params, timeout):
        assert url == f"https://api.github.com/repos/OpenGeoMetadata/{name}/contents{suffix}"
        assert params == {"ref": "master"}
        return FakeResponse(200, [{"name": "provider", "type": "dir"}])

    monkeypatch.setattr(populate_ogm_repos, "_github_get", fake_get)
    has_aardvark = populate_ogm_repos.repo_has_metadata_aardvark(
        "OpenGeoMetadata", name, "master", None
    )
    row = populate_ogm_repos.build_repo_row(
        {"name": name, "archived": False}, has_aardvark=has_aardvark
    )
    assert row["ogm_enabled"] is True
    assert row["ogm_watch_mode"] == "both"


def test_missing_standard_directory_remains_disabled(monkeypatch):
    monkeypatch.setattr(populate_ogm_repos, "_github_get", lambda *a, **kw: FakeResponse(404, {}))
    has_aardvark = populate_ogm_repos.repo_has_metadata_aardvark(
        "OpenGeoMetadata", "utility", "main", None
    )
    assert (
        populate_ogm_repos.build_repo_row({"name": "utility"}, has_aardvark=has_aardvark)[
            "ogm_enabled"
        ]
        is False
    )


def test_list_org_repos_retries_without_rejected_token(monkeypatch, capsys):
    responses = [
        FakeResponse(401, {"message": "Bad credentials"}),
        FakeResponse(200, [{"name": "edu.example"}]),
        FakeResponse(200, []),
    ]
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return responses.pop(0)

    monkeypatch.setattr(populate_ogm_repos.requests, "get", fake_get)

    repos = populate_ogm_repos.list_org_repos("OpenGeoMetadata", "bad-token")

    assert repos == [{"name": "edu.example"}]
    assert calls[0]["headers"]["Authorization"] == "Bearer bad-token"
    assert "Authorization" not in calls[1]["headers"]
    assert "Authorization" not in calls[2]["headers"]
    assert "configured GitHub token was rejected with 401" in capsys.readouterr().err


def test_repo_has_metadata_aardvark_retries_without_rejected_token(monkeypatch):
    responses = [
        FakeResponse(401, {"message": "Bad credentials"}),
        FakeResponse(200, [{"name": "geoblacklight.json"}]),
    ]
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return responses.pop(0)

    monkeypatch.setattr(populate_ogm_repos.requests, "get", fake_get)

    assert (
        populate_ogm_repos.repo_has_metadata_aardvark(
            "OpenGeoMetadata", "edu.example", "main", "bad-token"
        )
        is True
    )
    assert calls[0]["headers"]["Authorization"] == "Bearer bad-token"
    assert "Authorization" not in calls[1]["headers"]


@pytest.mark.parametrize("script_name", ["populate_ogm_repos", "trigger_ogm_nightly_sync"])
@pytest.mark.parametrize("extra_args", [[], ["--include-archived"]])
def test_refresh_disables_archived_repos_and_enables_geobtaa(monkeypatch, script_name, extra_args):
    from importlib import import_module

    script = import_module(f"scripts.{script_name}")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/test")
    args = [script_name, *extra_args]
    if script_name == "trigger_ogm_nightly_sync":
        args.append("--skip-harvest")
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setattr(
        script,
        "list_org_repos",
        lambda *a, **kw: [
            {"name": "edu.umn", "archived": True},
            {"name": "geobtaa", "archived": False},
        ],
    )
    monkeypatch.setattr(script, "repo_has_metadata_aardvark", lambda *a: True)
    rows = []

    def capture(url, values, dry_run=False):
        rows.extend(values)
        return len(values), 0

    monkeypatch.setattr(script, "upsert_rows", capture)
    script.main()
    assert [(r["ogm_repo_name"], r["ogm_enabled"]) for r in rows] == [
        ("edu.umn", False),
        ("geobtaa", True),
    ]
    assert rows[0]["ogm_watch_mode"] == "manual"
    assert rows[0]["ogm_tags"]["ogm_archived"] is True
