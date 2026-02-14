from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import shlex
import tempfile
from pathlib import Path

from rich.markup import escape
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
DEFAULT_SSH_KEY = "~/.ssh/id_rsa_remarkable"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        existing = _load_config()
        existing.update(data)
        CONFIG_FILE.write_text(json.dumps(existing) + "\n")
    except OSError:
        pass


def _load_saved_ip() -> str:
    return _load_config().get("ip", DEFAULT_IP)


def _load_saved_rsync() -> str:
    return _load_config().get("rsync", "rsync")


def _load_saved_ssh_key() -> str | None:
    return _load_config().get("ssh_key")


def _load_saved_mirror_host() -> str | None:
    return _load_config().get("mirror_host")


def _load_saved_mirror_path() -> str | None:
    return _load_config().get("mirror_path")


def _load_saved_mirror_key() -> str | None:
    return _load_config().get("mirror_key")


def _save_ip(ip: str) -> None:
    _save_config({"ip": ip})

# Dark reMarkable colour scheme (inverted from rm_viewer CSS)
# Warm dark browns/greys inspired by the reMarkable palette


def _build_steps(mirror: bool) -> tuple[list[str], dict[str, int]]:
    """Build step labels and name→index mapping, optionally including a mirror step."""
    labels = ["Connecting..."]
    if mirror:
        labels.append("Syncing to mirror...")
    labels += ["Uploading file...", "Writing metadata...", "Restarting xochitl...", "Done!"]
    idx: dict[str, int] = {}
    for i, label in enumerate(labels):
        if label.startswith("Connecting"):
            idx["connect"] = i
        elif label.startswith("Syncing"):
            idx["mirror"] = i
        elif label.startswith("Uploading"):
            idx["upload"] = i
        elif label.startswith("Writing"):
            idx["metadata"] = i
        elif label.startswith("Restarting"):
            idx["restart"] = i
        elif label.startswith("Done"):
            idx["done"] = i
    return labels, idx


