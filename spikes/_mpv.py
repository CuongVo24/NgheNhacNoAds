"""Minimal asyncio client for mpv's JSON IPC, shared by spikes S1 and S3.

Windows uses a named pipe (ProactorEventLoop.create_pipe_connection); other
platforms use a unix socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any


class MpvError(RuntimeError):
    pass


def find_mpv(path: str = "mpv") -> str | None:
    # On Windows the mpv folder also has mpv.com, a console wrapper that PATHEXT
    # prefers over mpv.exe; the daemon wants the real player.
    if sys.platform == "win32" and not path.lower().endswith((".exe", ".com")):
        found = shutil.which(path + ".exe")
        if found:
            return found
    return shutil.which(path)


def mpv_version(mpv: str) -> str:
    result = subprocess.run(
        [mpv, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.splitlines()[0].strip()


def new_ipc_path() -> str:
    token = secrets.token_hex(4)
    if sys.platform == "win32":
        return rf"\\.\pipe\nghenhacnoads-spike-{token}"
    return f"/tmp/nghenhacnoads-spike-{token}.sock"


class Mpv:
    """An mpv process started in idle mode and controlled over JSON IPC."""

    def __init__(self, mpv: str, extra_args: list[str] | None = None) -> None:
        self.mpv = mpv
        self.extra_args = extra_args or []
        self.ipc_path = new_ipc_path()
        self.events: list[tuple[float, dict[str, Any]]] = []
        self._new_event = asyncio.Event()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        args = [
            self.mpv,
            "--idle=yes",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            "--no-config",
            "--ytdl=no",
            "--no-input-default-bindings",
            "--cache=yes",
            f"--input-ipc-server={self.ipc_path}",
            *self.extra_args,
        ]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        await self._connect()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _connect(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    loop = asyncio.get_running_loop()
                    reader = asyncio.StreamReader(limit=1 << 20)
                    protocol = asyncio.StreamReaderProtocol(reader)
                    transport, _ = await loop.create_pipe_connection(  # type: ignore[attr-defined]
                        lambda: protocol, self.ipc_path
                    )
                    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
                else:
                    reader, writer = await asyncio.open_unix_connection(
                        self.ipc_path, limit=1 << 20
                    )
            except OSError:
                assert self._proc is not None
                if time.monotonic() > deadline or self._proc.returncode is not None:
                    raise
                await asyncio.sleep(0.05)
            else:
                self._reader, self._writer = reader, writer
                return

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while line := await self._reader.readline():
            message = json.loads(line)
            request_id = message.get("request_id")
            if request_id in self._pending:
                future = self._pending.pop(request_id)
                if message.get("error") == "success":
                    future.set_result(message.get("data"))
                else:
                    future.set_exception(MpvError(message.get("error")))
            elif "event" in message:
                self.events.append((time.perf_counter(), message))
                self._new_event.set()
        for future in self._pending.values():
            future.set_exception(MpvError("mpv IPC connection closed"))
        self._pending.clear()

    async def command(self, command: list[Any] | dict[str, Any], timeout: float = 10.0) -> Any:
        assert self._writer is not None
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": command, "request_id": request_id}, ensure_ascii=False)
        self._writer.write(payload.encode("utf-8") + b"\n")
        await self._writer.drain()
        return await asyncio.wait_for(future, timeout)

    async def get(self, prop: str) -> Any:
        return await self.command(["get_property", prop])

    async def set(self, prop: str, value: Any) -> Any:
        return await self.command(["set_property", prop, value])

    async def wait_event(
        self, name: str, timeout: float, since: int = 0, **match: Any
    ) -> tuple[float, dict[str, Any]]:
        """Wait for an event called ``name`` at index >= ``since`` whose fields match ``match``."""
        deadline = time.monotonic() + timeout
        index = since
        while True:
            while index < len(self.events):
                stamp, event = self.events[index]
                index += 1
                if event.get("event") == name and all(event.get(k) == v for k, v in match.items()):
                    return stamp, event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no {name!r} event within {timeout}s")
            self._new_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new_event.wait(), remaining)

    async def wait_property(
        self, prop: str, predicate: Any, timeout: float, interval: float = 0.1
    ) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            with contextlib.suppress(MpvError):
                value = await self.get(prop)
                if predicate(value):
                    return value
            if time.monotonic() > deadline:
                raise TimeoutError(f"{prop} did not reach the expected value within {timeout}s")
            await asyncio.sleep(interval)

    async def quit(self) -> None:
        with contextlib.suppress(Exception):
            await self.command(["quit"], timeout=2)
        if self._proc is not None:
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
