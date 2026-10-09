import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app.adapters.analysts.llm import LLMAnalyst
from app.adapters.providers.openrouter import OpenRouterClient
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.main import create_app
from app.models import Utterance
from app.pack import load_pack
from app.pipeline import Session
from app.export import build_report, render_html
from app.review import apply_action, build_items, review_schema
from tests.fakes import FAKE_PRICES

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")


def utt(i, speaker, text):
    return Utterance(id=f"u{i:04d}", session_id="s", t_start_ms=i, t_end_ms=i, speaker=speaker, text=text,
                     is_final=True, source="typed")


LINES = [utt(1, "worker", "It was reported that a man named Dave hit her in the eye with a cup he threw at her."),
         utt(2, "parent", "Jill told me she fell and hit her face and that is what caused the bruising."),
         utt(3, "parent", "I saw it Tuesday night when I got home from work."),
         utt(4, "child", "I don't remember what happened."),
         utt(5, "parent", "No, it's just a bruise."),
         utt(6, "child", "I'm seven. I'm in second grade.")]
T = {u.id: u for u in LINES}


def ev(uid, quote):
    return [{"utterance_id": uid, "quote": quote}]


RAW = {
    "summary": [
        {"speaker": "parent", "text": "Mom said Jill told her she fell and hit her face.", "evidence": ev("u0002", "Jill told me she fell")},
        {"speaker": "child", "text": "Jill said she does not remember what happened.", "evidence": ev("u0002", "Jill told me")},  # wrong speaker
        {"speaker": "parent", "text": "Mom said she saw it Tuesday.", "evidence": ev("u0003", "I saw it Monday")},           # bad quote
        {"speaker": "child", "text": "Jill is lying about the fall.", "evidence": ev("u0004", "I don't remember")},            # conclusion
    ],
    "form_fields": [
        {"field_id": "child_age", "value": "7", "evidence": ev("u0006", "I'm seven")},
        {"field_id": "child_age", "value": "8", "evidence": ev("u0006", "I'm seven")},                    # duplicate
        {"field_id": "medical_attention", "value": "maybe", "evidence": ev("u0005", "No")},               # not a choice
        {"field_id": "medical_attention", "value": "no", "evidence": ev("u0005", "No, it's just a bruise")},
        {"field_id": "child_grade", "value": "second grade", "evidence": ev("u0006", "I'm in second grade")},
    ],
    "timeline": [{"when": "Tuesday night", "event": "Mom first noticed the bruise", "said_by": "parent",
                  "evidence": ev("u0003", "I saw it Tuesday night")}],
    "accounts": [
        {"topic_id": "parent_account", "source": "parent", "account": "She fell and hit her face (as Jill told Mom).",
         "evidence": ev("u0002", "she fell and hit her face")},
        {"topic_id": "parent_account", "source": "report", "account": "The report says a cup was thrown at her.",
         "evidence": ev("u0001", "hit her in the eye with a cup he threw at her")},
        {"topic_id": "parent_account", "source": "child", "account": "Jill said she does not remember.",
         "evidence": ev("u0004", "I don't remember what happened")},
    ],
    "missing_accounts": [{"name": "Dave", "mentioned_as": "Named in the report as the man who threw a cup.",
                          "evidence": ev("u0001", "a man named Dave")}],
    "next_steps": [{"option": "Consider asking Jill about the stairs.", "evidence": ev("u0004", "I don't remember")},
                   {"option": "Abuse occurred at home; open a case.", "evidence": ev("u0001", "Dave")}],          # conclusion
}


