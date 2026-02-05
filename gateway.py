import argparse
import contextlib
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, List, Tuple

import aiohttp
from aiohttp import web
import aiohttp_cors

# Simple multi-process gateway for LiveTalking.
# Spawns worker app.py processes on-demand, proxies requests,
# and stops idle workers after a timeout.

BASE_DIR = Path(__file__).resolve().parent
APP_PY = str(BASE_DIR / "app.py")


@dataclass
class Worker:
    port: int
    process: subprocess.Popen
    started_at: float
    last_activity: float
    sessionid: Optional[int] = None
    log_path: Optional[Path] = None
    ready: bool = False
    log_fp = None


class WorkerManager:
    def __init__(
        self,
        base_args: List[str],
        ports: List[int],
        idle_timeout: int,
        env: Dict[str, str],
        startup_timeout: int = 30,
    ) -> None:
        self.base_args = list(base_args)
        self.ports = list(ports)
        self.idle_timeout = idle_timeout
        self.env = env
        self.startup_timeout = startup_timeout
        self.workers_by_port: Dict[int, Worker] = {}
        self.session_map: Dict[int, Worker] = {}
        self.client: Optional[aiohttp.ClientSession] = None
        self.log_dir = BASE_DIR / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        if self.client is None:
            timeout = aiohttp.ClientTimeout(total=30)
            self.client = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        await self.stop_all()
        if self.client:
            await self.client.close()
            self.client = None

    async def stop_all(self) -> None:
        for worker in list(self.workers_by_port.values()):
            await self.stop_worker(worker, reason="shutdown")

    async def stop_worker(self, worker: Worker, reason: str = "idle") -> None:
        print(f"[gateway] stopping worker on port {worker.port} (pid={worker.process.pid}) reason={reason}", flush=True)
        if worker.sessionid in self.session_map:
            del self.session_map[worker.sessionid]
        if worker.port in self.workers_by_port:
            del self.workers_by_port[worker.port]

        proc = worker.process
        if proc.poll() is None:
            try:
                proc.terminate()
                await asyncio.sleep(0.2)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        if worker.log_fp:
            try:
                worker.log_fp.close()
            except Exception:
                pass

    async def cleanup_loop(self) -> None:
        while True:
            now = time.time()
            for worker in list(self.workers_by_port.values()):
                # Remove crashed workers.
                if worker.process.poll() is not None:
                    await self.stop_worker(worker, reason="crash")
                    await self.start_worker_at(worker.port)
                    continue
                # Idle timeout based on last message activity.
                if worker.sessionid is not None:
                    if now - worker.last_activity >= self.idle_timeout:
                        await self.end_session(worker, reason="idle")
            await asyncio.sleep(5)

    async def wait_ready(self, port: int) -> bool:
        if not self.client:
            return False
        deadline = time.time() + self.startup_timeout
        url = f"http://127.0.0.1:{port}/health"
        while time.time() < deadline:
            try:
                async with self.client.get(url) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                await asyncio.sleep(0.3)
        return False

    async def start_worker_at(self, port: int) -> Optional[Worker]:
        existing = self.workers_by_port.get(port)
        if existing and existing.process.poll() is None:
            return existing
        if existing:
            await self.stop_worker(existing, reason="restart")

        log_path = self.log_dir / f"worker-{port}.log"
        log_fp = open(log_path, "ab")
        cmd = [sys.executable, APP_PY] + self.base_args + [
            "--listenport",
            str(port),
            "--max_session",
            "1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=log_fp,
            env=self.env,
            cwd=str(BASE_DIR),
        )
        worker = Worker(
            port=port,
            process=proc,
            started_at=time.time(),
            last_activity=time.time(),
            log_path=log_path,
        )
        worker.log_fp = log_fp
        self.workers_by_port[port] = worker
        print(f"[gateway] starting worker on port {port} (pid={proc.pid})", flush=True)

        ready = await self.wait_ready(port)
        if not ready or proc.poll() is not None:
            await self.stop_worker(worker, reason="startup-failed")
            return None
        worker.ready = True
        return worker

    async def start_all_workers(self) -> None:
        for port in self.ports:
            await self.start_worker_at(port)

    def get_free_worker(self) -> Optional[Worker]:
        for worker in self.workers_by_port.values():
            if worker.process.poll() is None and worker.sessionid is None and worker.ready:
                return worker
        return None

    async def wait_for_free_worker(self, timeout: int = 60) -> Optional[Worker]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            worker = self.get_free_worker()
            if worker:
                return worker
            await asyncio.sleep(0.5)
        return None

    def map_session(self, sessionid: int, worker: Worker) -> None:
        worker.sessionid = sessionid
        worker.last_activity = time.time()
        self.session_map[sessionid] = worker
        print(f"[gateway] session {sessionid} mapped to port {worker.port}", flush=True)

    def release_worker(self, worker: Worker) -> None:
        if worker.sessionid in self.session_map:
            del self.session_map[worker.sessionid]
        worker.sessionid = None
        worker.last_activity = time.time()

    def get_worker_for_session(self, sessionid: int) -> Optional[Worker]:
        return self.session_map.get(sessionid)

    def touch(self, worker: Worker) -> None:
        worker.last_activity = time.time()

    async def end_session(self, worker: Worker, reason: str = "idle") -> None:
        if not self.client:
            self.release_worker(worker)
            return
        sessionid = worker.sessionid
        if not sessionid:
            return
        try:
            url = f"http://127.0.0.1:{worker.port}/end_session"
            await self.client.post(url, json={"sessionid": sessionid})
        except Exception:
            pass
        self.release_worker(worker)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def offer(request: web.Request) -> web.Response:
    params = await request.json()
    profile = params.get("profile") or request.app.get("default_profile") or "default"
    managers = _get_managers(request.app)
    manager = managers.get(profile)
    if not manager:
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": f"Unknown profile: {profile}"}),
        )
    profiles_cfg = request.app.get("profiles", {})
    if profiles_cfg and not _profile_ready(profiles_cfg.get(profile, {})):
        return web.Response(
            status=503,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "Profile not ready"}),
        )

    worker = await manager.wait_for_free_worker()
    if not worker:
        return web.Response(
            status=503,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "No available worker"}),
        )

    url = f"http://127.0.0.1:{worker.port}/offer"
    async with manager.client.post(url, json=params) as resp:
        body_text = await resp.text()
        try:
            data = json.loads(body_text)
        except Exception:
            return web.Response(
                status=502,
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": "Invalid worker response", "detail": body_text[:200]}),
            )

        if resp.status == 200 and isinstance(data, dict) and data.get("code", 0) == 0 and "sessionid" in data:
            sessionid = int(data["sessionid"])
            manager.map_session(sessionid, worker)
        return web.Response(
            content_type="application/json",
            status=resp.status,
            text=json.dumps(data),
        )


