import json

from scripts.ogm_harvester import OGMHarvester


def test_docs_to_index_excludes_archived_checkouts(tmp_path, monkeypatch):
    for repo in ("edu.umn", "geobtaa"):
        directory = tmp_path / repo / "metadata-aardvark"
        directory.mkdir(parents=True)
        (directory / "record.json").write_text(
            json.dumps(
                {
                    "id": "same-migrated-record",
                    "gbl_mdVersion_s": "Aardvark",
                    "source": repo,
                }
            )
        )
    harvester = OGMHarvester(ogm_path=str(tmp_path))
    monkeypatch.setattr(harvester, "repositories", lambda: ["geobtaa"])
    records = list(harvester.docs_to_index())
    assert len(records) == 1
    assert records[0][0]["source"] == "geobtaa"
