from __future__ import annotations

import asyncio
import atexit
import json
import os
import platform
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from anicli.common.config import get_config_dir
from anicli.common.history import format_time

DEFAULT_CLIENT_ID = "448016723057049601"
DEFAULT_CUSTOM_IMAGE = "https://media1.tenor.com/m/T6cnb8csQAMAAAAC/%E3%81%A1%E3%82%87%E3%81%93%E3%81%88%E3%81%84-chocoeiru.gif"

DEFAULT_RPC_CFG: Dict[str, Any] = {
    "enabled": True,
    "client_id": DEFAULT_CLIENT_ID,
    "custom_image": DEFAULT_CUSTOM_IMAGE,
    "use_anime_posters": False,
    "show_idle": True,
    "show_buttons": True,
    "show_dub": True,
    "show_source": True,
}

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


def get_rpc_config_path() -> Path:
    return get_config_dir() / "discord_rpc.json"


def load_rpc_config() -> Dict[str, Any]:
    path = get_rpc_config_path()
    if not path.exists():
        try:
            path.write_text(json.dumps(DEFAULT_RPC_CFG, ensure_ascii=False, indent=4), encoding="utf-8")
        except OSError:
            pass
        return dict(DEFAULT_RPC_CFG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            merged = dict(DEFAULT_RPC_CFG)
            merged.update(raw)
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return dict(DEFAULT_RPC_CFG)


def save_rpc_config(cfg: Dict[str, Any]) -> None:
    path = get_rpc_config_path()
    try:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
    except OSError:
        pass


def _clip(text: str, max_len: int = 128) -> str:
    clean = " ".join(str(text).split()).strip()
    if not clean:
        return "anicli-ru"
    if len(clean) == 1:
        clean = clean + "\u200b"
    if len(clean) > max_len:
        return clean[: max_len - 1].rstrip() + "…"
    return clean


def _valid_url(url: Optional[str], max_len: int = 256) -> Optional[str]:
    if not url:
        return None
    u = str(url).strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith(("http://", "https://")) and len(u) <= max_len:
        return u
    return None


class _DiscordIPC:
    """Pure-stdlib local IPC transport to the running Discord desktop client."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self._lock = threading.Lock()
        self._conn: Any = None
        self._is_windows = platform.system() == "Windows"
        self._last_attempt: float = 0.0
        self._supports_display_type = True

    def _candidate_paths(self) -> List[str]:
        if self._is_windows:
            return [rf"\\.\pipe\discord-ipc-{i}" for i in range(10)]
        roots = [
            os.environ.get("XDG_RUNTIME_DIR"),
            os.environ.get("TMPDIR"),
            os.environ.get("TMP"),
            os.environ.get("TEMP"),
            "/tmp",
        ]
        paths: List[str] = []
        for root in roots:
            if not root:
                continue
            for sub in ("", "app/com.discordapp.Discord", "snap.discord"):
                base = os.path.join(root, sub) if sub else root
                for i in range(10):
                    paths.append(os.path.join(base, f"discord-ipc-{i}"))
        return paths

    def _raw_write(self, data: bytes) -> None:
        if self._is_windows:
            self._conn.write(data)
            self._conn.flush()
        else:
            self._conn.sendall(data)

    def _raw_read(self, n: int) -> bytes:
        chunks: List[bytes] = []
        remaining = n
        while remaining > 0:
            chunk = self._conn.read(remaining) if self._is_windows else self._conn.recv(remaining)
            if not chunk:
                raise EOFError("Discord IPC closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, op: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = struct.pack("<II", op, len(raw))
        self._raw_write(header + raw)

    def _recv_frame(self) -> Dict[str, Any]:
        while True:
            header = self._raw_read(8)
            op, length = struct.unpack("<II", header)
            body = self._raw_read(length) if length > 0 else b"{}"
            if op == OP_PING:
                try:
                    self._send_frame(OP_PONG, json.loads(body.decode("utf-8", errors="ignore")))
                except Exception:
                    pass
                continue
            if op == OP_CLOSE:
                raise EOFError("Discord IPC closed by peer")
            data = json.loads(body.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                return data
            return {}

    def _close_locked(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _ensure_connected_locked(self) -> bool:
        if self._conn is not None:
            return True
        now = time.monotonic()
        if now - self._last_attempt < 15.0:
            return False
        self._last_attempt = now

        for candidate in self._candidate_paths():
            try:
                if self._is_windows:
                    if not os.path.exists(candidate):
                        continue
                    conn = open(candidate, "r+b", buffering=0)
                else:
                    if not os.path.exists(candidate):
                        continue
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(3.0)
                    sock.connect(candidate)
                    conn = sock
                self._conn = conn
                self._send_frame(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                reply = self._recv_frame()
                if reply.get("evt") == "ERROR":
                    self._close_locked()
                    continue
                return True
            except Exception:
                self._close_locked()
        return False

    def set_activity(self, activity: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._ensure_connected_locked():
                return None

            payload_activity = dict(activity) if activity is not None else None
            used_display_type = False
            if payload_activity is not None and "status_display_type" in payload_activity:
                if not self._supports_display_type:
                    payload_activity.pop("status_display_type", None)
                else:
                    used_display_type = True

            for attempt in range(2):
                try:
                    if not self._ensure_connected_locked():
                        return None
                    req = {
                        "cmd": "SET_ACTIVITY",
                        "args": {"pid": os.getpid(), "activity": payload_activity},
                        "nonce": str(uuid.uuid4()),
                    }
                    self._send_frame(OP_FRAME, req)
                    reply = self._recv_frame()
                    if (
                        reply.get("evt") == "ERROR"
                        and used_display_type
                        and payload_activity is not None
                        and "status_display_type" in payload_activity
                    ):
                        self._supports_display_type = False
                        payload_activity.pop("status_display_type", None)
                        used_display_type = False
                        continue
                    return reply
                except Exception:
                    self._close_locked()
                    self._last_attempt = 0.0
                    if attempt == 0:
                        continue
                    return None
        return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._send_frame(
                        OP_FRAME,
                        {
                            "cmd": "SET_ACTIVITY",
                            "args": {"pid": os.getpid(), "activity": None},
                            "nonce": str(uuid.uuid4()),
                        },
                    )
                except Exception:
                    pass
                self._close_locked()


class DiscordRPCManager:
    def __init__(self) -> None:
        self._cfg = load_rpc_config()
        self._ipc = _DiscordIPC(str(self._cfg.get("client_id") or DEFAULT_CLIENT_ID))
        self._session_start = int(time.time())

    def reload_config(self) -> Dict[str, Any]:
        self._cfg = load_rpc_config()
        cid = str(self._cfg.get("client_id") or DEFAULT_CLIENT_ID)
        if cid != self._ipc.client_id:
            self._ipc.close()
            self._ipc = _DiscordIPC(cid)
        return self._cfg

    def is_enabled(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    def toggle(self) -> bool:
        new_val = not self.is_enabled()
        self._cfg["enabled"] = new_val
        save_rpc_config(self._cfg)
        if not new_val:
            self.clear()
        return new_val

    def _is_default_app(self) -> bool:
        return str(self._cfg.get("client_id") or DEFAULT_CLIENT_ID) == DEFAULT_CLIENT_ID

    def _resolve_large_image(self, thumbnail: Optional[str] = None) -> Optional[str]:
        custom_img = _valid_url(self._cfg.get("custom_image"), 256)
        poster_img = _valid_url(thumbnail, 256)
        if self._cfg.get("use_anime_posters", False):
            return poster_img or custom_img or ("mpv" if self._is_default_app() else None)
        return custom_img or poster_img or ("mpv" if self._is_default_app() else None)

    def set_browsing(self, source_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_enabled() or not self._cfg.get("show_idle", True):
            return self.clear()

        details = f"Выбирает аниме · {source_name}" if source_name else "Выбирает аниме"
        activity: Dict[str, Any] = {
            "type": 3,
            "status_display_type": 1,
            "details": _clip(details, 128),
            "state": "anicli-ru",
            "timestamps": {"start": self._session_start},
        }
        large_img = self._resolve_large_image(None)
        if large_img:
            activity["assets"] = {
                "large_image": large_img,
                "large_text": "anicli-ru",
            }
        return self._ipc.set_activity(activity)

    def set_watching(
        self,
        anime_title: str,
        episode: str,
        *,
        dub: Optional[str] = None,
        source_name: Optional[str] = None,
        thumbnail: Optional[str] = None,
        quality: Optional[int] = None,
        time_pos: Optional[int] = None,
        duration: Optional[int] = None,
        paused: bool = False,
        page_url: Optional[str] = None,
        episode_num: Optional[int] = None,
        total_episodes: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.is_enabled():
            return None

        show_dub = bool(self._cfg.get("show_dub", True))
        show_source = bool(self._cfg.get("show_source", True))
        show_buttons = bool(self._cfg.get("show_buttons", True))

        ep_text = str(episode or "Episode").strip()
        if (
            episode_num is not None
            and total_episodes is not None
            and total_episodes > 1
            and f"/{total_episodes}" not in ep_text
        ):
            ep_text = f"{ep_text} ({episode_num}/{total_episodes})"

        state_parts: List[str] = []
        if paused:
            state_parts.append("⏸ Пауза")
            state_parts.append(ep_text)
            if time_pos is not None and time_pos >= 0:
                if duration and duration > 0:
                    state_parts.append(f"{format_time(time_pos)} / {format_time(duration)}")
                else:
                    state_parts.append(format_time(time_pos))
            if show_dub and dub:
                state_parts.append(str(dub).strip())
        else:
            state_parts.append(ep_text)
            if show_dub and dub:
                state_parts.append(str(dub).strip())
            elif show_source and source_name:
                state_parts.append(str(source_name).strip())

        activity: Dict[str, Any] = {
            "type": 3,
            "status_display_type": 2,
            "details": _clip(anime_title, 128),
            "state": _clip(" · ".join(p for p in state_parts if p), 128),
        }

        assets: Dict[str, str] = {}
        large_img = self._resolve_large_image(thumbnail)
        if large_img:
            assets["large_image"] = large_img

        large_text_parts = [
            anime_title,
            f"{quality}p" if quality else None,
            source_name if show_source else None,
        ]
        if assets.get("large_image"):
            assets["large_text"] = _clip(" · ".join(p for p in large_text_parts if p), 128)

        if self._is_default_app():
            assets["small_image"] = "player_pause" if paused else "player_play"
            if paused:
                if time_pos is not None and duration and duration > 0:
                    small_txt = f"Пауза · {format_time(time_pos)} / {format_time(duration)}"
                elif time_pos is not None and time_pos >= 0:
                    small_txt = f"Пауза · {format_time(time_pos)}"
                else:
                    small_txt = "Пауза"
            else:
                small_parts = [
                    "Смотрит",
                    f"{quality}p" if quality else None,
                    source_name if show_source else None,
                ]
                small_txt = " · ".join(p for p in small_parts if p)
            assets["small_text"] = _clip(small_txt, 128)

        if assets:
            activity["assets"] = assets

        if not paused:
            now = int(time.time())
            pos = max(0, int(time_pos or 0))
            start_ts = now - pos
            if duration and duration > 0 and time_pos is not None:
                activity["timestamps"] = {"start": start_ts, "end": start_ts + int(duration)}
            else:
                activity["timestamps"] = {"start": start_ts}

        if show_buttons:
            valid_btn_url = _valid_url(page_url, 512)
            if valid_btn_url:
                btn_label = f"Открыть на {source_name}" if (show_source and source_name) else "Открыть аниме"
                activity["buttons"] = [{"label": _clip(btn_label, 32), "url": valid_btn_url}]

        return self._ipc.set_activity(activity)

    def clear(self) -> Optional[Dict[str, Any]]:
        return self._ipc.set_activity(None)

    def close(self) -> None:
        self._ipc.close()

    async def a_set_browsing(self, source_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.set_browsing, source_name)

    async def a_set_watching(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.set_watching, *args, **kwargs)

    async def a_clear(self) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.clear)

    async def a_close(self) -> None:
        await asyncio.to_thread(self.close)


_RPC: Optional[DiscordRPCManager] = None


def get_rpc() -> DiscordRPCManager:
    global _RPC
    if _RPC is None:
        _RPC = DiscordRPCManager()
        atexit.register(_RPC.close)
    return _RPC
