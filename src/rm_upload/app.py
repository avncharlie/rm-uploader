from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import shlex
import tempfile
from pathlib import Path

from textual import events, work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    ProgressBar,
    Static,
)
from textual.worker import Worker

from rm_upload.uploader import RemarkableUploader, UploadJob

VALID_EXTENSIONS = {".pdf", ".epub"}

CONFIG_DIR = Path.home() / ".config" / "rm-upload"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_IP = "192.168.7.237"


def _load_saved_ip() -> str:
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data.get("ip", DEFAULT_IP)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DEFAULT_IP


def _save_ip(ip: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"ip": ip})  + "\n")
    except OSError:
        pass

STEP_CONNECT = 0
STEP_UPLOAD = 1
STEP_METADATA = 2
STEP_RESTART = 3
STEP_DONE = 4

STEPS = [
    "Connecting...",
    "Uploading file...",
    "Writing metadata...",
    "Restarting xochitl...",
    "Done!",
]

# Dark reMarkable colour scheme (inverted from rm_viewer CSS)
# Warm dark browns/greys inspired by the reMarkable palette


def _render_steps(current: int, error: str | None = None, detail: str = "") -> str:
    lines = []
    for i, label in enumerate(STEPS):
        if error and i == current:
            lines.append(f"  [#cc6666]x[/]  {label}  [#cc6666]{error}[/]")
        elif i < current:
            lines.append(f"  [#7a9a6a]v[/]  {label}")
        elif i == current:
            suffix = f"  [#7a7268]{detail}[/]" if detail else ""
            lines.append(f"  [#d4cdc0]>[/]  [bold]{label}[/]{suffix}")
        else:
            lines.append(f"  [#4a4440]-  {label}[/]")
    return "\n".join(lines)


