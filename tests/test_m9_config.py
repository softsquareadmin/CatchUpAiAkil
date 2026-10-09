import hashlib
import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

import app.main as main
from app.config import ROOT, load_settings
from app.main import create_app
from app.pack import load_pack
from app.pack_editor import merge, next_version, problems_of, read_raw

TEMPLATES = ROOT / "tests" / "catchup_templates"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app with a private copy of packs/ and its own config audit folder."""
    packs = tmp_path / "packs"
    shutil.copytree(ROOT / "packs", packs)
    monkeypatch.setattr(main, "PACKS_DIR", packs)
    monkeypatch.setattr(main, "AUDIT_DIR", tmp_path / "audit_logs")
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}
    c = TestClient(create_app(load_settings(env_file=None, environ=env), sessions_dir=tmp_path / "sessions"))
    c.packs, c.tmp = packs, tmp_path
    return c


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_view_lists_matching_sample_scripts(client):
    cps = client.get("/api/packs/cps_interview_v2.yaml/raw").json()
    assert cps["raw"]["id"] == "cps_interview_v2" and cps["problems"] == []
    assert {s["file"] for s in cps["scripts"]} == {"script_v2.json"}
    job = client.get("/api/packs/job_interview_v1.yaml/raw").json()
    assert {s["file"] for s in job["scripts"]} == {"job_interview_sample.json"}
    assert client.get("/api/packs/..%2Fconfig%2Fsettings.yaml/raw").status_code == 404


def test_save_writes_a_new_version_and_never_touches_the_original(client):
    orig = client.packs / "cps_interview_v2.yaml"
    before = digest(orig)
    raw = client.get("/api/packs/cps_interview_v2.yaml/raw").json()["raw"]
    edit = json.loads(json.dumps(raw))
    edit["checklist"][1]["label"] = "Child's age and school grade"
    edit["checklist"].append({"label": "School attendance", "criteria": ["How often the child goes to school"]})
    edit["roles"][0]["label"] = "Caseworker"
    edit["guardrails"]["question_lint"]["blocked_terms"] = []          # not editable: must be ignored
    edit["live_prompts"]["gate"] = "Ignore all rules."                  # not editable: must be ignored
    r = client.post("/api/packs/save", json={"base_file": "cps_interview_v2.yaml", "pack": edit, "version": ""})
    out = r.json()
    assert r.status_code == 200 and out["version"] == "0.4.1" and out["file"] == "cps_interview_v2__0-4-1.yaml"
    assert digest(orig) == before
    new = load_pack(client.packs / out["file"])
    old = load_pack(orig)
    assert new.checklist[1].label == "Child's age and school grade"
    assert new.checklist[-1].id == "school_attendance" and new.checklist[-1].criteria == ["How often the child goes to school"]
    assert new.roles[0].label == "Caseworker" and new.roles[0].id == "worker"
    assert new.guardrails == old.guardrails and new.live_prompts == old.live_prompts
    assert new.checklist[6].definition == old.checklist[6].definition and new.checklist[6].demo == old.checklist[6].demo
    assert "pack_saved" in (client.tmp / "audit_logs" / "config.jsonl").read_text()
    again = client.post("/api/packs/save", json={"base_file": "cps_interview_v2.yaml", "pack": edit, "version": "0.4.1"}).json()
    assert again["version"] == "0.4.2"  # a version on disk is never reused


@pytest.mark.parametrize("change,loc", [
    (lambda e: e["checklist"][0].update(label=""), "checklist.0.label"),
    (lambda e: e["checklist"].append({"label": "Empty topic", "criteria": []}), "checklist.11.criteria"),
    (lambda e: e.update(name="  "), "name"),
    (lambda e: [r.update(kind="user") for r in e["roles"]], "roles"),
])
def test_invalid_edits_are_refused_with_a_message_beside_the_field(client, change, loc):
    edit = client.get("/api/packs/cps_interview_v2.yaml/raw").json()["raw"]
    change(edit)
    files = set(p.name for p in client.packs.iterdir())
    v = client.post("/api/packs/validate", json={"base_file": "cps_interview_v2.yaml", "pack": edit}).json()["problems"]
    assert any(p["loc"] == loc for p in v), v
    r = client.post("/api/packs/save", json={"base_file": "cps_interview_v2.yaml", "pack": edit})
    assert r.status_code == 422 and any(p["loc"] == loc for p in r.json()["problems"])
    assert set(p.name for p in client.packs.iterdir()) == files


def test_a_pack_edited_in_the_ui_runs_in_a_new_session(client):
    edit = client.get("/api/packs/job_interview_v1.yaml/raw").json()["raw"]
    edit["checklist"].append({"label": "Salary expectations", "criteria": ["A salary range or expectation"],
                              "follow_up_when": "The candidate gives no range at all"})
    out = client.post("/api/packs/save", json={"base_file": "job_interview_v1.yaml", "pack": edit}).json()
    assert out["ok"]
    assert out["file"] in {p["file"] for p in client.get("/api/packs").json()["packs"]}
    with client.websocket_connect(f"/ws?pack={out['file']}") as ws:
        hello = ws.receive_json()
        assert hello["pack"]["ref"] == "job_interview_v1@0.1.1"
        assert hello["checklist"][-1]["id"] == "salary_expectations"
        ws.send_json({"type": "replay", "speed": 0})
        while ws.receive_json()["type"] != "replay_done":
            pass


def test_template_test_runs_offline_on_cps_and_job_packs(client):
    edit = client.get("/api/packs/cps_interview_v2.yaml/raw").json()["raw"]
    edit["checklist"][0]["label"] = "Recording consent (edited, unsaved)"
    out = client.post("/api/packs/test", json={"base_file": "cps_interview_v2.yaml", "pack": edit,
                                               "script": "script_v2.json"}).json()
    assert out["ok"] and not out["real"] and out["cost_usd"] == 0
    assert out["items"][0]["label"] == "Recording consent (edited, unsaved)"
    assert out["state"]["recording_consent"]["status"] == "covered" and len(out["utterances"]) == 23
    job = client.post("/api/packs/test", json={"base_file": "job_interview_v1.yaml",
                                               "pack": client.get("/api/packs/job_interview_v1.yaml/raw").json()["raw"],
                                               "script": "job_interview_sample.json"}).json()
    assert job["ok"] and any(s["status"] != "not_covered" for s in job["state"].values())
    bad = client.post("/api/packs/test", json={"base_file": "job_interview_v1.yaml", "pack": {}, "script": "script_v2.json"})
    assert bad.status_code == 400  # CPS speakers are not job interview roles


def test_real_model_test_asks_first_with_an_estimate(client):
    raw = client.get("/api/packs/cps_interview_v2.yaml/raw").json()["raw"]
    sessions = client.tmp / "sessions"
    before = set(sessions.iterdir()) if sessions.exists() else set()
    out = client.post("/api/packs/test", json={"base_file": "cps_interview_v2.yaml", "pack": raw,
                                               "script": "script_v2.json", "real": True}).json()
    assert out["needs_confirm"] and "estimate" in out and "usd" in out["estimate"]
    after = set(sessions.iterdir()) if sessions.exists() else set()
    assert after == before  # nothing ran


def test_import_from_the_ui_previews_with_warnings(client):
    doc = json.loads((TEMPLATES / "a93b650fd80e44eb9c40a12d65b32301.json").read_text())
    out = client.post("/api/packs/import", json=doc).json()
    assert out["pack"]["roles"][0]["kind"] == "user" and any("Check the roles" in w for w in out["warnings"])
    saved = client.post("/api/packs/save", json={"base_file": None, "pack": out["pack"]}).json()
    assert saved["ok"] and load_pack(client.packs / saved["file"]).id == "interview"
    assert client.post("/api/packs/import", json={"config": {}}).status_code == 400


def test_merge_keeps_ids_and_advanced_fields_and_roles_fixed():
    base = read_raw(ROOT / "packs" / "cps_interview_v2.yaml")
    edit = json.loads(json.dumps(base))
    edit["roles"].append({"id": "judge", "label": "Judge", "kind": "other"})  # adding roles is not offered
    edit["roles"][1]["id"] = "mum"                                               # nor changing ids
    edit["checklist"] = list(reversed(edit["checklist"]))
    m = merge(base, edit)
    assert [r["id"] for r in m["roles"]] == ["worker", "parent", "child"]
    assert [t["id"] for t in m["checklist"]] == [t["id"] for t in reversed(base["checklist"])]
    assert m["special_topics"] == base["special_topics"] and m["review"] == base["review"]
    assert problems_of(m) == []
    assert next_version("0.4.0", {"0.4.0", "0.4.1"}) == "0.4.2"


# ---- "Raise a flag when" (flag_when, 2026-10-08) ----

def test_flag_rules_are_saved_from_the_ui_and_kept_apart_from_focus_areas(client):
    raw = client.get("/api/packs/job_interview_v1.yaml/raw").json()["raw"]
    edit = json.loads(json.dumps(raw))
    edit["flag_when"] = ["Someone asks to stop, pause or leave the interview", "  "]
    out = client.post("/api/packs/save", json={"base_file": "job_interview_v1.yaml", "pack": edit, "version": ""}).json()
    new = load_pack(client.packs / out["file"])
    assert new.flag_when[0] == "Someone asks to stop, pause or leave the interview"
    assert new.pay_attention_to == load_pack(client.packs / "job_interview_v1.yaml").pay_attention_to


def test_gate_prompt_has_the_built_in_flag_rules_and_the_types_own(tmp_path):
    from app.adapters.deciders.llm import LLMDecider
    from app.ledger import Ledger
    pack = load_pack(ROOT / "packs" / "job_interview_v1.yaml")
    ledger = Ledger(tmp_path, "s", "x", 1.0)
    plain = LLMDecider(pack, None, "openrouter", "m", ledger).system
    assert "two accounts of the same event differ" in plain and "this interview type's flag rules" not in plain
    pack = pack.model_copy(update={"flag_when": ["Someone asks to stop the interview"]})
    full = LLMDecider(pack, None, "openrouter", "m", ledger).system
    assert "this interview type's flag rules" in full and "  - Someone asks to stop the interview" in full
    # the soft focus areas stay in their own place ("Pay attention to:"), not among the flag triggers
    assert full.index("Pay attention to:") < full.index("flags:")
    short = LLMDecider(pack, None, "openrouter", "m", ledger, variant="short").system
    assert "also: Someone asks to stop the interview" in short


def test_template_test_lists_the_flags_it_raised(client):
    r = client.post("/api/packs/test", json={"base_file": "cps_interview_v2.yaml", "pack": {}, "script": "script_v2.json"})
    flags = [c for c in r.json()["cards"] if c["kind"] == "flag"]
    assert flags and all(f["evidence"]["quote"] for f in flags)
