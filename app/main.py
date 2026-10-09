"""FastAPI app and WebSocket. One interview session per browser; a reconnecting browser resumes its session
(`/ws?resume=<session_id>`) within RESUME_GRACE_S. Text frames are JSON messages; binary frames are mic audio."""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.adapters.transcribers.replay import load_script, parse_paste, paste_to_script, suggest_mapping
from app.audio_file import MAX_UPLOAD_BYTES, AudioFileError, decode, ffmpeg_path
from app.audit import AuditLog
from app.config import ROOT, SECRET_NAMES, Settings, load_settings
from app.import_template import TemplateError, convert
from app.pack import PackError, list_packs, load_pack, pack_from_dict
from app import pack_editor
from app import report as cost_report
from app.store import read_utterances
from app import review_actions
from app.export import build_report, load_session, render_html
from app.pipeline import Session, pack_info

STATIC = Path(__file__).parent / "static"
FIXTURE = ROOT / "tests" / "fixtures" / "script_v2.json"  # the CPS demo script (tests and comparison scripts)
PACKS_DIR = ROOT / "packs"
AUDIT_DIR = ROOT / "audit_logs"  # configuration changes (packs saved from the UI)
PACK_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.yaml$")
SPEEDS = (0, 1, 2)  # 0 = instant
SESSION_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
RESUME_GRACE_S = 120  # a dropped browser connection keeps its session this long

