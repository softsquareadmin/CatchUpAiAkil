"""M10 item 3 (PAID, about $0.35; run only when asked): python -m tests.eval_job_audio [audio] [--label X]
The owner's job interview recording through the Audio file mode with the job interview pack and the configured real
models (speech, gate, cue writer, review), at real-time pace (about 6 minutes for test.mp3). The speaker mapping is
done the way a user would do it on screen: the first voice label heard is mapped to the interviewer (the role that
uses the tool), the second to the candidate; the mapping is printed so it can be checked against the transcript.
Nothing in the pack or prompts is tuned on this recording (hold-out). Results: sessions/eval-job-audio-<time>.json."""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from app.audio_file import decode
from app.config import ROOT, load_settings
from app.pack import load_pack
from app.pipeline import Session

AUDIO = ROOT / "tests" / "fixtures" / "audio" / "test.mp3"
PACK = ROOT / "packs" / "job_interview_v1.yaml"


async def main(audio: Path, label: str):
    pack = load_pack(PACK)
    settings = load_settings().model_copy(update={"experiment_label": label})
    sent = []

    async def send(m):
        sent.append(m)

    s = Session(settings, pack, ROOT / "sessions", send)
    if s.decider is None or s.analyst is None:
        raise SystemExit(f"off: gate '{s.gate_off_reason}' cue '{s.cue_off_reason}'")
    order = [pack.user_role.id] + pack.subject_ids  # interviewer, candidate
    mapped: dict[str, str] = {}
    real_speaker_for = s.speaker_for

    def speaker_for(lbl):  # map voices in the order they are first heard, as a user would on screen
        if lbl and lbl != "PENDING" and lbl not in mapped and len(mapped) < len(order):
            mapped[lbl] = order[len(mapped)]
            s.speaker_map[lbl] = mapped[lbl]
            s.audit.write("user", "speaker_set", label=lbl, speaker=mapped[lbl], by="eval script (first-heard order)")
        return real_speaker_for(lbl)
    s.speaker_for = speaker_for  # build_mic hands this to the transcriber
    pcm = await decode(audio.read_bytes(), audio.name)
    print(f"decoded {len(pcm) / 32000:.1f} s; streaming at 1x ...", flush=True)
    if err := await s.start_audio_file(pcm, audio.name):
        raise SystemExit(err)
    while s.file_playing():
        await asyncio.sleep(1)
    await s.gate_idle()
    await s.cue_idle()
    await s.end_interview()  # runs the post-interview review
    while s.review_state == "running":
        await asyncio.sleep(1)
    await s.cue_idle()
    await s.close()

    events = [json.loads(l) for l in (s.dir / "events.jsonl").read_text().splitlines()]
    rows = [json.loads(l) for l in (s.dir / "ledger.jsonl").read_text().splitlines()]
    labels = {e["utterance_id"]: e["label"] for e in events if e["kind"] == "stt_label"}
    result = {
        "session": s.id, "pack": pack.ref, "audio": audio.name, "seconds": round(len(pcm) / 32000, 1),
        "speaker_mapping": mapped,
        "utterances": [{**u.model_dump(include={"id", "speaker", "text", "t_start_ms"}), "label": labels.get(u.id)}
                       for u in s.store.utterances],
        "final": {c.id: {"label": c.label, **s.checklist[c.id].model_dump(include={"status", "follow_up",
                                                                                   "follow_up_reason", "evidence"})}
                  for c in pack.checklist},
        "cards": [m for m in sent if m["type"] == "cue"],
        "review": s.draft, "review_error": s.review_error,
        "ledger": rows,
    }
    out = ROOT / "sessions" / f"eval-job-audio-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(result, indent=1))

    print(f"\nspeaker mapping (first heard): {mapped}")
    for u in result["utterances"]:
        print(f"  {u['id']} {u['t_start_ms'] / 1000:6.1f}s [{u['label']}] {u['speaker']:11} {u['text'][:110]}")
    counts = {"covered": 0, "partial": 0, "not_covered": 0}
    print("\nchecklist:")
    for k, v in result["final"].items():
        counts[v["status"]] += 1
        q = "; ".join(f"{e['utterance_id']}: {e['quote'][:70]!r}" for e in v["evidence"][:2])
        print(f"  {v['label']:45} {v['status']:12} follow-up {v['follow_up']:9} {q}")
    score = round((counts["covered"] * 100 + counts["partial"] * 50) / len(pack.checklist))
    print(f"counts {counts}, coverage score {score}% (CatchUp's report on the same file: 2 covered, 2 partial, "
          f"1 not covered, 60%)")
    print("\ncards:", *(f"\n  [{c['cue']['kind']}] {c['cue']['topic_id']}: {c['cue']['question_or_note']}" for c in result["cards"]))
    if s.draft:
        print("\nreview sections:", [(sec["title"], sum(i["section"] == sec["id"] for i in s.draft["items"])) for sec in s.draft["sections"]])
        for i in s.draft["items"]:
            if i["section"] == "overall_assessment":
                print(f"  {i['label']}: {i['value'][:160]}")
    else:
        print("review failed:", s.review_error)
    by_role = {}
    for r in rows:
        x = by_role.setdefault(r["role"], {"calls": 0, "cost": 0.0, "lat": [], "sec": 0.0})
        x["calls"] += 1
        x["cost"] += r["cost_usd"]
        x["sec"] += r["audio_seconds"]
        if r["ok"] and r["latency_ms"]:
            x["lat"].append(r["latency_ms"])
    for role, x in by_role.items():
        lat = sorted(x["lat"])
        print(f"{role}: calls {x['calls']}, cost ${x['cost']:.4f}" + (f", p50 {lat[len(lat) // 2]} ms" if lat else "")
              + (f", {x['sec']:.0f} s connected" if x["sec"] else ""))
    print(f"total ${sum(r['cost_usd'] for r in rows):.4f}; results: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    label = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--label"), "m10-job-audio")
    asyncio.run(main(Path(args[0]) if args else AUDIO, label))
