import asyncio
import contextlib
import json
import os
import platform
import shlex
import shutil
import tempfile
import time
from asyncio.streams import StreamReader, StreamWriter, open_connection
from asyncio.subprocess import create_subprocess_exec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from anicli.common import history
from anicli.common.discord_rpc import get_rpc
from anicli.common.m3u import generate_m3u_str_playlist
from anicli.common.sanitizer import sanitize_filename

if TYPE_CHECKING:
    from anicli_api.player.base import Video

PLAYER = "mpv"
TITLE_KEY = "--title"
HEADERS_KEY = "--http-header-fields"
USER_AGENT_KEY = "--user-agent"
REFERER_KEY = "--referrer"


def is_mpv_installed() -> bool:
    return shutil.which("mpv") is not None


class MpvIpcClient:
    def __init__(self):
        self.tmp_dir: Optional[Path] = None
        self.socket_path: Optional[str] = None

        self.reader: Optional[StreamReader] = None
        self.writer: Optional[StreamWriter] = None

    async def __aenter__(self):
        if platform.system() == "Windows":
            self.socket_path = rf"\\.\pipe\mpvserver_{os.getpid()}"
        else:
            self.tmp_dir = Path(tempfile.mkdtemp(prefix="anicli_mpv_"))
            self.socket_path = str(self.tmp_dir / "socket")
            await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            self.reader = None
        if self.tmp_dir and self.tmp_dir.exists():
            for p in self.tmp_dir.iterdir():
                p.unlink()
            self.tmp_dir.rmdir()

    async def connect(self) -> None:
        if self.reader:
            self.reader, self.writer = await open_connection(self.socket_path)

    async def send(self, command: dict):
        line = None
        if platform.system() == "Windows":
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(None, self._windows_send, command)
        if self.reader and self.writer:
            self.writer.write(json.dumps(command).encode() + b"\n")
            await self.writer.drain()
            line = await self.reader.readline()

        if line:
            return json.loads(line.decode("utf-8", errors="ignore"))

    async def read(self):
        line = None
        if platform.system() == "Windows":
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(None, self._windows_read)
        if self.reader:
            line = await self.reader.readline()

        if line:
            return json.loads(line.decode("utf-8", errors="ignore"))

    def _windows_read(self):
        try:
            if self.socket_path:
                with open(self.socket_path, "r+b", buffering=0) as f:
                    return f.readline()
        except Exception:
            return None

    def _windows_send(self, command: dict):
        try:
            if self.socket_path:
                with open(self.socket_path, "w+b", buffering=0) as f:
                    f.write(json.dumps(command).encode() + b"\n")
                    f.flush()
                    for _ in range(5):
                        line = f.readline()
                        if not line:
                            return None
                        try:
                            data = json.loads(line.decode("utf-8", errors="ignore"))
                        except Exception:
                            continue
                        if isinstance(data, dict) and "error" in data:
                            return line
                    return None
        except Exception:
            return None


