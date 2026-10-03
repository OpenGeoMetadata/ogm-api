from unittest.mock import Mock, patch

import pytest

from scripts import trigger_ogm_nightly_sync as trigger


def test_wait_checks_children_after_enqueue(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/test")
    monkeypatch.setattr("sys.argv", ["trigger", "--wait"])
    task = Mock(id="batch")
    with (
        patch.object(trigger, "list_org_repos", return_value=[]),
        patch.object(trigger, "upsert_rows", return_value=(0, 0)),
        patch.object(trigger.ogm_harvest_all, "delay", return_value=task) as enqueue,
        patch("scripts.wait_ogm_harvest.wait_for_harvest") as wait,
    ):
        trigger.main()
    enqueue.assert_called_once_with(trigger="nightly")
    assert wait.call_args.args[0] is task


def test_discovery_error_does_not_disable_sources_or_enqueue(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/test")
    monkeypatch.setattr("sys.argv", ["trigger", "--wait"])
    with (
        patch.object(trigger, "list_org_repos", return_value=[{"name": "source"}]),
        patch.object(trigger, "repo_has_metadata_aardvark", side_effect=RuntimeError("offline")),
        patch.object(trigger, "upsert_rows") as upsert,
        patch.object(trigger.ogm_harvest_all, "delay") as enqueue,
    ):
        with pytest.raises(RuntimeError, match="Repository discovery failed"):
            trigger.main()
    upsert.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.parametrize("flag", ["--dry-run", "--skip-harvest", "--limit=1"])
def test_wait_requires_a_complete_batch(monkeypatch, flag):
    monkeypatch.setattr("sys.argv", ["trigger", "--wait", flag])
    with pytest.raises(SystemExit) as exc:
        trigger.main()
    assert exc.value.code == 2