def _render_steps(
    steps: list[str], current: int, error: str | None = None, detail: str = ""
) -> str:
    lines = []
    for i, label in enumerate(steps):
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
        yield Input(value=self._current_ip, placeholder="Tablet IP", id="ip-input")

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
        ("i", "set_ip", "Change tablet IP"),
        ("u", "set_usb_ip", "Set to USB IP"),
        ("ctrl+q", "cancel_upload", "Cancel upload"),
    ]

    ENABLE_COMMAND_PALETTE = False
    TITLE = "reMarkable uploader"

    def __init__(
        self,
        ip: str = "192.168.7.237",
        rsync_path: str = "rsync",
        ssh_key: str | None = None,
        mirror_host: str | None = None,
        mirror_path: str | None = None,
        mirror_key: str | None = None,
    ) -> None:
        super().__init__()
        self.ip = ip
        self.rsync_path = rsync_path
        self.ssh_key = ssh_key
        self.mirror_host = mirror_host
        self.mirror_path = mirror_path
        self.mirror_key = mirror_key
        self._uploading = False
        self._upload_worker: Worker | None = None
        self._step_labels, self._steps = _build_steps(mirror=bool(mirror_path))

    def _make_uploader(self) -> RemarkableUploader:
        return RemarkableUploader(
            ip=self.ip,
            rsync_path=self.rsync_path,
            ssh_key=Path(self.ssh_key).expanduser() if self.ssh_key else None,
            mirror_host=self.mirror_host,
            mirror_path=self.mirror_path,
            mirror_key=Path(self.mirror_key).expanduser() if self.mirror_key else None,
        )

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield Static("Drag and drop", id="drop-hint")
        yield ProgressBar(total=100, show_eta=False, id="progress-bar")
        yield Static("", id="steps")
        yield Static("", id="message")
        yield Footer()

    def on_mount(self) -> None:
        self.action_test_connection()

    def action_set_usb_ip(self) -> None:
        self.ip = "10.11.99.1"
        self.action_test_connection()

    def action_set_ip(self) -> None:
        def _on_dismiss(new_ip: str | None) -> None:
            if new_ip:
                self.ip = new_ip
                _save_ip(new_ip)
                self.action_test_connection()

        self.push_screen(IpScreen(_load_saved_ip()), callback=_on_dismiss)

    def _set_message(self, msg: str) -> None:
        self.query_one("#message", Static).update(f"  {msg}")

    def _set_steps(self, current: int, error: str | None = None, detail: str = "") -> None:
        self.query_one("#steps", Static).update(
            _render_steps(self._step_labels, current, error=error, detail=detail)
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
        uploader = self._make_uploader()
        if await uploader.test_connection():
            self._set_message("[#7a9a6a]Connected[/]")
        else:
            self._set_message("[#cc6666]Connection failed[/]")

    def action_cancel_upload(self) -> None:
        if self._upload_worker and self._upload_worker.is_running:
            self._upload_worker.cancel()
            self._uploading = False
            self._set_steps(self._steps["connect"], error="Cancelled")
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
        uploader = self._make_uploader()
        job = UploadJob(filepath=filepath)
        s = self._steps
        mirror_done = False

        try:
            # Step: Connect
            self._set_steps(s["connect"])
            self._set_message(f"Uploading {filepath.name} ({job.size_mb})")
            if not await uploader.test_connection():
                self._set_steps(s["connect"], error="Connection failed")
                self._set_message(f"Check IP ({self.ip}) and SSH key.")
                return

            # Step: Mirror (if enabled)
            if uploader.mirror_enabled:
                self._set_steps(s["mirror"])
                mirror_log = lambda msg: self._set_message(f"[#7a7268]{msg}[/]")

                def on_mirror_progress(j: UploadJob) -> None:
                    pct = int(j.progress * 100)
                    progress_bar.progress = pct
                    self._set_steps(s["mirror"], detail=f"{pct}%")

                await uploader.mirror_upload(
                    job, on_log=mirror_log, on_progress=on_mirror_progress
                )
                mirror_done = True
                progress_bar.progress = 0

            # Step: Upload to device
            self._set_steps(s["upload"])

            def on_progress(j: UploadJob) -> None:
                pct = int(j.progress * 100)
                progress_bar.progress = pct
                self._set_steps(s["upload"], detail=f"{pct}%")

            await uploader.upload_file(job, on_progress=on_progress)
            progress_bar.progress = 100

            # Step: Restart
            self._set_steps(s["restart"])
            await uploader.restart_xochitl()

            # Done
            self._set_steps(s["done"])
            self._set_message(f"[#7a9a6a]{filepath.name} uploaded![/]")

        except asyncio.CancelledError:
            self._set_steps(s["connect"], error="Cancelled")
            self._set_message("Upload cancelled.")
            if mirror_done:
                await uploader.mirror_cleanup(job, on_log=lambda _: None)
            await uploader.device_cleanup(job)
        except Exception as e:
            self._set_message(f"[#cc6666]Error: {escape(str(e))}[/]")
            if mirror_done:
                await uploader.mirror_cleanup(job, on_log=lambda _: None)
            await uploader.device_cleanup(job)
        finally:
            self._uploading = False
            self._upload_worker = None
            # Clean up temp files (from web uploads)
            tmp = str(Path(tempfile.gettempdir()))
            if str(filepath.parent).startswith(tmp):
                shutil.rmtree(filepath.parent, ignore_errors=True)


def main() -> None:
    saved_ip = _load_saved_ip()
    saved_rsync = _load_saved_rsync()
    saved_ssh_key = _load_saved_ssh_key()
    saved_mirror_host = _load_saved_mirror_host()
    saved_mirror_path = _load_saved_mirror_path()
    saved_mirror_key = _load_saved_mirror_key()

    parser = argparse.ArgumentParser(description="Upload PDFs/EPUBs to reMarkable")
    parser.add_argument("ip", nargs="?", default=saved_ip, help="Tablet IP address")
    parser.add_argument("--ip", dest="ip_flag", default=None, help="Tablet IP address")
    parser.add_argument("--web", action="store_true", help="Serve via browser")
    parser.add_argument("--port", type=int, default=8765, help="Port for web server")
    parser.add_argument("--rsync", default=saved_rsync, help="Path to rsync binary")
    parser.add_argument(
        "--ssh-key",
        default=saved_ssh_key or DEFAULT_SSH_KEY,
        help="SSH key for the reMarkable device",
    )
    parser.add_argument("--mirror-host", default=saved_mirror_host, help="Mirror remote host (e.g. user@server)")
    parser.add_argument("--mirror-path", default=saved_mirror_path, help="Remote xochitl directory on mirror host")
    parser.add_argument("--mirror-key", default=saved_mirror_key, help="SSH key for the mirror host")
    args = parser.parse_args()
    ip = args.ip_flag or args.ip

    if args.rsync != "rsync":
        _save_config({"rsync": args.rsync})

    if args.ssh_key != DEFAULT_SSH_KEY:
        _save_config({"ssh_key": args.ssh_key})

    if args.mirror_host:
        _save_config({"mirror_host": args.mirror_host})
    if args.mirror_path:
        _save_config({"mirror_path": args.mirror_path})
    if args.mirror_key:
        _save_config({"mirror_key": args.mirror_key})

    app = RmUploadApp(
        ip=ip,
        rsync_path=args.rsync,
        ssh_key=args.ssh_key,
        mirror_host=args.mirror_host,
        mirror_path=args.mirror_path,
        mirror_key=args.mirror_key,
    )

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
