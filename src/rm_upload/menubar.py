"""macOS menubar app for rm-upload — single-click toggle, no dropdown."""

from __future__ import annotations

import asyncio
import threading
import webbrowser
from pathlib import Path

import AppKit
import objc
from aiohttp.web_runner import GracefulExit
from PyObjCTools import AppHelper

from rm_upload.app import _load_saved_ip
from rm_upload.web_server import RmUploadServer


SERVER_PORT = 8765
SERVER_URL = f"http://localhost:{SERVER_PORT}"
_ICONS_DIR = Path(__file__).parent
ICON_OFF = str(_ICONS_DIR / "icon_off.png")
ICON_ON = str(_ICONS_DIR / "icon_on.png")
ICON_SIZE = (18, 18)  # points — images are 36x36px @2x retina


class MenubarServer(RmUploadServer):
    """RmUploadServer with thread-safe stop capability."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None

    def serve(self, debug: bool = False) -> None:
        self.debug = debug
        self.initialize_logging()

        loop = asyncio.new_event_loop()
        self._loop = loop

        from aiohttp import web

        web.run_app(
            self._make_app(),
            host=self.host,
            port=self.port,
            handle_signals=False,
            loop=loop,
            print=lambda *args: None,
        )

    def stop(self) -> None:
        """Thread-safe server stop."""
        if self._loop and self._loop.is_running():

            def _raise_exit():
                raise GracefulExit()

            self._loop.call_soon_threadsafe(_raise_exit)


class MenubarDelegate(AppKit.NSObject):
    """NSApplication delegate that owns the status bar item."""

    def init(self):
        self = objc.super(MenubarDelegate, self).init()
        if self is None:
            return None
        self.server = None
        self.server_thread = None
        self._running = False
        return self

    def applicationDidFinishLaunching_(self, notification):
        # Create status bar item
        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )

        # Load icons
        self.icon_off = self._load_icon(ICON_ON)
        self.icon_on = self._load_icon(ICON_OFF)
        self.status_item.button().setImage_(self.icon_off)

        # Direct click action — no menu
        self.status_item.button().setTarget_(self)
        self.status_item.button().setAction_(b"toggle:")

    @staticmethod
    def _load_icon(path):
        image = AppKit.NSImage.alloc().initByReferencingFile_(path)
        image.setScalesWhenResized_(True)
        image.setSize_(ICON_SIZE)
        image.setTemplate_(True)
        return image

    def toggle_(self, sender):
        if self._running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self):
        ip = _load_saved_ip()
        self.server = MenubarServer(
            command=f"python -m rm_upload.app {ip}",
            port=SERVER_PORT,
        )

        self.server_thread = threading.Thread(target=self.server.serve, daemon=True)
        self.server_thread.start()
        self._running = True

        # Swap to "on" icon
        self.status_item.button().setImage_(self.icon_on)

        # Open browser after a short delay
        self.performSelector_withObject_afterDelay_(b"_openBrowser", None, 1.5)

    def _openBrowser(self):
        webbrowser.open(SERVER_URL)

    def _stop_server(self):
        self._running = False
        self.status_item.button().setImage_(self.icon_off)

        server = self.server
        thread = self.server_thread
        self.server = None
        self.server_thread = None

        def _shutdown():
            if server:
                server.stop()
            if thread:
                thread.join(timeout=5)

        threading.Thread(target=_shutdown, daemon=True).start()


def main():
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = MenubarDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
