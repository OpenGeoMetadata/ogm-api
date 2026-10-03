"""Wait for every repository's harvest and search synchronization to finish."""

import time


def wait_for_harvest(task, result_factory, timeout=18000):
    deadline = time.monotonic() + timeout

    def result(item):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Nightly harvest deadline exceeded")
        return item.get(timeout=remaining, propagate=True)

    batch = result(task)
    ids = batch.get("task_ids", [])
    names = batch.get("repo_names", [])
    if (
        not ids
        or len(ids) != batch.get("enqueued")
        or len(ids) != len(names)
        or len(set(ids)) != len(ids)
        or len(set(names)) != len(names)
    ):
        raise RuntimeError("Incomplete or empty harvest batch")
    failures = []
    for name, task_id in zip(names, ids):
        try:
            outcome = result(result_factory(task_id))
            stats = outcome.get("stats", {})
            index = stats.get("search_index", {})
            if (
                not outcome.get("ogm_run_id")
                or outcome.get("ogm_repo_name") != name
                or stats.get("errors") != 0
                or index.get("errors") != 0
                or not {"processed", "indexed", "deleted"}.issubset(index)
            ):
                raise RuntimeError("Harvest or indexing did not complete successfully")
            print(
                f"Completed {name}: imported={stats.get('imported', 0)} "
                f"indexed={index['indexed']} deleted={index['deleted']}",
                flush=True,
            )
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    if failures:
        raise RuntimeError("Nightly sync failed: " + "; ".join(failures))
    return batch