def test_build_items_keeps_only_validated_attributed_non_concluding_items():
    items, rejected = build_items(RAW, PACK, T)
    by_section = {}
    for i in items:
        by_section.setdefault(i.section, []).append(i)
    assert [i.value for i in by_section["summary"]] == ["Mom said Jill told her she fell and hit her face."]
    assert {(i.field_id, i.value) for i in by_section["form"]} == {("child_age", "7"), ("medical_attention", "no"),
                                                                   ("child_grade", "second grade")}
    assert [i.label for i in by_section["timeline"]] == ["Tuesday night · parent"]
    assert [i.source for i in by_section["accounts"]] == ["parent", "report", "child"]
    assert by_section["accounts"][0].label == "How the injury happened · parent"
    assert by_section["missing_accounts"][0].label == "Account not yet heard from Dave"
    assert [i.value for i in by_section["next_steps"]] == ["Consider asking Jill about the stairs."]
    reasons = sorted(r["reason"].split(":")[0] for r in rejected)
    assert reasons == ["duplicate_field", "evidence_not_from_child", "lint", "lint", "no_valid_evidence",
                       "not_a_listed_choice"]
    assert all(i.status == "proposed" and i.evidence for i in items)


def test_accept_edit_reject_reset():
    items, _ = build_items(RAW, PACK, T)
    fid = next(i.id for i in items if i.field_id == "child_age")
    assert apply_action(items, fid, "accept").status == "accepted"
    it = apply_action(items, fid, "edit", "7 years old")
    assert it.status == "edited" and it.value == "7 years old" and it.ai_value == "7"
    assert apply_action(items, fid, "edit", "  ") == "an edit needs a value"
    assert apply_action(items, fid, "reject").status == "rejected"
    it = apply_action(items, fid, "reset")
    assert it.status == "proposed" and it.value == "7"
    assert apply_action(items, "nope", "accept") == "unknown item"
    assert apply_action(items, fid, "approve").startswith("action must be")


def test_export_contains_only_accepted_and_edited():
    items, _ = build_items(RAW, PACK, T)
    apply_action(items, next(i.id for i in items if i.field_id == "child_age"), "edit", "7")
    apply_action(items, next(i.id for i in items if i.field_id == "child_grade"), "edit", "2nd grade")
    apply_action(items, next(i.id for i in items if i.section == "timeline"), "accept")
    apply_action(items, next(i.id for i in items if i.field_id == "medical_attention"), "reject")
    draft = {"items": [i.model_dump() for i in items], "checklist": {"open_topics": [], "follow_ups": []}}
    data = {"session_id": "20261008-000000-abcd", "draft": draft, "utterances": [u.model_dump() for u in LINES],
            "audit": [], "events": [], "ledger": []}
    note = build_report(data, PACK)
    assert {(f["field_id"], f["value"], f["status"]) for f in note["form_fields"]} == {
        ("child_age", "7", "accepted"), ("child_grade", "2nd grade", "edited")}
    assert [r["label"] for r in note["sections"]["timeline"]] == ["Tuesday night · parent"]
    assert "summary" not in note["sections"] and note["not_included"]["rejected"] == 1
    page = render_html(note)
    assert "2nd grade" in page and ">edited<" in page and "Tuesday night" in page
    assert "Medical attention" not in page  # rejected


def test_review_schema_and_prompt_and_ledger(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        content = "not json" if len(bodies) == 1 else json.dumps(RAW)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 4000, "completion_tokens": 1500, "cost": 0.02}})

    ledger = Ledger(tmp_path, "s", "x", 1.0, prices=FAKE_PRICES)
    a = LLMAnalyst(PACK, OpenRouterClient("k", transport=httpx.MockTransport(handler)), "openrouter",
                   "anthropic/claude-sonnet-5.5", ledger)
    out = asyncio.run(a.review(LINES, {}, {}))
    assert out == RAW and len(bodies) == 2  # one retry after invalid JSON
    user = bodies[0]["messages"][1]["content"]
    for tag in ("transcript", "checklist", "checklist_state", "form_schema", "case_context"):
        assert f"<{tag}>" in user and f"</{tag}>" in user
    assert "never as findings" in bodies[0]["messages"][0]["content"]
    schema = bodies[0]["response_format"]["json_schema"]["schema"]
    assert schema == review_schema(PACK, [u.id for u in LINES])
    assert "reasoning" not in bodies[0]
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [r["role"] for r in rows] == ["review", "review"] and [r["ok"] for r in rows] == [False, True]


