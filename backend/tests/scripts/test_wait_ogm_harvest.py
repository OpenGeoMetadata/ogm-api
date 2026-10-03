from unittest.mock import Mock, patch

import pytest

from scripts.wait_ogm_harvest import wait_for_harvest


def successful(name="a"):
    return {
        "ogm_run_id": 1,
        "ogm_repo_name": name,
        "stats": {
            "errors": 0,
            "imported": 5,
            "search_index": {"processed": 5, "indexed": 5, "deleted": 0, "errors": 0},
        },
    }


def batch():
    return Mock(
        get=Mock(return_value={"enqueued": 2, "repo_names": ["a", "b"], "task_ids": ["1", "2"]})
    )


def test_waits_for_all_children_and_index_results():
    factory = Mock(
        side_effect=[
            Mock(get=Mock(return_value=successful("a"))),
            Mock(get=Mock(return_value=successful("b"))),
        ]
    )
    assert wait_for_harvest(batch(), factory)["enqueued"] == 2
    assert factory.call_count == 2


@pytest.mark.parametrize("failure", ["import", "index", "missing_index", "skipped", "raised"])
def test_failed_child_fails_batch_but_remaining_children_are_awaited(failure):
    outcome = successful()
    if failure == "import":
        outcome["stats"]["errors"] = 1
    if failure == "index":
        outcome["stats"]["search_index"]["errors"] = 1
    if failure == "missing_index":
        del outcome["stats"]["search_index"]
    if failure == "skipped":
        outcome = {"status": "skipped"}
    first = (
        Mock(get=Mock(side_effect=RuntimeError("worker failed")))
        if failure == "raised"
        else Mock(get=Mock(return_value=outcome))
    )
    factory = Mock(side_effect=[first, Mock(get=Mock(return_value=successful("b")))])
    with pytest.raises(RuntimeError, match="Nightly sync failed"):
        wait_for_harvest(batch(), factory)
    assert factory.call_count == 2


def test_empty_batch_cannot_report_success():
    with pytest.raises(RuntimeError, match="Incomplete or empty"):
        wait_for_harvest(Mock(get=Mock(return_value={"enqueued": 0})), Mock())


def test_wait_deadline():
    with patch("scripts.wait_ogm_harvest.time.monotonic", side_effect=[0, 100]):
        with pytest.raises(TimeoutError):
            wait_for_harvest(batch(), Mock(), timeout=10)