async def daily_start(request: web.Request) -> web.Response:
    params = await request.json()
    profile = params.get("profile") or request.app.get("default_profile") or "default"
    managers = _get_managers(request.app)
    manager = managers.get(profile)
    if not manager:
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": f"Unknown profile: {profile}"}),
        )
    profiles_cfg = request.app.get("profiles", {})
    if profiles_cfg and not _profile_ready(profiles_cfg.get(profile, {})):
        return web.Response(
            status=503,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "Profile not ready"}),
        )

    worker = await manager.wait_for_free_worker()
    if not worker:
        return web.Response(
            status=503,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "No available worker"}),
        )

    url = f"http://127.0.0.1:{worker.port}/daily/start"
    async with manager.client.post(url, json=params) as resp:
        body_text = await resp.text()
        try:
            data = json.loads(body_text)
        except Exception:
            return web.Response(
                status=502,
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": "Invalid worker response", "detail": body_text[:200]}),
            )

        if resp.status == 200 and isinstance(data, dict) and data.get("code", 0) == 0 and "sessionid" in data:
            sessionid = int(data["sessionid"])
            manager.map_session(sessionid, worker)
        return web.Response(
            content_type="application/json",
            status=resp.status,
            text=json.dumps(data),
        )


async def human(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    manager.touch(worker)

    url = f"http://127.0.0.1:{worker.port}/human"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def humanaudio(request: web.Request) -> web.Response:
    form = await request.post()
    sessionid = int(form.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    manager.touch(worker)

    data = aiohttp.FormData()
    for key, value in form.items():
        if hasattr(value, "filename"):
            payload = value.file.read()
            data.add_field(
                key,
                payload,
                filename=value.filename,
                content_type=value.content_type,
            )
        else:
            data.add_field(key, value)

    url = f"http://127.0.0.1:{worker.port}/humanaudio"
    async with manager.client.post(url, data=data) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def set_audiotype(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")

    url = f"http://127.0.0.1:{worker.port}/set_audiotype"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def config(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid) if sessionid else (None, None)
    if sessionid and not worker:
        return web.Response(status=410, text="Session expired")

    if worker:
        url = f"http://127.0.0.1:{worker.port}/config"
        async with manager.client.post(url, json=params) as resp:
            body = await resp.read()
            return web.Response(status=resp.status, body=body, content_type=resp.content_type)

    # If no session, just return OK.
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "msg": "ok"}),
    )