def create_app(settings: Settings | None = None, sessions_dir: Path = ROOT / "sessions") -> FastAPI:
    settings = settings or load_settings()
    pack = load_pack(ROOT / settings.pack)  # the default pack; raises PackError with a clear message if invalid
    default_file = (ROOT / settings.pack).name
    ffmpeg = ffmpeg_path()  # checked once at startup; the UI explains when it is missing (M8)

    def pack_for(file: str | None):
        """A pack from packs/ chosen in the UI (validated name, must load), else the default."""
        if not file or file == default_file or not PACK_FILE.match(file) or not (PACKS_DIR / file).is_file():
            return pack
        try:
            return load_pack(PACKS_DIR / file)
        except PackError:
            return pack

    def pack_by_ref(ref: str | None):
        """The pack a saved session used (for export after a restart), else the default."""
        if not ref or ref == pack.ref:
            return pack
        for row in list_packs(PACKS_DIR):
            if row["ok"] and f"{row['id']}@{row['version']}" == ref:
                return load_pack(PACKS_DIR / row["file"])
        return pack
    live: dict[str, tuple[Session, asyncio.Task | None]] = {}  # session id -> (session, pending close task)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for session, closer in list(live.values()):
            if closer:
                closer.cancel()
            await session.close()
        live.clear()

    app = FastAPI(title="Voice Case Notetaker PoC", lifespan=lifespan)
    app.state.settings, app.state.pack, app.state.live = settings, pack, live

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "pack": pack.ref,
            "experiment": settings.experiment_label,
            "gate_model": settings.models["gate"],
            "keys_present": {n: settings.has_key(n) for n in SECRET_NAMES},
            "ffmpeg": bool(ffmpeg),
        }

    # ---- pages (M12): plain HTML, each with its own script; shared header, tokens and components ----
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/review/{sid}")
    def review_page(sid: str):
        session_dir(sid)
        return FileResponse(STATIC / "review.html")

    @app.get("/config")
    def config_page():
        return FileResponse(STATIC / "config.html")

    @app.get("/sessions")
    def sessions_page():
        return FileResponse(STATIC / "sessions.html")

    @app.get("/costs")
    def costs_page():
        return FileResponse(STATIC / "costs.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/packs")
    def packs():
        """Pack picker: every packs/*.yaml (invalid ones listed with the reason) and the default."""
        return {"default": default_file, "packs": list_packs(PACKS_DIR)}

    # ---- configuration screen (M9) ----
    def raw_pack(file: str) -> dict:
        if not PACK_FILE.match(file or "") or not (PACKS_DIR / file).is_file():
            raise HTTPException(404, "unknown pack file")
        return pack_editor.read_raw(PACKS_DIR / file)

    @app.get("/api/packs/{file}/raw")
    def pack_raw(file: str):
        raw = raw_pack(file)
        roles = [r.get("id") for r in raw.get("roles") or [] if isinstance(r, dict)]
        return {"file": file, "raw": raw, "problems": pack_editor.problems_of(raw),
                "scripts": pack_editor.list_scripts(ROOT / "tests" / "fixtures", roles)}

    @app.post("/api/packs/validate")
    async def pack_validate(request: Request):
        body = await request.json()
        base = raw_pack(body["base_file"]) if body.get("base_file") else None
        return {"problems": pack_editor.problems_of(pack_editor.merge(base, body.get("pack") or {}))}

    @app.post("/api/packs/save")
    async def pack_save(request: Request):
        """Always a new file with a new version; the original is never changed."""
        body = await request.json()
        out = pack_editor.save_new(PACKS_DIR, body.get("base_file"), body.get("pack") or {}, body.get("version"),
                                   AUDIT_DIR)
        if not out["ok"]:
            return JSONResponse(out, status_code=422)
        return out

    @app.post("/api/packs/import")
    async def pack_import(request: Request):
        """A CatchUp template (JSON body) -> an unsaved pack plus the importer's warnings, for review in the editor."""
        try:
            pack, warnings = convert(await request.json())
        except (TemplateError, PackError, ValueError) as e:
            raise HTTPException(400, f"Not imported: {e}") from None
        taken = {r["id"] for r in list_packs(PACKS_DIR) if r["ok"]}
        if pack["id"] in taken:
            warnings.append(f"A pack with id '{pack['id']}' already exists; this one will be saved as a new version of it.")
        return {"pack": pack, "warnings": warnings}

    @app.post("/api/packs/test")
    async def pack_test(request: Request):
        """Test this template: replay a sample transcript instantly with the (possibly unsaved) pack. Offline models
        are free; real models need `confirm` after the estimate has been shown."""
        body = await request.json()
        base = raw_pack(body["base_file"]) if body.get("base_file") else None
        raw = pack_editor.merge(base, body.get("pack") or {})
        if problems := pack_editor.problems_of(raw):
            return JSONResponse({"ok": False, "problems": problems}, status_code=422)
        pk = pack_from_dict(raw, "edited pack")
        script = str(body.get("script") or "")
        if not PACK_FILE.match(script.replace(".json", ".yaml")) or not (ROOT / "tests" / "fixtures" / script).is_file():
            raise HTTPException(404, "unknown sample script")
        try:
            lines = load_script(ROOT / "tests" / "fixtures" / script, pk.role_ids)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        real = bool(body.get("real"))
        if real and not body.get("confirm"):
            return {"ok": True, "needs_confirm": True, "estimate": pack_editor.estimate_cost(
                sessions_dir, settings.models, len(lines), len(pk.checklist), settings.window_utterances)}
        return await pack_editor.run_template_test(settings, pk, lines, sessions_dir, real)

    def session_dir(sid: str) -> Path:
        d = Path(sessions_dir) / sid
        if not SESSION_ID.match(sid) or not d.is_dir():
            raise HTTPException(404, "unknown session")
        return d

    def jsonl(path: Path) -> list[dict]:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []

    @app.get("/api/sessions")
    def list_sessions():
        """Sessions page: newest first; only folders with an audit log (comparison runs write none)."""
        out = []
        for d in sorted((p for p in Path(sessions_dir).iterdir() if p.is_dir() and SESSION_ID.match(p.name)), reverse=True):
            audit = jsonl(d / "audit.jsonl")
            start = next((a for a in audit if a["action"] == "session_start"), None)
            if start is None:
                continue
            utts = read_utterances(d)
            has_review = (d / "draft_note.json").exists()
            decided = any(a["actor"] == "user" and (a["action"].startswith("review_") or
                                                   a["action"] == "topic_status_changed_by_user") for a in audit)
            running = d.name in live and not live[d.name][0].stopped
            status = ("running" if running else "reviewed" if decided else "review ready" if has_review
                      else "review failed" if any(a["action"] == "review_failed" for a in audit) else "ended")
            out.append({"id": d.name, "started_at": start["ts"], "pack": start["details"].get("pack"),
                        "experiment": start["details"].get("experiment"), "lines": len(utts),
                        "duration_s": max((u["t_end_ms"] for u in utts), default=0) // 1000, "status": status,
                        "has_review": has_review,
                        "exported": any(a["action"] == "report_exported" for a in audit),
                        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in jsonl(d / "ledger.jsonl")), 4)})
        return out

    @app.get("/api/sessions/{sid}/review")
    def get_review(sid: str):
        """Review page: the draft (live session first, else the saved one), the pack and the transcript."""
        d = session_dir(sid)
        if sid in live:
            s = live[sid][0]
            review, pk, utts = s.review_message(), s.pack, [u.model_dump() for u in s.store.utterances]
        else:
            draft = json.loads((d / "draft_note.json").read_text(encoding="utf-8")) if (d / "draft_note.json").exists() else None
            failed = next((a for a in jsonl(d / "audit.jsonl") if a["action"] == "review_failed"), None)
            start = next((a for a in jsonl(d / "audit.jsonl") if a["action"] == "session_start"), {"details": {}})
            pk = pack_by_ref(draft["pack"] if draft else start["details"].get("pack"))
            review = {"state": "ready" if draft else "failed" if failed else "none", "draft": draft,
                      "model": (draft or {}).get("model"), "error": (failed or {}).get("details", {}).get("error")}
            utts = read_utterances(d)
        return {"session_id": sid, "live": sid in live, "review": review, "pack": pack_info(pk), "utterances": utts}

    @app.post("/api/sessions/{sid}/review")
    async def post_review(sid: str, request: Request):
        """One review decision ({kind: item | topic | status | viewed, ...}); returns the updated review."""
        d = session_dir(sid)
        body = await request.json()
        if sid in live:
            s = live[sid][0]
            if body.get("kind") == "viewed":
                s.report_viewed("review_page")
                return {"ok": True}
            if err := await s.review_decision(body):
                raise HTTPException(400, err)
            return {"ok": True, "review": s.review_message()}
        if not (d / "draft_note.json").exists():
            raise HTTPException(409, "no review yet")
        draft = json.loads((d / "draft_note.json").read_text(encoding="utf-8"))
        audit = AuditLog(d, sid)
        if body.get("kind") == "viewed":
            audit.write("user", "report_viewed", where="review_page", topics=len(draft.get("topics", [])))
            return {"ok": True}
        err, _ = review_actions.apply(draft, audit, pack_by_ref(draft.get("pack")), body)
        if err:
            raise HTTPException(400, err)
        (d / "draft_note.json").write_text(json.dumps(draft, indent=1), encoding="utf-8")
        return {"ok": True, "review": {"state": "ready", "draft": draft, "model": draft.get("model"), "error": None}}

    @app.get("/api/costs")
    def costs(experiment: str | None = None):
        """Cost report page: the ledger report (app/report.py) with the experiment filter."""
        rows, totals = cost_report.build(Path(sessions_dir), experiment or None)
        _, everything = cost_report.build(Path(sessions_dir)) if experiment else (rows, totals)
        return {"rows": rows, "totals": totals, "experiments": [t["experiment"] for t in everything],
                "health": cost_report.session_health(Path(sessions_dir), experiment or None)}

    @app.get("/api/sessions/{sid}/audit")
    def audit_trail(sid: str):
        path = session_dir(sid) / "audit.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    @app.get("/api/sessions/{sid}/export.{fmt}")
    def export(sid: str, fmt: str, unreviewed: bool = False, transcript: bool = False, cost: bool = False,
               reviewer: str = "", view: bool = False):
        """The report: only items the user accepted or edited, unless `unreviewed` (then marked "Not reviewed").
        Transcript appendix and cost only on request. `view` opens the HTML in the browser instead of downloading.
        Writes a copy to the session folder (export.json, report.html)."""
        if fmt not in ("json", "html"):
            raise HTTPException(404, "format must be json or html")
        d = session_dir(sid)
        if not (d / "draft_note.json").exists():
            raise HTTPException(409, "no review yet")
        data = load_session(d)
        pk = live[sid][0].pack if sid in live else pack_by_ref(data["draft"].get("pack"))
        report = build_report(data, pk, include_unreviewed=unreviewed, transcript=transcript, cost=cost,
                              reviewer=reviewer[:120])
        audit = AuditLog(d, sid)
        if view and fmt == "html":
            audit.write("user", "report_viewed", where="html")
        else:
            audit.write("user", "report_exported", format=fmt, included_unreviewed=unreviewed,
                        transcript_appendix=transcript, cost=cost, topics=len(report["topics"]),
                        items=sum(len(s["items"]) for s in report["custom_and_other_sections"]),
                        not_included=report["not_included"])
        name = f"{pk.id}-report-{sid}.{fmt}"
        headers = {} if view else {"Content-Disposition": f'attachment; filename="{name}"'}
        if fmt == "json":
            (d / "export.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
            return JSONResponse(report, headers=headers)
        page = render_html(report)
        (d / "report.html").write_text(page, encoding="utf-8")
        return HTMLResponse(page, headers=headers)

    @app.post("/api/sessions/{sid}/audio")
    async def audio_file(sid: str, request: Request, name: str = ""):
        """Audio file mode: the raw file is the request body. Decoded to PCM, then streamed at 1x by the session."""
        if sid not in live:
            raise HTTPException(404, "unknown or closed session")
        session = live[sid][0]
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        try:
            pcm = await decode(await request.body(), name, ffmpeg)
        except AudioFileError as e:
            session.audit.write("system", "audio_file_rejected", name=Path(name).name[:120], reason=str(e))
            raise HTTPException(400, str(e)) from None
        if error := await session.start_audio_file(pcm, name):
            raise HTTPException(409, error)
        return {"ok": True, "seconds": round(len(pcm) / 32000, 1)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, resume: str | None = None, pack: str | None = None):
        await websocket.accept()
        resume_failed = resume if resume and resume not in live else None
        if resume in live:
            session, closer = live[resume]
            if closer:
                closer.cancel()
            session.send = websocket.send_json
            session.audit.write("system", "browser_reconnected")
        else:
            session = Session(settings, pack_for(pack), sessions_dir, websocket.send_json)
            session.pack_file = pack if pack_for(pack) is not app.state.pack else default_file
            await session.start_typed()
        live[session.id] = (session, None)
        connection = session.connection = object()
        # resume_failed: the browser asked for a session the server no longer holds (closed after RESUME_GRACE_S or a
        # restart); its files stay on the Sessions page, and this is a new interview
        await websocket.send_json({**session.hello(), "ffmpeg": bool(ffmpeg), "resume_failed": resume_failed})
        try:
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                if frame.get("bytes") is not None:
                    await session.mic_audio(frame["bytes"])
                    continue
                try:
                    msg = json.loads(frame.get("text") or "")
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "messages must be JSON"})
                    continue
                if error := await handle(session, msg):
                    await websocket.send_json({"type": "error", "message": error})
        except WebSocketDisconnect:
            pass
        finally:
            await detach(session, connection)

    async def detach(session: Session, connection: object) -> None:
        """Browser gone: stop the mic (AssemblyAI bills idle time), keep the session for a while in case it comes back.
        Does nothing if a newer connection has already resumed the session."""
        if session.connection is not connection:
            return

        async def drop(m):
            pass
        session.send = drop
        session.audit.write("system", "browser_disconnected")
        await session.stop_audio_file()  # no one is listening, and AssemblyAI bills connected time
        await session.stop_mic()
        if session.connection is not connection:  # resumed while the mic was stopping
            return
        if not session.started_input() and session.review_state == "none":  # e.g. the user switched packs
            live.pop(session.id, None)
            await session.close()
            return

        async def close_later():
            await asyncio.sleep(RESUME_GRACE_S)
            while session.review_state == "running":  # the review page may be waiting for it (M12)
                await asyncio.sleep(1)
            live.pop(session.id, None)
            await session.close()
        live[session.id] = (session, asyncio.create_task(close_later()))

    return app