class IpScreen(ModalScreen[str]):
    CSS = """
    IpScreen {
        align: center middle;
    }

    IpScreen #ip-input {
        width: 50;
    }
    """

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, current_ip: str) -> None:
        super().__init__()
        self._current_ip = current_ip

    def compose(self) -> ComposeResult:
        yield Input(value="10.11.99.1", placeholder="Tablet IP", id="ip-input")

    def on_mount(self) -> None:
        self.query_one("#ip-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class RmUploadApp(App):
    CSS = """
    Screen {
        background: #2a2520;
        color: #d4cdc0;
    }

    Header {
        background: #352f29;
        color: #d4cdc0;
        height: 1 !important;
    }

    Header.-tall {
        height: 1 !important;
    }

    HeaderIcon {
        display: none;
    }

    HeaderClockSpace {
        display: none;
    }

    Footer {
        background: #352f29;
        color: #d4cdc0;
    }

    FooterKey {
        background: #3e3832;
        color: #d4cdc0;
    }


    #drop-hint {
        height: 1fr;
        min-height: 3;
        content-align: center middle;
        color: #7a7268;
        margin: 1 2;
    }

    #progress-bar {
        height: auto;
        width: 100%;
        margin: 0;
        padding: 0;
    }

    #progress-bar Bar {
        width: 1fr;
    }

    #progress-bar Bar > .bar--bar {
        color: #c87840;
        background: #3e3832;
    }

    #progress-bar Bar > .bar--complete {
        color: #c87840;
        background: #3e3832;
    }

    #progress-bar PercentageStatus {
        display: none;
    }

    #steps {
        height: auto;
        min-height: 7;
        padding: 1 2;
        color: #d4cdc0;
    }

    #message {
        height: 1;
        padding: 0 2;
        color: #d4cdc0;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("t", "test_connection", "Test"),
        ("i", "set_ip", "Set IP"),
        ("ctrl+q", "cancel_upload", "Cancel upload"),
    ]

    ENABLE_COMMAND_PALETTE = False
    TITLE = "reMarkable uploader"

    def __init__(self, ip: str = "192.168.7.237") -> None:
        super().__init__()
        self.ip = ip
        self._uploading = False
        self._upload_worker: Worker | None = None

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield Static("Drag and drop", id="drop-hint")
        yield ProgressBar(total=100, show_eta=False, id="progress-bar")
        yield Static("", id="steps")
        yield Static("", id="message")
        yield Footer()

    def on_mount(self) -> None:
        self.action_test_connection()

    def action_set_ip(self) -> None:
        def _on_dismiss(new_ip: str | None) -> None:
            if new_ip:
                self.ip = new_ip
                _save_ip(new_ip)
                self.action_test_connection()

        self.push_screen(IpScreen(self.ip), callback=_on_dismiss)

    def _set_message(self, msg: str) -> None:
        self.query_one("#message", Static).update(f"  {msg}")

    def _set_steps(self, current: int, error: str | None = None, detail: str = "") -> None:
        self.query_one("#steps", Static).update(
            _render_steps(current, error=error, detail=detail)
        )

    def _parse_pasted_paths(self, text: str) -> list[Path]:
        paths = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            raw = Path(line).expanduser().resolve()
            if raw.exists():
                paths.append(raw)
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = [line]
            for token in tokens:
                token = token.strip()
                if token:
                    paths.append(Path(token).expanduser().resolve())
        return paths

    def on_paste(self, event: events.Paste) -> None:
        paths = self._parse_pasted_paths(event.text)
        for p in paths:
            if not p.is_file():
                self._set_message(f"Not a file: {p}")
                continue
            if p.suffix.lower() not in VALID_EXTENSIONS:
                self._set_message(f"Unsupported: {p.suffix} ({p.name})")
                continue
            self._start_upload(p)
            return

    @work(exclusive=True)
    async def action_test_connection(self) -> None:
        self._set_message(f"[#7a7268]Connecting to {self.ip}...[/]")
        uploader = RemarkableUploader(ip=self.ip)
        if await uploader.test_connection():
            self._set_message("[#7a9a6a]Connected[/]")
        else:
            self._set_message("[#cc6666]Connection failed[/]")

    def action_cancel_upload(self) -> None:
        if self._upload_worker and self._upload_worker.is_running:
            self._upload_worker.cancel()
            self._uploading = False
            self._set_steps(STEP_CONNECT, error="Cancelled")
            self._set_message("Upload cancelled.")

    def _start_upload(self, filepath: Path) -> None:
        if self._uploading:
            self._set_message("Upload in progress — press Ctrl+Q to cancel.")
            return
        self._upload_worker = self._do_upload(filepath)

    @work(exclusive=True)
    async def _do_upload(self, filepath: Path) -> None:
        self._uploading = True
        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.progress = 0
        uploader = RemarkableUploader(ip=self.ip)
        job = UploadJob(filepath=filepath)

        try:
            # Step: Connect
            self._set_steps(STEP_CONNECT)
            self._set_message(f"Uploading {filepath.name} ({job.size_mb})")
            if not await uploader.test_connection():
                self._set_steps(STEP_CONNECT, error="Connection failed")
                self._set_message(f"Check IP ({self.ip}) and SSH key.")
                return

            # Step: Upload
            self._set_steps(STEP_UPLOAD)

            def on_progress(j: UploadJob) -> None:
                pct = int(j.progress * 100)
                progress_bar.progress = pct
                self._set_steps(STEP_UPLOAD, detail=f"{pct}%")

            await uploader.upload_file(job, on_progress=on_progress)
            progress_bar.progress = 100

            # Step: Metadata (already written by upload_file, just show it)
            self._set_steps(STEP_RESTART)

            # Step: Restart
            await uploader.restart_xochitl()

            # Done
            self._set_steps(STEP_DONE)
            self._set_message(f"[#7a9a6a]{filepath.name} uploaded![/]")

        except asyncio.CancelledError:
            self._set_steps(STEP_CONNECT, error="Cancelled")
            self._set_message("Upload cancelled.")
        except Exception as e:
            self._set_message(f"[#cc6666]Error: {e}[/]")
        finally:
            self._uploading = False
            self._upload_worker = None
            # Clean up temp files (from web uploads)
            tmp = str(Path(tempfile.gettempdir()))
            if str(filepath.parent).startswith(tmp):
                shutil.rmtree(filepath.parent, ignore_errors=True)


def main() -> None:
    saved_ip = _load_saved_ip()
    parser = argparse.ArgumentParser(description="Upload PDFs/EPUBs to reMarkable")
    parser.add_argument("ip", nargs="?", default=saved_ip, help="Tablet IP address")
    parser.add_argument("--ip", dest="ip_flag", default=None, help="Tablet IP address")
    parser.add_argument("--web", action="store_true", help="Serve via browser")
    parser.add_argument("--port", type=int, default=8765, help="Port for web server")
    args = parser.parse_args()
    ip = args.ip_flag or args.ip

    app = RmUploadApp(ip=ip)

    if args.web:
        from rm_upload.web_server import RmUploadServer

        server = RmUploadServer(
            command="python -m rm_upload.app", port=args.port
        )
        server.serve()
    else:
        app.run()


if __name__ == "__main__":
    main()
