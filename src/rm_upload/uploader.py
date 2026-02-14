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
        rsync_path: str = "rsync",
        mirror_host: str | None = None,
        mirror_path: str | None = None,
        mirror_key: Path | None = None,
    ):
        self.ip = ip
        self.ssh_key = ssh_key or Path.home() / ".ssh" / "id_rsa_remarkable"
        self.parent_uuid = parent_uuid
        self.rsync_path = rsync_path
        self.remote = f"root@{ip}"
        self.remote_dir = "/home/root/.local/share/remarkable/xochitl"
        self.ssh_opts = [
            "-i", str(self.ssh_key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]

        # Mirror config
        self.mirror_host = mirror_host
        self.mirror_path = mirror_path
        self.mirror_key = mirror_key
        self.mirror_enabled = bool(mirror_path)
        self._mirror_remote = bool(mirror_host)
        if self._mirror_remote:
            self._mirror_ssh_opts = [
                "-i", str(mirror_key or Path.home() / ".ssh" / "id_rsa"),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
            ]

    def _ssh_cmd(self, remote_cmd: str) -> list[str]:
        return ["ssh", *self.ssh_opts, self.remote, remote_cmd]

    def _ssh_e_string(self) -> str:
        return "ssh " + " ".join(self.ssh_opts)

    def _mirror_ssh_cmd(self, remote_cmd: str) -> list[str]:
        assert self.mirror_host is not None
        return ["ssh", *self._mirror_ssh_opts, self.mirror_host, remote_cmd]

    def _mirror_ssh_e_string(self) -> str:
        return "ssh " + " ".join(self._mirror_ssh_opts)

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
        """Upload file to the reMarkable device (device only, no mirror)."""
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
            self.rsync_path, "-a", "--info=progress2",
            "-e", self._ssh_e_string(),
            str(job.filepath), remote_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
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
        except (asyncio.CancelledError, Exception):
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            assert proc.stderr is not None
            stderr = (await proc.stderr.read()).decode(errors="replace")
            raise RuntimeError(f"rsync failed (exit {proc.returncode}): {stderr}")

    def _metadata_json(self, job: UploadJob) -> str:
        now_ms = str(int(time.time() * 1000))
        return json.dumps({
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

    def _content_json(self, job: UploadJob) -> str:
        return json.dumps({
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

    async def _write_metadata(self, job: UploadJob) -> None:
        remote_path = f"{self.remote_dir}/{job.uuid}.metadata"
        await self._ssh_write(remote_path, self._metadata_json(job))

    async def _write_content(self, job: UploadJob) -> None:
        remote_path = f"{self.remote_dir}/{job.uuid}.content"
        await self._ssh_write(remote_path, self._content_json(job))

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

    async def _mirror_write(self, path: str, data: str) -> None:
        if self._mirror_remote:
            proc = await asyncio.create_subprocess_exec(
                *self._mirror_ssh_cmd(f"cat > '{path}'"),
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=data.encode()), timeout=30
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Mirror SSH write failed: {stderr.decode(errors='replace')}"
                )
        else:
            Path(path).write_text(data)

    async def mirror_upload(
        self,
        job: UploadJob,
        on_log: Callable[[str], None],
        on_progress: Callable[[UploadJob], None] | None = None,
    ) -> None:
        assert self.mirror_path is not None
        progress = on_progress or (lambda _: None)
        on_log(f"Mirror: rsyncing {job.filepath.name}...")

        if self._mirror_remote:
            # Remote mirror via SSH
            dest = f"{self.mirror_host}:{self.mirror_path}/{job.uuid}.{job.ext}"
            cmd = [
                self.rsync_path, "-a", "--info=progress2",
                "-e", self._mirror_ssh_e_string(),
                str(job.filepath), dest,
            ]
        else:
            # Local mirror
            dest = f"{self.mirror_path}/{job.uuid}.{job.ext}"
            cmd = [self.rsync_path, "-a", "--info=progress2",
                   str(job.filepath), dest]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                buf += chunk
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
                        progress(job)

            await proc.wait()
        except (asyncio.CancelledError, Exception):
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            assert proc.stderr is not None
            stderr = (await proc.stderr.read()).decode(errors="replace")
            raise RuntimeError(
                f"Mirror rsync failed (exit {proc.returncode}): {stderr}"
            )

        # Write metadata JSON to mirror
        on_log("Mirror: writing metadata...")
        metadata_path = f"{self.mirror_path}/{job.uuid}.metadata"
        await self._mirror_write(metadata_path, self._metadata_json(job))

        # Write content JSON to mirror
        on_log("Mirror: writing content descriptor...")
        content_path = f"{self.mirror_path}/{job.uuid}.content"
        await self._mirror_write(content_path, self._content_json(job))

        on_log("Mirror: upload complete.")

    async def mirror_cleanup(self, job: UploadJob, on_log: Callable[[str], None]) -> None:
        on_log("Mirror: cleaning up after device upload failure...")
        files = [
            f"{self.mirror_path}/{job.uuid}.{job.ext}",
            f"{self.mirror_path}/{job.uuid}.metadata",
            f"{self.mirror_path}/{job.uuid}.content",
        ]
        try:
            if self._mirror_remote:
                rm_cmd = "rm -f " + " ".join(f"'{f}'" for f in files)
                proc = await asyncio.create_subprocess_exec(
                    *self._mirror_ssh_cmd(rm_cmd),
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
            else:
                for f in files:
                    Path(f).unlink(missing_ok=True)
        except Exception:
            pass  # Best-effort cleanup

    async def device_cleanup(self, job: UploadJob) -> None:
        """Best-effort removal of all files for a job's uuid from the device.

        Removes both final files ({uuid}.*) and rsync temp files (.{uuid}.*).
        """
        rm_cmd = (
            f"rm -f '{self.remote_dir}/{job.uuid}'.*"
            f" '{self.remote_dir}/.{job.uuid}'.*"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._ssh_cmd(rm_cmd),
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            pass

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
