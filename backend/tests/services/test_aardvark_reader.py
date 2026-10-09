import json

from app.services.ogm_harvest.aardvark_reader import read_aardvark_records


def write_json(repo, path, value):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))


def test_lunaris_provider_layout_filters_records_and_preserves_provenance(tmp_path):
    repo = tmp_path / "ca.lunaris"
    record = {"id": "transit", "gbl_mdVersion_s": "Aardvark"}
    write_json(repo, "abacus-open-data/transit.json", record)
    write_json(
        repo,
        "another-provider/nested/records.json",
        [
            {"id": "second", "gbl_mdVersion_s": "Aardvark"},
            {"id": "legacy", "geoblacklight_version": "1.0"},
            "not a record",
        ],
    )
    write_json(repo, "package.json", {"name": "unrelated"})
    write_json(repo, ".git/hidden.json", record)
    write_json(repo, "abacus-open-data/layers.json", record)
    (repo / "broken.json").write_text("not json")

    refs = sorted(read_aardvark_records(repo), key=lambda ref: ref.record["id"])
    assert [ref.record["id"] for ref in refs] == ["second", "transit"]
    assert refs[1].source_path == "abacus-open-data/transit.json"


def test_standard_layout_keeps_existing_behavior(tmp_path):
    repo = tmp_path / "edu.example"
    record = {"id": "existing"}
    write_json(repo, "metadata-aardvark/nested/record.json", record)
    write_json(repo, "fixtures/unrelated.json", {"id": "fixture", "gbl_mdVersion_s": "Aardvark"})
    refs = list(read_aardvark_records(repo))
    assert [ref.record for ref in refs] == [record]
    assert refs[0].source_path == "metadata-aardvark/nested/record.json"


def test_unknown_repo_does_not_fall_back_to_root(tmp_path):
    repo = tmp_path / "utility"
    write_json(repo, "fixtures/record.json", {"id": "fixture", "gbl_mdVersion_s": "Aardvark"})
    assert list(read_aardvark_records(repo)) == []
