"""macOS menubar app for rm-upload — single-click toggle, no dropdown."""

from __future__ import annotations

import asyncio
import sys
import threading
import webbrowser
from pathlib import Path

import AppKit
import objc
from aiohttp.web_runner import GracefulExit
from PyObjCTools import AppHelper

from rm_upload.app import _load_saved_ip, _load_saved_rsync
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

        # Build the menu shown when server is running
        self._running_menu = AppKit.NSMenu.alloc().init()
        open_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open uploader", b"openBrowser:", ""
        )
        open_item.setTarget_(self)
        stop_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Shutdown", b"stopServer:", ""
        )
        stop_item.setTarget_(self)
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", b"quitApp:", ""
        )
        quit_item.setTarget_(self)
        self._running_menu.addItem_(stop_item)
        self._running_menu.addItem_(open_item)
        self._running_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._running_menu.addItem_(quit_item)

        # Start with direct click action (no menu)
        self._set_click_mode()

    @staticmethod
    def _load_icon(path):
        image = AppKit.NSImage.alloc().initByReferencingFile_(path)
        image.setScalesWhenResized_(True)
        image.setSize_(ICON_SIZE)
        image.setTemplate_(True)
        return image

    def _set_click_mode(self):
        """Direct click — starts the server."""
        self.status_item.setMenu_(None)
        self.status_item.button().setTarget_(self)
        self.status_item.button().setAction_(b"startClicked:")

    def _set_menu_mode(self):
        """Show dropdown menu with stop/quit."""
        self.status_item.button().setTarget_(None)
        self.status_item.button().setAction_(None)
        self.status_item.setMenu_(self._running_menu)

    def startClicked_(self, sender):
        self._start_server()

    def openBrowser_(self, sender):
        webbrowser.open(SERVER_URL)

    def stopServer_(self, sender):
        self._stop_server()

    def quitApp_(self, sender):
        self._stop_server()
        AppKit.NSApplication.sharedApplication().terminate_(None)

    def _start_server(self):
        ip = _load_saved_ip()
        rsync = _load_saved_rsync()
        self.server = MenubarServer(
            command=f"{sys.executable} -m rm_upload.app {ip} --rsync {rsync}",
            port=SERVER_PORT,
        )

        self.server_thread = threading.Thread(target=self.server.serve, daemon=True)
        self.server_thread.start()
        self._running = True

        # Swap to "on" icon and show menu on next click
        self.status_item.button().setImage_(self.icon_on)
        self._set_menu_mode()

        # Open browser after a short delay
        self.performSelector_withObject_afterDelay_(b"_openBrowser", None, 1.5)

    def _openBrowser(self):
        webbrowser.open(SERVER_URL)

    def _stop_server(self):
        self._running = False
        self.status_item.button().setImage_(self.icon_off)
        self._set_click_mode()

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
    import argparse

    parser = argparse.ArgumentParser(description="reMarkable uploader menubar")
    parser.add_argument("--rsync", default=None, help="Path to rsync binary")
    args = parser.parse_args()

    if args.rsync:
        from rm_upload.app import _save_config

        _save_config({"rsync": args.rsync})

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = MenubarDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