class MPVController:
    def __init__(
        self,
        videos: Sequence["Video"],
        titles: Sequence[str],
        *,
        mpv_opts: str = "",
        save_time: bool = False,
        start_time: Optional[int] = None,
        rpc_meta: Optional[Dict[str, Any]] = None,
    ):
        self.videos = videos
        self.titles = titles
        self.mpv_opts = mpv_opts
        self.save_time = save_time
        self.start_time: Optional[int] = (
            int(start_time) if (start_time is not None and start_time > 0) else None
        )
        self.last_time: Optional[int] = self.start_time
        self.last_duration: Optional[int] = None
        self.rpc_meta = rpc_meta

        self.ipc: Optional[MpvIpcClient] = None
        self.ipc_socket: Optional[str] = None

    async def play(self):
        async with MpvIpcClient() as ipc:
            self.ipc = ipc
            if self.ipc.socket_path:
                self.ipc_socket = self.ipc.socket_path

            args = []
            first_vid = self.videos[0]

            # all videos same equal provider
            # eg: only aniboom, jutsu, or sibnet
            headers = first_vid.headers
            if headers:
                args.extend(self._build_args(headers))
            if self.mpv_opts:
                args.extend(shlex.split(self.mpv_opts))
            if self.start_time:
                args.append(f"--start={self.start_time}")

            args.append(f"--input-ipc-server={self.ipc_socket}")

            if len(self.videos) == 1:
                title = sanitize_filename(self.titles[0])
                args.extend([f"{TITLE_KEY}={title}", f"--force-media-title={title}", first_vid.url])
                await self._run_player(args)
                return

            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".m3u", encoding="utf-8"
            ) as temp_file:
                raw_playlist = generate_m3u_str_playlist(self.videos, self.titles)
                temp_file.write(raw_playlist)
                playlist_path = temp_file.name
                args.append(playlist_path)
            try:
                await self._run_player(args)
            finally:
                # Ensure cleanup of the temporary file after player exit
                p = Path(playlist_path)
                if p.exists():
                    p.unlink()

    async def _monitor_time(self, interval: float = 2.0):
        if self.ipc:
            is_playlist = len(self.videos) > 1
            last_title = None
            last_time: Optional[int] = None
            last_duration: Optional[int] = None
            last_rpc_paused: Optional[bool] = False if self.rpc_meta else None
            last_rpc_dur: Optional[int] = None
            last_rpc_start: Optional[float] = None
            last_rpc_ep: Optional[str] = str(self.rpc_meta.get("episode") or "") if self.rpc_meta else None
            await asyncio.sleep(interval)
            while True:
                try:
                    # title handler (playlist only; skip raw m3u8 manifest names)
                    if is_playlist:
                        title = await self.get_title()
                        if title and ".m3u8" not in str(title) and title != last_title:
                            last_title = str(title)
                            last_duration = None
                            if self.save_time:
                                history.update_progress(last_time, duration=None, episode=last_title)

                    # time & duration handler
                    if self.ipc:
                        time_resp = await self.ipc.send(
                            {"command": ["get_property", "time-pos"]}
                        )
                        if isinstance(time_resp, dict) and time_resp.get("error") == "success":
                            raw_t = time_resp.get("data")
                            if isinstance(raw_t, (int, float)) and raw_t >= 0:
                                t = int(raw_t)
                                if not (
                                    self.start_time
                                    and self.start_time > 5
                                    and t == 0
                                    and last_time is None
                                ):
                                    if t != last_time:
                                        last_time = t
                                        self.last_time = t
                                        if self.save_time:
                                            history.update_progress(t, duration=last_duration)

                        if last_duration is None:
                            dur_resp = await self.ipc.send(
                                {"command": ["get_property", "duration"]}
                            )
                            if isinstance(dur_resp, dict) and dur_resp.get("error") == "success":
                                raw_d = dur_resp.get("data")
                                if isinstance(raw_d, (int, float)) and raw_d > 0:
                                    last_duration = int(raw_d)
                                    self.last_duration = last_duration
                                    if self.save_time:
                                        history.update_progress(
                                            last_time if last_time is not None else self.last_time,
                                            duration=last_duration,
                                        )

                        if self.rpc_meta:
                            paused = last_rpc_paused if last_rpc_paused is not None else False
                            pause_resp = await self.ipc.send(
                                {"command": ["get_property", "pause"]}
                            )
                            if isinstance(pause_resp, dict) and pause_resp.get("error") == "success":
                                if isinstance(pause_resp.get("data"), bool):
                                    paused = pause_resp["data"]

                            cur_ep = (
                                last_title
                                if (is_playlist and last_title)
                                else str(self.rpc_meta.get("episode") or "")
                            )
                            cur_t = last_time if last_time is not None else self.last_time
                            now = time.time()
                            seeked = (
                                (not paused)
                                and (cur_t is not None)
                                and (
                                    last_rpc_start is None
                                    or abs((now - last_rpc_start) - cur_t) > 3.5
                                )
                            )
                            if (
                                paused != last_rpc_paused
                                or last_duration != last_rpc_dur
                                or cur_ep != last_rpc_ep
                                or seeked
                            ):
                                meta = dict(self.rpc_meta)
                                meta["episode"] = cur_ep
                                await get_rpc().a_set_watching(
                                    **meta,
                                    time_pos=cur_t,
                                    duration=last_duration,
                                    paused=paused,
                                )
                                last_rpc_paused = paused
                                last_rpc_dur = last_duration
                                last_rpc_ep = cur_ep
                                last_rpc_start = (
                                    (time.time() - cur_t)
                                    if (not paused and cur_t is not None)
                                    else None
                                )

                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(1.0)

    async def get_title(self):
        if self.ipc:
            response = await self.ipc.send({"command": ["get_property", "media-title"]})
            if response:
                return response.get("data")

    async def pause(self):
        if self.ipc:
            await self.ipc.send({"command": ["set_property", "pause", True]})

    async def seek(self, seconds: int):
        if self.ipc:
            await self.ipc.send({"command": ["seek", seconds, "absolute"]})

    async def quit(self):
        if self.ipc:
            await self.ipc.send({"command": ["quit"]})

    async def _run_player(self, args: List[str]):
        if self.ipc_socket:
            print(f"Executing: {PLAYER} {' '.join(args)}")
            process = await create_subprocess_exec(PLAYER, *args)

            if self.rpc_meta:
                await get_rpc().a_set_watching(
                    **self.rpc_meta,
                    time_pos=self.start_time or 0,
                    paused=False,
                )

            if self.save_time or self.rpc_meta:
                if platform.system() != "Windows":
                    for _ in range(50):
                        if Path(self.ipc_socket).exists():
                            break
                        await asyncio.sleep(0.1)
                monitor_task = asyncio.create_task(self._monitor_time())

                try:
                    await process.wait()
                finally:
                    monitor_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await monitor_task
            else:
                await process.wait()

    def _build_args(self, headers: Dict[str, str]) -> List[str]:
        """Build shared mpv arguments for headers and extra options."""
        args = []

        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        referer = headers.get("Referer") or headers.get("referer")

        if user_agent:
            args.append(f"{USER_AGENT_KEY}={user_agent}")
        if referer:
            args.append(f"{REFERER_KEY}={referer}")

        # All other headers go to --http-header-fields
        others = [
            f"{k}: {v}"
            for k, v in headers.items()
            if k.lower() not in ("user-agent", "referer")
        ]
        if others:
            args.append(f"{HEADERS_KEY}={','.join(others)}")
        return args
