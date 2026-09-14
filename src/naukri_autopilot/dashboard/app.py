"""Local dashboard. Binds 127.0.0.1 only.

It is unauthenticated by design, which is only safe because it is not reachable
off-box. Never bind 0.0.0.0 here: the page can trigger a browser run and read
back screenshots of the user's profile.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, diagnostics, lock, scheduling, store
from ..results import RunResult, Status, Trigger
from ..scheduler import VALID_INTERVALS, decide
from . import chart

HERE = Path(__file__).parent
HOST = "127.0.0.1"
PORT = 8765

templates = Jinja2Templates(directory=str(HERE / "templates"))

# A run takes ~30s of browser time, far too long to hold an HTTP request open.
# The work happens on a thread and the page polls /api/status.
_active = {"running": False, "started": None, "kind": None}
_active_guard = threading.Lock()


def create_app() -> FastAPI:
    app = FastAPI(title="Naukri Autopilot", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # -- helpers ---------------------------------------------------------- #

    def snapshot():
        conn = store.connect()
        try:
            now = datetime.now(timezone.utc)
            state = store.sched_state(conn)
            settings = store.settings(conn)
            decision = decide(now, state, settings)
            rows = store.recent(conn, limit=200)
            by_day = chart.bucket_by_day([(r["started_at"], r["status"]) for r in rows])
            stale_after = timedelta(hours=settings.interval_hours * 2)
            return {
                "now": now,
                "state": state,
                "settings": settings,
                "decision": decision,
                "rows": rows,
                "by_day": by_day,
                "streak": chart.streak(by_day),
                "resume_path": store.get(conn, "resume_path") or "",
                "headed_mode": store.get_int(conn, "headed_mode", 0),
                "needs_login": state.last_status == Status.NEEDS_LOGIN,
                "stale": (
                    state.last_success_at is not None
                    and now - state.last_success_at > stale_after
                ),
                "never_run": state.last_success_at is None,
                "task": scheduling.query(),
            }
        finally:
            conn.close()

    def start_run(dry_run: bool) -> bool:
        """Kick a run onto a thread. False if one is already going."""
        with _active_guard:
            if _active["running"]:
                return False
            _active.update(running=True, started=datetime.now(timezone.utc),
                           kind="dry" if dry_run else "real")

        def work():
            from ..runner import run_once

            conn = store.connect()
            try:
                resume = store.get(conn, "resume_path") or ""
                if not resume:
                    r = RunResult(status=Status.FAILED, trigger=Trigger.MANUAL)
                    store.record(conn, r.finish(
                        Status.FAILED, error_kind="RESUME_MISSING",
                        error_detail="no resume configured"))
                    return
                with lock.exclusive() as acquired:
                    if not acquired:
                        r = RunResult(status=Status.SKIPPED_LOCKED, trigger=Trigger.MANUAL)
                        store.record(conn, r.finish(Status.SKIPPED_LOCKED))
                        return
                    result = run_once(
                        resume_path=resume,
                        trigger=Trigger.MANUAL,
                        dry_run=dry_run,
                        offscreen=store.get_int(conn, "headed_mode", 0) == 0,
                    )
                store.record(conn, result)
                store.prune_screenshots(conn)
            finally:
                conn.close()
                with _active_guard:
                    _active.update(running=False, started=None, kind=None)

        threading.Thread(target=work, daemon=True).start()
        return True

    # -- routes ----------------------------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        data = snapshot()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "d": data,
                "grid": chart.render([(r["started_at"], r["status"]) for r in data["rows"]]),
                "legend": chart.legend(),
                "intervals": VALID_INTERVALS,
                "checks": diagnostics.run_all(),
                "diag": diagnostics,
                "active": dict(_active),
                "task_name": scheduling.TASK_NAME,
                "tick_minutes": scheduling.TICK_MINUTES,
            },
        )

    @app.get("/api/status")
    def api_status():
        d = snapshot()
        return JSONResponse({
            "running": _active["running"],
            "kind": _active["kind"],
            "last_status": d["state"].last_status,
            "last_success_at": (
                d["state"].last_success_at.isoformat() if d["state"].last_success_at else None
            ),
            "decision": d["decision"].describe(d["now"]),
            "needs_login": d["needs_login"],
            "stale": d["stale"],
            "streak": d["streak"],
        })

    @app.post("/run")
    def post_run(dry: str = Form(default="")):
        start_run(dry_run=bool(dry))
        return RedirectResponse("/", status_code=303)

    @app.post("/settings")
    def post_settings(
        interval_hours: int = Form(...),
        resume_path: str = Form(default=""),
        quiet_start: str = Form(default=""),
        quiet_end: str = Form(default=""),
        headed_mode: str = Form(default=""),
    ):
        if interval_hours not in VALID_INTERVALS:
            raise HTTPException(400, "interval must be one of {}".format(VALID_INTERVALS))
        conn = store.connect()
        try:
            store.put(conn, "interval_hours", interval_hours)
            store.put(conn, "resume_path", resume_path.strip())
            store.put(conn, "quiet_start", quiet_start.strip())
            store.put(conn, "quiet_end", quiet_end.strip())
            store.put(conn, "headed_mode", "1" if headed_mode else "0")
        finally:
            conn.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/install-task")
    def post_install_task(remove: str = Form(default="")):
        if remove:
            scheduling.unregister()
        else:
            scheduling.register()
        return RedirectResponse("/", status_code=303)

    @app.get("/screenshot/{run_id}")
    def screenshot(run_id: int):
        """Serve by run id, never by path.

        Taking a filename from the URL would be a path-traversal hole straight
        into the user's filesystem; the id is looked up and the resolved path is
        checked to be inside the screenshot directory before anything is served.
        """
        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT screenshot FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["screenshot"]:
            raise HTTPException(404)

        path = (config.PROJECT_ROOT / row["screenshot"]).resolve()
        try:
            path.relative_to(config.SCREENSHOT_DIR.resolve())
        except ValueError:
            raise HTTPException(404)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(str(path), media_type="image/png")

    return app


def serve(host: str = HOST, port: int = PORT, open_browser: bool = True) -> int:
    import uvicorn

    if open_browser:
        import webbrowser

        threading.Timer(
            0.8, lambda: webbrowser.open("http://{}:{}".format(host, port))
        ).start()

    print("Dashboard on http://{}:{}  (Ctrl-C to stop)".format(host, port))
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    return 0
