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
from typing import Dict, Optional, List

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
                    continue
                # Idle timeout based on last message activity.
                if worker.sessionid is not None:
                    if now - worker.last_activity >= self.idle_timeout:
                        await self.stop_worker(worker, reason="idle")
            await asyncio.sleep(5)

    async def wait_ready(self, port: int) -> bool:
        if not self.client:
            return False
        deadline = time.time() + self.startup_timeout
        url = f"http://127.0.0.1:{port}/ice"
        while time.time() < deadline:
            try:
                async with self.client.get(url) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                await asyncio.sleep(0.3)
        return False

    async def start_worker(self) -> Optional[Worker]:
        for port in self.ports:
            existing = self.workers_by_port.get(port)
            if existing and existing.process.poll() is None:
                continue
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
                continue
            return worker
        return None

    def map_session(self, sessionid: int, worker: Worker) -> None:
        worker.sessionid = sessionid
        worker.last_activity = time.time()
        self.session_map[sessionid] = worker
        print(f"[gateway] session {sessionid} mapped to port {worker.port}", flush=True)

    def get_worker_for_session(self, sessionid: int) -> Optional[Worker]:
        return self.session_map.get(sessionid)

    def touch(self, worker: Worker) -> None:
        worker.last_activity = time.time()


async def _fetch_cf_ice_servers() -> Optional[list]:
    token_id = os.getenv("CF_TURN_TOKEN_ID", "")
    api_token = os.getenv("CF_TURN_API_TOKEN", "")
    if not token_id or not api_token:
        return None

    ttl = int(os.getenv("CF_TURN_TTL", "3600"))
    url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{token_id}/credentials/generate-ice-servers"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {"ttl": ttl}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                text = await response.text()
                if response.status not in (200, 201):
                    return None
                data = json.loads(text)
    except aiohttp.ClientError:
        return None

    return data.get("iceServers") or data.get("ice_servers") or data.get("ice")


async def ice(request: web.Request) -> web.Response:
    ice_servers = await _fetch_cf_ice_servers()
    if not ice_servers:
        ice_servers = []
    return web.Response(
        content_type="application/json",
        text=json.dumps({"iceServers": ice_servers}),
    )


async def offer(request: web.Request) -> web.Response:
    manager: WorkerManager = request.app["manager"]
    params = await request.json()

    worker = await manager.start_worker()
    if not worker:
        return web.Response(status=503, text="No available worker")

    url = f"http://127.0.0.1:{worker.port}/offer"
    async with manager.client.post(url, json=params) as resp:
        data = await resp.json()
        if resp.status == 200 and isinstance(data, dict) and "sessionid" in data:
            sessionid = int(data["sessionid"])
            manager.map_session(sessionid, worker)
        return web.Response(
            content_type="application/json",
            status=resp.status,
            text=json.dumps(data),
        )


async def human(request: web.Request) -> web.Response:
    manager: WorkerManager = request.app["manager"]
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    manager.touch(worker)

    url = f"http://127.0.0.1:{worker.port}/human"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def humanaudio(request: web.Request) -> web.Response:
    manager: WorkerManager = request.app["manager"]
    form = await request.post()
    sessionid = int(form.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid)
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
    manager: WorkerManager = request.app["manager"]
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")

    url = f"http://127.0.0.1:{worker.port}/set_audiotype"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def config(request: web.Request) -> web.Response:
    manager: WorkerManager = request.app["manager"]
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid) if sessionid else None
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
    manager: WorkerManager = request.app["manager"]
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")

    url = f"http://127.0.0.1:{worker.port}/record"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def is_speaking(request: web.Request) -> web.Response:
    manager: WorkerManager = request.app["manager"]
    params = await request.json()
    sessionid = int(params.get("sessionid", 0))
    worker = manager.get_worker_for_session(sessionid)
    if not worker:
        return web.Response(status=410, text="Session expired")
    url = f"http://127.0.0.1:{worker.port}/is_speaking"
    async with manager.client.post(url, json=params) as resp:
        body = await resp.read()
        return web.Response(status=resp.status, body=body, content_type=resp.content_type)


async def on_startup(app: web.Application) -> None:
    manager: WorkerManager = app["manager"]
    await manager.start()
    app["cleanup_task"] = asyncio.create_task(manager.cleanup_loop())


async def on_shutdown(app: web.Application) -> None:
    cleanup_task = app.get("cleanup_task")
    if cleanup_task:
        cleanup_task.cancel()
        with contextlib.suppress(Exception):
            await cleanup_task
    manager: WorkerManager = app["manager"]
    await manager.close()


def _load_args_file(path: str) -> List[str]:
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("worker_args_file must be a JSON list of strings")
    return [str(x) for x in data]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listenport", type=int, default=8090)
    parser.add_argument("--base_port", type=int, default=8091)
    parser.add_argument("--max_workers", type=int, default=5)
    parser.add_argument("--idle_timeout", type=int, default=60)
    parser.add_argument("--worker_args_file", type=str, default=str(BASE_DIR / "worker_args.json"))
    args = parser.parse_args()

    try:
        base_args = _load_args_file(args.worker_args_file)
    except FileNotFoundError:
        base_args = []

    ports = [args.base_port + i for i in range(args.max_workers)]
    env = os.environ.copy()

    manager = WorkerManager(
        base_args=base_args,
        ports=ports,
        idle_timeout=args.idle_timeout,
        env=env,
    )

    app = web.Application()
    app["manager"] = manager

    # Routes
    app.router.add_get("/ice", ice)
    app.router.add_post("/offer", offer)
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/config", config)
    app.router.add_post("/record", record)
    app.router.add_post("/is_speaking", is_speaking)
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