async def record(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")

    url = f"http://127.0.0.1:{worker.port}/record"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def is_speaking(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    url = f"http://127.0.0.1:{worker.port}/is_speaking"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def webrtc_quality(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    url = f"http://127.0.0.1:{worker.port}/webrtc_quality"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def on_startup(app: web.Application) -> None:
    managers = _get_managers(app)
    if not managers:
        return
    for manager in managers.values():
        await manager.start()

    warm_tasks = []
    cleanup_tasks = []
    profiles_cfg = app.get("profiles", {})
    profile_ready = {}
    for name, manager in managers.items():
        ready = True
        if profiles_cfg:
            ready = _profile_ready(profiles_cfg.get(name, {}))
        profile_ready[name] = ready
        if ready:
            warm_tasks.append(asyncio.create_task(manager.start_all_workers()))
        cleanup_tasks.append(asyncio.create_task(manager.cleanup_loop()))
    app["warm_tasks"] = warm_tasks
    app["cleanup_tasks"] = cleanup_tasks
    app["profile_ready"] = profile_ready
    if profiles_cfg:
        app["profile_watch_task"] = asyncio.create_task(profile_watch_loop(app))


async def on_shutdown(app: web.Application) -> None:
    for task in app.get("warm_tasks", []):
        task.cancel()
        with contextlib.suppress(Exception):
            await task
    for task in app.get("cleanup_tasks", []):
        task.cancel()
        with contextlib.suppress(Exception):
            await task
    watch_task = app.get("profile_watch_task")
    if watch_task:
        watch_task.cancel()
        with contextlib.suppress(Exception):
            await watch_task
    for manager in _get_managers(app).values():
        await manager.close()


async def end_session(request: web.Request) -> web.Response:
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    manager, worker = _find_worker(request.app, sessionid)
    if not worker:
        return web.Response(
            status=410,
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": "Session expired"}),
        )
    await manager.end_session(worker, reason="client")
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": 0, "msg": "ended"}),
    )


@web.middleware
async def no_cache_middleware(request, handler):
    resp = await handler(request)
    path = request.path.lower()
    if path.endswith(".html") or path.endswith(".js"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def profile_watch_loop(app: web.Application) -> None:
    while True:
        profiles_cfg = app.get("profiles", {})
        if not profiles_cfg:
            return
        profile_ready = app.get("profile_ready", {})
        for name, manager in _get_managers(app).items():
            cfg = profiles_cfg.get(name, {})
            ready = _profile_ready(cfg)
            was_ready = profile_ready.get(name, False)
            profile_ready[name] = ready
            if ready and not was_ready:
                await manager.start_all_workers()
        app["profile_ready"] = profile_ready
        await asyncio.sleep(10)


async def profiles(request: web.Request) -> web.Response:
    profiles_cfg = request.app.get("profiles")
    if not profiles_cfg:
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "default": "default",
                "profiles": [{"id": "default", "label": "Default", "ready": True}],
            }),
        )

    items = []
    for name, cfg in profiles_cfg.items():
        items.append({
            "id": name,
            "label": cfg.get("label", name),
            "ready": _profile_ready(cfg),
        })
    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "default": request.app.get("default_profile") or next(iter(profiles_cfg.keys())),
            "profiles": items,
        }),
    )


