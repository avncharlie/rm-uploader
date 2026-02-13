from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from aiohttp import web

from textual_serve.server import Server
from textual_serve.app_service import AppService

log = logging.getLogger("textual-serve")

VALID_EXTENSIONS = {".pdf", ".epub"}


class RmUploadServer(Server):
    """Textual-serve Server with an HTTP file-upload endpoint and drag-drop UI."""

    def __init__(
        self,
        command: str,
        host: str = "localhost",
        port: int = 8000,
        title: str | None = None,
        public_url: str | None = None,
        statics_path: str | os.PathLike = "./static",
    ):
        # Our custom templates dir (absolute so Server won't resolve relative to its own package)
        our_templates = (Path(__file__).parent / "templates").resolve()
        super().__init__(
            command=command,
            host=host,
            port=port,
            title=title,
            public_url=public_url,
            statics_path=statics_path,
        )
        # Override templates_path after init since Server resolves relative paths
        # against its own package directory
        self.templates_path = our_templates
        self._app_service: AppService | None = None
        self._temp_dirs: list[str] = []

    async def _make_app(self) -> web.Application:
        app = await super()._make_app()
        app.router.add_post("/upload", self.handle_upload)
        app.router.add_get("/favicon.svg", self._handle_favicon)
        return app

    async def _handle_favicon(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            self.templates_path / "favicon.svg",
            headers={"Content-Type": "image/svg+xml"},
        )

    async def handle_upload(self, request: web.Request) -> web.Response:
        if self._app_service is None:
            return web.json_response(
                {"ok": False, "error": "No active session"}, status=503
            )

        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response(
                {"ok": False, "error": "No file field"}, status=400
            )

        filename = field.filename or "upload"
        ext = Path(filename).suffix.lower()
        if ext not in VALID_EXTENSIONS:
            return web.json_response(
                {"ok": False, "error": f"Unsupported file type: {ext}"},
                status=400,
            )

        tmp_dir = tempfile.mkdtemp(prefix="rm_upload_")
        self._temp_dirs.append(tmp_dir)
        dest = Path(tmp_dir) / filename

        with open(dest, "wb") as f:
            while True:
                chunk = await field.read_chunk(8192)
                if not chunk:
                    break
                f.write(chunk)

        log.info("Uploaded %s (%d bytes)", dest, dest.stat().st_size)

        # Inject the file path as a bracketed paste so the Textual app
        # receives it as an events.Paste (not individual keystrokes)
        path_bytes = str(dest).encode()
        await self._app_service.send_bytes(
            b"\x1b[200~" + path_bytes + b"\x1b[201~"
        )

        return web.json_response({"ok": True, "filename": filename})

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(heartbeat=15)

        from textual_serve.server import to_int

        width = to_int(request.query.get("width", "80"), 80)
        height = to_int(request.query.get("height", "24"), 24)
        app_service: AppService | None = None
        try:
            await websocket.prepare(request)
            app_service = AppService(
                self.command,
                write_bytes=websocket.send_bytes,
                write_str=websocket.send_str,
                close=websocket.close,
                download_manager=self.download_manager,
                debug=self.debug,
            )
            self._app_service = app_service
            await app_service.start(width, height)
            try:
                await self._process_messages(websocket, app_service)
            finally:
                await app_service.stop()
        except Exception as error:
            log.exception(error)
        finally:
            if app_service is not None:
                await app_service.stop()
            if self._app_service is app_service:
                self._app_service = None
        return websocket

    async def on_shutdown(self, app: web.Application) -> None:
        await super().on_shutdown(app)
        for tmp_dir in self._temp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._temp_dirs.clear()