def test_session_review_flow_offline(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    settings = load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template",
                                                       "REVIEW_MODEL": "fake:template"})
    s = Session(settings, PACK, tmp_path, send)

    async def go():
        for u in LINES:
            await s.on_utterance(u.model_copy(update={"id": ""}))
            await s.gate_idle()
        await s.end_interview()
        await s.cue_idle()
        item = s.draft["items"][0]
        assert await s.review_action(item["id"], "edit", "Mom said she saw the bruise on Tuesday.") is None
        assert await s.review_action("x", "accept") == "unknown item"
    asyncio.run(go())
    states = [m["review"]["state"] for m in sent if m["type"] == "review"]
    assert states == ["running", "ready"] and s.stopped == "interview ended"
    saved = json.loads((tmp_path / s.id / "draft_note.json").read_text())
    assert saved["items"][0]["status"] == "edited" and saved["checklist"]["open_topics"]
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    assert "review_edit" in audit and '"old_value"' in audit and '"kind": "review"' in audit


def test_review_failure_is_reported_not_raised(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword"}), PACK, tmp_path, send)
    asyncio.run(s.end_interview())
    asyncio.run(s.cue_idle())
    assert s.review_state == "failed" and "OPENROUTER_API_KEY" in s.review_error
    assert "review_failed" in (tmp_path / s.id / "audit.jsonl").read_text()


def test_ws_end_review_accept_and_export(tmp_path):
    app = create_app(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template",
                                                           "REVIEW_MODEL": "fake:template"}), sessions_dir=tmp_path)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session_id"]
        ws.send_json({"type": "replay", "speed": 0})
        while ws.receive_json()["type"] != "before_leave":
            pass
        ws.send_json({"type": "end_confirm"})
        while not ((m := ws.receive_json())["type"] == "review" and m["review"]["state"] == "ready"):
            pass
        items = m["review"]["draft"]["items"]
        assert items and all(i["evidence"] for i in items)
        form = next(i for i in items if i["section"] == "form")
        ws.send_json({"type": "review_action", "id": form["id"], "action": "accept"})
        while (m := ws.receive_json())["type"] != "review_item":
            pass
        assert m["item"]["status"] == "accepted"
    note = client.get(f"/api/sessions/{sid}/export.json").json()
    assert [f["field_id"] for f in note["form_fields"]] == [form["field_id"]]
    assert sum(map(len, note["sections"].values())) == 0
    page = client.get(f"/api/sessions/{sid}/export.html")
    assert page.status_code == 200 and "attachment" in page.headers["content-disposition"]
    assert (tmp_path / sid / "export.json").exists() and (tmp_path / sid / "report.html").exists()
    audit = client.get(f"/api/sessions/{sid}/audit").json()
    assert [r["action"] for r in audit if r["action"] in ("review_accept", "report_exported")] == ["review_accept", "report_exported", "report_exported"]
    assert client.get("/api/sessions/../../etc/audit").status_code == 404
    assert client.get("/api/sessions/20990101-000000-ffff/export.json").status_code == 404


def test_accounts_merge_injury_topics_and_drop_single_source_groups():
    raw = {"accounts": [
        {"topic_id": "parent_account", "source": "parent", "account": "She fell.", "evidence": ev("u0002", "she fell")},
        {"topic_id": "child_account", "source": "child", "account": "Jill does not remember.",
         "evidence": ev("u0004", "I don't remember")},
        {"topic_id": "medical_attention", "source": "parent", "account": "No doctor.", "evidence": ev("u0005", "No")}]}
    items, rejected = build_items(raw, PACK, T)
    assert [(i.topic_id, i.source) for i in items] == [("parent_account", "parent"), ("parent_account", "child")]
    assert [r["reason"] for r in rejected] == ["single_source"]