def _load_args_file(path: str) -> List[str]:
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("worker_args_file must be a JSON list of strings")
    return [str(x) for x in data]


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


def _load_profiles_file(path: str) -> Tuple[Dict[str, dict], str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict) and "profiles" in raw:
        profiles = raw.get("profiles", {})
        default_profile = raw.get("default")
    else:
        profiles = raw if isinstance(raw, dict) else {}
        default_profile = None

    if not profiles:
        raise ValueError("profiles_file has no profiles")

    if not default_profile:
        default_profile = next(iter(profiles.keys()))

    normalized: Dict[str, dict] = {}
    for name, cfg in profiles.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"profile {name} must be an object")
        args_file = cfg.get("worker_args_file") or cfg.get("args_file")
        base_port = cfg.get("base_port")
        max_workers = cfg.get("max_workers")
        if not args_file or base_port is None or max_workers is None:
            raise ValueError(f"profile {name} missing worker_args_file/base_port/max_workers")
        ready_check = cfg.get("ready_check")
        normalized[name] = {
            "label": cfg.get("label", name),
            "worker_args_file": str(args_file),
            "base_port": int(base_port),
            "max_workers": int(max_workers),
            "ready_check": str(ready_check) if ready_check else "",
        }

    return normalized, str(default_profile)


def _profile_ready(cfg: dict) -> bool:
    ready_check = cfg.get("ready_check") or ""
    if not ready_check:
        return True
    return _resolve_path(ready_check).exists()


def _get_managers(app: web.Application) -> Dict[str, "WorkerManager"]:
    managers = app.get("managers")
    if managers:
        return managers
    manager = app.get("manager")
    if manager:
        return {"default": manager}
    return {}


def _find_worker(app: web.Application, sessionid: int) -> Tuple[Optional["WorkerManager"], Optional[Worker]]:
    for manager in _get_managers(app).values():
        worker = manager.get_worker_for_session(sessionid)
        if worker:
            return manager, worker
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listenport", type=int, default=8090)
    parser.add_argument("--base_port", type=int, default=8091)
    parser.add_argument("--max_workers", type=int, default=5)
    parser.add_argument("--idle_timeout", type=int, default=60)
    parser.add_argument("--worker_args_file", type=str, default=str(BASE_DIR / "worker_args.json"))
    parser.add_argument("--profiles_file", type=str, default="")
    args = parser.parse_args()

    env = os.environ.copy()
    profiles_file = args.profiles_file.strip()
    if profiles_file:
        profiles_cfg, default_profile = _load_profiles_file(_resolve_path(profiles_file))
        managers: Dict[str, WorkerManager] = {}
        for name, cfg in profiles_cfg.items():
            try:
                base_args = _load_args_file(_resolve_path(cfg["worker_args_file"]))
            except FileNotFoundError:
                base_args = []
            ports = [cfg["base_port"] + i for i in range(cfg["max_workers"])]
            managers[name] = WorkerManager(
                base_args=base_args,
                ports=ports,
                idle_timeout=args.idle_timeout,
                env=env,
            )
        manager = None
    else:
        try:
            base_args = _load_args_file(args.worker_args_file)
        except FileNotFoundError:
            base_args = []
        ports = [args.base_port + i for i in range(args.max_workers)]
        managers = {}
        manager = WorkerManager(
            base_args=base_args,
            ports=ports,
            idle_timeout=args.idle_timeout,
            env=env,
        )

    app = web.Application()
    app.middlewares.append(no_cache_middleware)
    if managers:
        app["managers"] = managers
        app["profiles"] = profiles_cfg
        app["default_profile"] = default_profile
    else:
        app["manager"] = manager

    # Routes
    app.router.add_get("/health", health)
    app.router.add_get("/profiles", profiles)
    app.router.add_post("/daily/start", daily_start)
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/config", config)
    app.router.add_post("/webrtc_quality", webrtc_quality)
    app.router.add_post("/record", record)
    app.router.add_post("/is_speaking", is_speaking)
    app.router.add_post("/end_session", end_session)
    app.router.add_static("/", path=str(BASE_DIR / "web"), show_index=True)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host="0.0.0.0", port=args.listenport)


if __name__ == "__main__":
    main()
