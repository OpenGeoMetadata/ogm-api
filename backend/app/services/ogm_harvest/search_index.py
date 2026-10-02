"""Run bounded bulk indexing in a process isolated from the harvest worker loop."""

import asyncio
import json
import sys
from pathlib import Path


async def sync_repo_search(repo_name: str) -> dict:
    script = Path(__file__).resolve().parents[3] / "scripts" / "sync_ogm_search_index.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--repo",
        repo_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    lines = output.decode("utf-8", errors="replace").splitlines()
    if process.returncode:
        raise RuntimeError("OGM search synchronization failed: " + "\n".join(lines[-10:]))
    for line in reversed(lines):
        if line.startswith("SEARCH_SYNC_RESULT "):
            return json.loads(line.removeprefix("SEARCH_SYNC_RESULT "))
    raise RuntimeError("OGM search synchronization returned no result")
