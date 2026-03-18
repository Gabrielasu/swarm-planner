"""Real-time dashboard server for the Planning Swarm.

Serves a sleek web UI that shows pipeline progress, task graph status,
and discoveries -- all updating in real time via WebSocket.

Usage:
    swarm dashboard           # start on port 8420
    swarm dashboard --port 9000
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .artifacts import load_json
from .graph_builder import (
    add_discovery,
    load_graph,
    mark_task_done,
    mark_task_in_progress,
    invalidate_task,
    get_task_packet,
    resolve_discovery,
)
from .schemas import Discovery, DiscoveryType, Severity
from .runner import PlanningSwarm, PipelineState, Step, STEP_ORDER, STEP_LABELS


# ---------------------------------------------------------------------------
# Snapshot builder -- reads .plan/ and assembles a dashboard payload
# ---------------------------------------------------------------------------


def _build_snapshot(project_dir: Path) -> dict:
    """Read all .plan/ state and return a single JSON-serialisable dict."""
    plan_dir = project_dir / ".plan"

    # Pipeline state
    state_file = plan_dir / ".state.json"
    pipeline = {
        "completed_steps": [],
        "current_step": None,
        "log": [],
    }
    if state_file.exists():
        raw = json.loads(state_file.read_text())
        pipeline["completed_steps"] = raw.get("completed_steps", [])
        pipeline["current_step"] = raw.get("current_step")
        pipeline["log"] = raw.get("log", [])

    # Step definitions (for the frontend)
    steps = []
    for step in STEP_ORDER:
        status = "pending"
        if step.value in pipeline["completed_steps"]:
            status = "done"
        elif pipeline["current_step"] == step.value:
            status = "running"
        steps.append({
            "id": step.value,
            "label": STEP_LABELS[step],
            "status": status,
        })

    # Task graph
    graph = load_json(plan_dir / "graph.json")
    tasks = []
    discoveries = []
    meta = {
        "total_tasks": 0, "done": 0, "ready": 0, "blocked": 0,
        "in_progress": 0, "version": 0,
    }
    project_name = ""
    components = {}

    if graph:
        tasks = graph.get("tasks", [])
        discoveries = graph.get("discoveries", [])
        project_name = graph.get("project", "")
        components = graph.get("components", {})
        gm = graph.get("meta", {})
        meta.update({
            "total_tasks": gm.get("total_tasks", 0),
            "done": gm.get("done", 0),
            "ready": gm.get("ready", 0),
            "blocked": gm.get("blocked", 0),
            "in_progress": sum(
                1 for t in tasks if t.get("status") == "in_progress"
            ),
            "version": gm.get("version", 0),
            "changelog": gm.get("changelog", []),
        })

    return {
        "project": project_name,
        "pipeline": {"steps": steps, "log": pipeline["log"]},
        "tasks": tasks,
        "discoveries": discoveries,
        "meta": meta,
        "components": components,
    }


# ---------------------------------------------------------------------------
# File watcher -- detects .plan/ changes and notifies WebSocket clients
# ---------------------------------------------------------------------------


class PlanWatcher:
    """Watches .plan/ directory for file modifications."""

    def __init__(self, project_dir: Path, interval: float = 0.5):
        self.plan_dir = project_dir / ".plan"
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_mtimes: dict[str, float] = {}
        self._callbacks: list = []

    def on_change(self, callback):
        self._callbacks.append(callback)

    def start(self):
        self._running = True
        self._last_mtimes = self._scan_mtimes()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _scan_mtimes(self) -> dict[str, float]:
        mtimes = {}
        if not self.plan_dir.exists():
            return mtimes
        for f in self.plan_dir.rglob("*.json"):
            try:
                mtimes[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        state = self.plan_dir / ".state.json"
        if state.exists():
            try:
                mtimes[str(state)] = state.stat().st_mtime
            except OSError:
                pass
        return mtimes

    def _watch(self):
        while self._running:
            time.sleep(self.interval)
            current = self._scan_mtimes()
            if current != self._last_mtimes:
                self._last_mtimes = current
                for cb in self._callbacks:
                    try:
                        cb()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def create_app(project_dir: Path) -> FastAPI:
    """Create the dashboard FastAPI application."""
    app = FastAPI(title="Swarm Dashboard")
    connected_clients: list[WebSocket] = []
    watcher = PlanWatcher(project_dir)

    static_dir = Path(__file__).parent / "static"
    html_path = static_dir / "dashboard.html"

    @app.on_event("startup")
    async def startup():
        watcher.on_change(lambda: _notify_clients(app, project_dir))
        watcher.start()

    @app.on_event("shutdown")
    async def shutdown():
        watcher.stop()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return html_path.read_text(encoding="utf-8")

    @app.get("/api/snapshot")
    async def snapshot():
        return _build_snapshot(project_dir)

    # -- Task detail (full prompt packet) -----------------------------------

    @app.get("/api/task/{task_id}")
    async def task_detail(task_id: str):
        packet = get_task_packet(project_dir, task_id)
        if packet is None:
            return {"error": f"Task {task_id} not found"}
        # Also attach the graph-level status
        graph = load_graph(project_dir)
        if graph:
            for t in graph["tasks"]:
                if t["id"] == task_id:
                    packet["_status"] = t["status"]
                    break
        return packet

    # -- Task management endpoints ------------------------------------------

    @app.post("/api/task/{task_id}/start")
    async def api_start_task(task_id: str):
        try:
            graph = mark_task_in_progress(project_dir, task_id)
            return {"ok": True, "status": "in_progress"}
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/task/{task_id}/done")
    async def api_done_task(task_id: str):
        try:
            graph = mark_task_done(project_dir, task_id)
            meta = graph["meta"]
            return {
                "ok": True,
                "status": "done",
                "ready": meta["ready"],
                "done": meta["done"],
            }
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/task/{task_id}/invalidate")
    async def api_invalidate_task(task_id: str):
        try:
            graph = invalidate_task(project_dir, task_id)
            return {"ok": True, "status": "invalidated"}
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    # -- Discovery management endpoints -------------------------------------

    @app.post("/api/discovery/add")
    async def api_add_discovery(body: dict):
        try:
            disc = Discovery(
                found_during=body.get("found_during", ""),
                type=DiscoveryType(body.get("type", "scope_change")),
                description=body.get("description", ""),
                affects=body.get("affects", []),
                severity=Severity(body.get("severity", "high")),
            )
            graph = add_discovery(project_dir, disc)
            unresolved = sum(
                1 for d in graph["discoveries"]
                if not d.get("resolved", False)
            )
            return {"ok": True, "unresolved": unresolved}
        except (FileNotFoundError, ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/discovery/{index}/resolve")
    async def api_resolve_discovery(index: int, body: dict):
        try:
            resolution = body.get("resolution", "")
            if not resolution:
                return {"ok": False, "error": "Resolution text is required"}
            graph = resolve_discovery(project_dir, index, resolution)
            meta = graph["meta"]
            return {
                "ok": True,
                "ready": meta["ready"],
                "done": meta["done"],
            }
        except (FileNotFoundError, IndexError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    # -- Pipeline control endpoints -----------------------------------------

    # Track whether a pipeline run is in progress
    _run_lock = {"running": False, "step": None, "error": None}

    def _load_config():
        """Load swarm config for pipeline runs."""
        try:
            from .config import load_config
            return load_config()
        except Exception:
            return {"max_adversary_rounds": 3}

    @app.post("/api/pipeline/rerun/{step_name}")
    async def api_rerun(step_name: str):
        if _run_lock["running"]:
            return {"ok": False, "error": f"Pipeline already running ({_run_lock['step']})"}
        try:
            step = Step(step_name)
        except ValueError:
            valid = [s.value for s in STEP_ORDER]
            return {"ok": False, "error": f"Unknown step. Valid: {valid}"}

        def _run():
            _run_lock["running"] = True
            _run_lock["step"] = step_name
            _run_lock["error"] = None
            try:
                cfg = _load_config()
                config = {"max_adversary_rounds": cfg.get("max_adversary_rounds", 3)}
                swarm = PlanningSwarm(project_dir, config)
                swarm.run(from_step=step_name)
            except Exception as e:
                _run_lock["error"] = str(e)
            finally:
                _run_lock["running"] = False
                _run_lock["step"] = None

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": f"Rerunning from {STEP_LABELS[step]}"}

    @app.post("/api/pipeline/approve")
    async def api_approve():
        if _run_lock["running"]:
            return {"ok": False, "error": f"Pipeline already running ({_run_lock['step']})"}
        state_file = project_dir / ".plan" / ".state.json"
        if not state_file.exists():
            return {"ok": False, "error": "No plan found"}
        state = PipelineState(state_file)
        if state.is_complete(Step.HUMAN_REVIEW):
            return {"ok": False, "error": "Already approved"}
        state.mark_complete(Step.HUMAN_REVIEW, log_msg="Approved from dashboard")

        # Continue pipeline in background
        def _run():
            _run_lock["running"] = True
            _run_lock["step"] = "post_approve"
            _run_lock["error"] = None
            try:
                cfg = _load_config()
                config = {"max_adversary_rounds": cfg.get("max_adversary_rounds", 3)}
                swarm = PlanningSwarm(project_dir, config)
                swarm.run(resume=True)
            except Exception as e:
                _run_lock["error"] = str(e)
            finally:
                _run_lock["running"] = False
                _run_lock["step"] = None

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "Approved. Pipeline continuing..."}

    @app.get("/api/pipeline/status")
    async def api_pipeline_status():
        return {
            "running": _run_lock["running"],
            "step": _run_lock["step"],
            "error": _run_lock["error"],
        }

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        connected_clients.append(ws)
        try:
            data = _build_snapshot(project_dir)
            await ws.send_json({"type": "snapshot", "data": data})
            while True:
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=30)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        finally:
            if ws in connected_clients:
                connected_clients.remove(ws)

    app.state.connected_clients = connected_clients
    app.state.project_dir = project_dir

    return app


def _notify_clients(app: FastAPI, project_dir: Path):
    """Called from the file watcher thread when .plan/ changes."""
    clients = app.state.connected_clients
    if not clients:
        return

    data = _build_snapshot(project_dir)
    message = json.dumps({"type": "snapshot", "data": data})

    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return

    async def _broadcast():
        dead = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in clients:
                clients.remove(ws)

    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(), loop)
    else:
        loop.run_until_complete(_broadcast())


def run_dashboard(project_dir: Path, port: int = 8420, open_browser: bool = True):
    """Start the dashboard server."""
    import uvicorn
    import webbrowser

    app = create_app(project_dir)
    url = f"http://localhost:{port}"

    print(f"\n  Swarm Dashboard")
    print(f"  ---------------")
    print(f"  Serving at: {url}")
    print(f"  Watching:   {project_dir / '.plan'}")
    print(f"  Press Ctrl+C to stop\n")

    if open_browser:
        def _open():
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
