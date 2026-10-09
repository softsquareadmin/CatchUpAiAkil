import pytest
import yaml

from app.config import ROOT
from app.pack import PackError, load_pack

PACK = ROOT / "packs" / "cps_interview_v1.yaml"


def write_variant(tmp_path, mutate):
    data = yaml.safe_load(PACK.read_text())
    mutate(data)
    p = tmp_path / "pack.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_shipped_pack_loads():
    pack = load_pack(PACK)
    assert pack.id == "cps_interview_v1"
    assert len(pack.checklist) == 11
    assert pack.checklist[0].id == "recording_consent"


def test_duplicate_topic_id_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["checklist"].append(dict(d["checklist"][1])))
    with pytest.raises(PackError, match="duplicate checklist id 'child_age_grade'"):
        load_pack(p)


def test_missing_topic_id_is_generated_from_the_label(tmp_path):
    """M7: an id is optional (made from the label), so a form field pointing at the old id now fails clearly."""
    p = write_variant(tmp_path, lambda d: d["checklist"][2].pop("id"))
    with pytest.raises(PackError, match="unknown topic 'parent_account'"):
        load_pack(p)
    p = write_variant(tmp_path, lambda d: (d["checklist"][2].pop("id"), d.update(form_schema=[])))
    assert load_pack(p).checklist[2].id == "parent_s_account_of_injury"


def test_missing_topic_label_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["checklist"][2].pop("label"))
    with pytest.raises(PackError, match=r"checklist\.2\.label: Field required"):
        load_pack(p)


def test_duplicate_form_field_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["form_schema"].append(dict(d["form_schema"][0])))
    with pytest.raises(PackError, match="duplicate form field id 'child_age'"):
        load_pack(p)


def test_unknown_source_topic_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["form_schema"][0].update(source_topic_ids=["nope"]))
    with pytest.raises(PackError, match="unknown topic 'nope'"):
        load_pack(p)


def test_choice_without_choices_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d["form_schema"][0].update(type="choice"))
    with pytest.raises(PackError, match="has no choices"):
        load_pack(p)


def test_empty_checklist_rejected(tmp_path):
    p = write_variant(tmp_path, lambda d: d.update(checklist=[], form_schema=[]))
    with pytest.raises(PackError, match="checklist is empty"):
        load_pack(p)


def test_bad_yaml_and_missing_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: [unclosed\n")
    with pytest.raises(PackError, match="not valid YAML"):
        load_pack(bad)
    with pytest.raises(PackError, match="not found"):
        load_pack(tmp_path / "missing.yaml")
