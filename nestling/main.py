import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from nestling.auth import TokenManager
from nestling.camera import CameraManager, Pipeline
from nestling.sdm import SDMClient
from nestling.stream.webrtc import BrowserPeerConnection

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

# Camera registry — device_name → CameraEntry.
# Protected by app["registry_lock"]; the lock is held only for dict reads/writes,
# never across slow WebRTC negotiation or ICE gathering.
_registry: dict[str, "_CameraEntry"] = {}


@dataclass
class _CameraEntry:
    manager: CameraManager
    raw_pipeline: Pipeline
    browser_pcs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(key: str) -> str:
    v = os.environ.get(key, "")
    if not v:
        sys.exit(f"missing required environment variable: {key}")
    return v


def _bad_device(device: str, sdm: SDMClient) -> bool:
    return not device or not sdm.validate_device(device)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def _handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC / "index.html")


async def _handle_devices(request: web.Request) -> web.Response:
    sdm: SDMClient = request.app["sdm"]
    try:
        data = await sdm.list_devices()
        return web.json_response(data)
    except Exception as exc:
        log.error("list_devices: %s", exc)
        raise web.HTTPBadGateway(text="SDM request failed")


async def _handle_status(request: web.Request) -> web.Response:
    sdm: SDMClient = request.app["sdm"]
    device = request.query.get("device", "")
    if _bad_device(device, sdm):
        raise web.HTTPBadRequest(text="invalid or missing device parameter")
    async with request.app["registry_lock"]:
        running = device in _registry
    return web.json_response({"running": running})


async def _handle_start(request: web.Request) -> web.Response:
    sdm: SDMClient = request.app["sdm"]
    device = request.query.get("device", "")
    if _bad_device(device, sdm):
        raise web.HTTPBadRequest(text="invalid or missing device parameter")

    lock: asyncio.Lock = request.app["registry_lock"]

    async with lock:
        if device in _registry:
            raise web.HTTPConflict(text="camera already running")

    raw_pipeline = Pipeline()
    manager = CameraManager(sdm, device, raw_pipeline)

    try:
        await manager.start()
    except Exception as exc:
        await manager.stop()
        raw_pipeline.close()
        log.exception("camera start failed for %s", device)
        raise web.HTTPBadGateway(text=f"failed to start camera: {exc}")

    async with lock:
        if device in _registry:
            await manager.stop()
            raw_pipeline.close()
            raise web.HTTPConflict(text="camera already running")
        _registry[device] = _CameraEntry(
            manager=manager,
            raw_pipeline=raw_pipeline,
        )

    return web.json_response({"status": "started", "device": device})


async def _handle_stop(request: web.Request) -> web.Response:
    sdm: SDMClient = request.app["sdm"]
    device = request.query.get("device", "")
    if _bad_device(device, sdm):
        raise web.HTTPBadRequest(text="invalid or missing device parameter")

    lock: asyncio.Lock = request.app["registry_lock"]
    async with lock:
        entry = _registry.pop(device, None)

    if entry is None:
        raise web.HTTPNotFound(text="camera not running")

    for bpc in list(entry.browser_pcs):
        await bpc.close()
    entry.raw_pipeline.close()
    await entry.manager.stop()
    return web.Response(status=200)


async def _handle_webrtc_offer(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid JSON body")

    sdm: SDMClient = request.app["sdm"]
    device = body.get("device", "")
    sdp    = body.get("sdp", "")
    type_  = body.get("type", "")

    if _bad_device(device, sdm):
        raise web.HTTPBadRequest(text="invalid or missing device")

    async with request.app["registry_lock"]:
        entry = _registry.get(device)

    if entry is None:
        raise web.HTTPNotFound(text="camera not running")

    bpc = BrowserPeerConnection(entry.raw_pipeline)
    try:
        answer_sdp, answer_type = await bpc.handle_offer(sdp, type_)
    except Exception as exc:
        await bpc.close()
        log.error("webrtc/offer failed: %s", exc)
        raise web.HTTPInternalServerError(text="WebRTC negotiation failed")

    entry.browser_pcs.append(bpc)
    log.info("webrtc: new browser connection for %s (%d total)", device.split("/")[-1], len(entry.browser_pcs))
    return web.json_response({"sdp": answer_sdp, "type": answer_type})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _on_startup(app: web.Application) -> None:
    app["registry_lock"] = asyncio.Lock()

    tm = TokenManager(
        client_id=_require_env("GOOGLE_CLIENT_ID"),
        client_secret=_require_env("GOOGLE_CLIENT_SECRET"),
        refresh_token=_require_env("GOOGLE_REFRESH_TOKEN"),
    )
    await tm.start()
    app["token_manager"] = tm

    sdm = SDMClient(tm, _require_env("GOOGLE_PROJECT_ID"))
    app["sdm"] = sdm

    log.info("nestling ready — port 8080")


async def _on_cleanup(app: web.Application) -> None:
    lock: asyncio.Lock = app["registry_lock"]
    async with lock:
        entries = list(_registry.values())
        _registry.clear()

    for entry in entries:
        for bpc in list(entry.browser_pcs):
            await bpc.close()
        entry.raw_pipeline.close()
        await entry.manager.stop()

    if sdm := app.get("sdm"):
        await sdm.close()

    if tm := app.get("token_manager"):
        await tm.stop()


# ---------------------------------------------------------------------------
# App factory and entrypoint
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", _handle_index)
    app.router.add_get("/devices", _handle_devices)
    app.router.add_get("/camera/status", _handle_status)
    app.router.add_post("/camera/start", _handle_start)
    app.router.add_post("/camera/stop", _handle_stop)
    app.router.add_post("/webrtc/offer", _handle_webrtc_offer)

    return app


async def _run() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    print("======== Running on http://0.0.0.0:8080 ========\n(Press CTRL+C to quit)")
    await stop.wait()
    log.info("nestling: shutting down")

    # Close browser PCs and pipelines before the drain phase so WebRTC
    # handlers can exit cleanly before runner.cleanup() waits for connections.
    async with app["registry_lock"]:
        snapshot = list(_registry.values())
    for entry in snapshot:
        for bpc in list(entry.browser_pcs):
            await bpc.close()
        entry.raw_pipeline.close()

    await runner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