async def handle(session: Session, msg: dict) -> str | None:
    """Apply one client message. Returns an error message, or None."""
    kind = msg.get("type")
    speed = msg.get("speed", 1)
    if kind in ("replay", "paste_play") and speed not in SPEEDS:
        return f"speed must be one of {SPEEDS}"
    if kind == "start":
        if session.stopped:
            return "This interview has ended. Use New interview."
        await session.begin()
    elif kind == "mode":
        session.set_mode(str(msg.get("mode")))
    elif kind == "replay":
        if not session.pack.sample_script:
            return "This pack has no sample script to replay. Use Typed or Paste."
        try:
            lines = load_script(ROOT / session.pack.sample_script, session.pack.role_ids)
        except (OSError, ValueError) as e:
            return f"Sample script cannot be played: {e}"
        return await session.start_replay(lines, speed, "replay")
    elif kind == "stop":
        await session.stop_replay()
    elif kind == "typed":
        return await session.submit_typed(msg["speaker"], str(msg.get("text", "")))
    elif kind == "paste_parse":
        _, labels = parse_paste(str(msg.get("text", "")))
        await session.send({"type": "paste_labels", "labels": labels,
                            "mapping": suggest_mapping(labels, session.pack.roles)})
    elif kind == "paste_play":
        turns, _ = parse_paste(str(msg.get("text", "")))
        if not turns:
            return "Nothing to play: paste lines like 'Speaker: text'"
        return await session.start_replay(paste_to_script(turns, msg.get("mapping") or {}, session.pack.role_ids),
                                          speed, "paste")
    elif kind == "cue_action":
        return await session.cue_action(str(msg.get("id")), str(msg.get("action")))
    elif kind == "note":
        return await session.add_note(str(msg.get("text", "")))
    elif kind == "consent":
        await session.consent_decision(stop=msg.get("decision") == "stop")
    elif kind == "end":
        await session.before_leave("end_button")
    elif kind == "end_confirm":
        await session.end_interview()
    elif kind == "experiment":
        return await session.set_experiment(str(msg.get("label", "")))
    elif kind == "review_action":
        return await session.review_action(str(msg.get("id")), str(msg.get("action")), msg.get("value"))
    elif kind == "topic_action":
        return await session.topic_action(str(msg.get("topic_id")), str(msg.get("part")), str(msg.get("action")),
                                          msg.get("value"))
    elif kind == "topic_status":
        return await session.topic_status(str(msg.get("topic_id")), str(msg.get("status")))
    elif kind == "report_viewed":
        session.report_viewed("review_screen")
    elif kind == "mic_start":
        return await session.start_mic()
    elif kind == "mic_stop":
        await session.stop_mic()
    elif kind == "file_stop":
        await session.stop_audio_file()
    elif kind == "line_speaker":
        return await session.set_line_speaker(str(msg.get("id")), str(msg.get("speaker")))
    elif kind == "speaker":
        return await session.set_speaker(msg.get("label"), msg.get("speaker"))
    else:
        return f"unknown message type: {kind}"
    return None


app = create_app()
