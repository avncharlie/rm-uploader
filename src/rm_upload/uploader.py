from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class UploadJob:
    filepath: Path
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"  # pending, uploading, metadata, done, error
    progress: float = 0.0
    error: str | None = None

    @property
    def ext(self) -> str:
        return self.filepath.suffix.lower().lstrip(".")

    @property
    def visible_name(self) -> str:
        return self.filepath.stem

    @property
    def size_mb(self) -> str:
        size = self.filepath.stat().st_size
        if size < 1024 * 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size / (1024 * 1024):.1f} MB"


class RemarkableUploader:
    def __init__(
        self,
        ip: str = "192.168.7.237",
        ssh_key: Path | None = None,
        parent_uuid: str = "",
    ):
        self.ip = ip
        self.ssh_key = ssh_key or Path.home() / ".ssh" / "id_rsa_remarkable"
        self.parent_uuid = parent_uuid
        self.remote = f"root@{ip}"
        self.remote_dir = "/home/root/.local/share/remarkable/xochitl"
        self.ssh_opts = [
            "-i", str(self.ssh_key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]

    def _ssh_cmd(self, remote_cmd: str) -> list[str]:
        return ["ssh", *self.ssh_opts, self.remote, remote_cmd]

    def _ssh_e_string(self) -> str:
        return "ssh " + " ".join(self.ssh_opts)

    async def test_connection(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._ssh_cmd("echo ok"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and b"ok" in stdout
        except (asyncio.TimeoutError, OSError):
            return False

    async def upload_file(
        self,
        job: UploadJob,
        on_progress: Callable[[UploadJob], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        log = on_log or (lambda _: None)
        progress = on_progress or (lambda _: None)

        job.status = "uploading"
        job.progress = 0.0
        progress(job)

        log(f"Uploading {job.filepath.name} → {job.uuid}")
        await self._rsync_file(job, progress, log)

        job.status = "metadata"
        progress(job)
        log("Writing metadata...")
        await self._write_metadata(job)

        log("Writing content descriptor...")
        await self._write_content(job)

        job.status = "done"
        job.progress = 1.0
        progress(job)
        log(f"Done: {job.filepath.name}")

    async def _rsync_file(
        self,
        job: UploadJob,
        on_progress: Callable[[UploadJob], None],
        on_log: Callable[[str], None],
    ) -> None:
        remote_path = f"{self.remote}:{self.remote_dir}/{job.uuid}.{job.ext}"
        cmd = [
            "rsync", "-a", "--info=progress2",
            "-e", self._ssh_e_string(),
            str(job.filepath), remote_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            # rsync uses \r to overwrite the same line
            lines = buf.replace(b"\r", b"\n").split(b"\n")
            buf = lines[-1]
            for line in lines[:-1]:
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                match = re.search(r"(\d+)%", text)
                if match:
                    pct = int(match.group(1))
                    job.progress = pct / 100.0
                    on_progress(job)

        await proc.wait()
        if proc.returncode != 0:
            assert proc.stderr is not None
            stderr = (await proc.stderr.read()).decode(errors="replace")
            raise RuntimeError(f"rsync failed (exit {proc.returncode}): {stderr}")

    async def _write_metadata(self, job: UploadJob) -> None:
        now_ms = str(int(time.time() * 1000))
        metadata = json.dumps({
            "deleted": False,
            "lastModified": now_ms,
            "metadatamodified": True,
            "modified": True,
            "parent": self.parent_uuid,
            "pinned": False,
            "synced": False,
            "type": "DocumentType",
            "version": 1,
            "visibleName": job.visible_name,
        }, indent=4)

        remote_path = f"{self.remote_dir}/{job.uuid}.metadata"
        await self._ssh_write(remote_path, metadata)

    async def _write_content(self, job: UploadJob) -> None:
        content = json.dumps({
            "dummyDocument": False,
            "extraMetadata": {},
            "fileType": job.ext,
            "fontName": "",
            "lastOpenedPage": 0,
            "legacyEpub": False,
            "lineHeight": -1,
            "margins": 100,
            "orientation": "portrait",
            "pageCount": 0,
            "textScale": 1,
            "transform": {
                "m11": 1, "m12": 0, "m13": 0,
                "m21": 0, "m22": 1, "m23": 0,
                "m31": 0, "m32": 0, "m33": 1,
            },
            "pages": [],
            "redirectionPageMap": [],
        }, indent=4)

        remote_path = f"{self.remote_dir}/{job.uuid}.content"
        await self._ssh_write(remote_path, content)

    async def _ssh_write(self, remote_path: str, data: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_cmd(f"cat > '{remote_path}'"),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=data.encode()), timeout=30
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"SSH write failed: {stderr.decode(errors='replace')}"
            )

    async def restart_xochitl(self, on_log: Callable[[str], None] | None = None) -> None:
        log = on_log or (lambda _: None)
        log("Restarting xochitl...")
        proc = await asyncio.create_subprocess_exec(
            *self._ssh_cmd(
                "systemctl reset-failed xochitl && systemctl restart xochitl"
            ),
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"xochitl restart failed: {stderr.decode(errors='replace')}"
            )
        log("xochitl restarted.")
