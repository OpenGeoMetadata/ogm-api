"""Metadata locations shared by GitHub discovery and checkout harvesting."""


def metadata_root(repo_name: str) -> str:
    # Lunaris publishes Aardvark JSON directly under provider directories.
    # Keep this explicit: scanning arbitrary utility repos would ingest fixtures.
    if repo_name == "ca.lunaris":
        return "."
    return "metadata-aardvark"
