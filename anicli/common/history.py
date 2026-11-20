import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import attr

from anicli.common.config import get_history_path
from anicli.common.extractors import dynamic_load_extractor_module

if TYPE_CHECKING:
    from anicli_api.base import BaseSource


def read() -> List[Dict[str, Any]]:
    """Read history from JSON file."""
    path = get_history_path()
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _write(entries: List[Dict[str, Any]]) -> None:
    """Atomically write history entries to disk."""
    path = get_history_path()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def format_time(seconds: Optional[int]) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    if seconds is None or seconds < 0:
        return "00:00"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def clean_episode_name(entry: Dict[str, Any]) -> str:
    """Return a clean episode name, falling back to source episode_title if corrupted by stream URL/manifest."""
    ep = str(entry.get("episode") or "").strip()
    if not ep or ".m3u8" in ep or ep.startswith(("http://", "https://")):
        sources = entry.get("source") or []
        if sources and isinstance(sources, list):
            fallback = str(sources[0].get("episode_title") or "").strip()
            if fallback:
                return fallback
    return ep or "Unknown"


def _same_anime(entry: Dict[str, Any], extractor_name: str, title: str, url: str = "") -> bool:
    data = entry.get("data") or {}
    entry_url = data.get("url") or ""
    if url not in ("", "_") and entry.get("extractor_name") == extractor_name and entry_url == url:
        return True
    if title and data.get("title") == title:
        return True
    return False


def find_entry(extractor_name: str, item: Any, anime: Any = None) -> Optional[Dict[str, Any]]:
    """Find matching history entry for an anime item."""
    item_url = getattr(item, "url", "") or ""
    titles = {t for t in (getattr(item, "title", ""), getattr(anime, "title", "")) if t}
    for entry in read():
        data = entry.get("data") or {}
        if item_url not in ("", "_") and entry.get("extractor_name") == extractor_name and data.get("url") == item_url:
            return entry
        if titles and data.get("title") in titles:
            return entry
    return None


def get_episode_progress_from_entry(
    entry: Optional[Dict[str, Any]],
    episode_label: str,
    episode_ordinal: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Get saved progress dict {time, duration, episode_ordinal, source_title} for an episode."""
    if not entry:
        return None

    ep_map = entry.get("episodes_progress") or {}
    if episode_label in ep_map and isinstance(ep_map[episode_label], dict):
        return ep_map[episode_label]

    if episode_ordinal is not None:
        for val in ep_map.values():
            if isinstance(val, dict) and val.get("episode_ordinal") == episode_ordinal:
                return val

    # Fallback to top-level entry (for legacy entries without episodes_progress)
    top_ep = clean_episode_name(entry)
    top_ord = entry.get("episode_ordinal")
    matched = top_ep == episode_label or (episode_ordinal is not None and top_ord == episode_ordinal)
    if not matched and episode_ordinal is not None:
        m = re.search(r"\d+", top_ep)
        if m and int(m.group()) == episode_ordinal:
            matched = True

    if matched and entry.get("time") is not None:
        sources = entry.get("source") or []
        src_title = sources[0].get("title") if sources and isinstance(sources, list) else None
        return {
            "time": entry.get("time"),
            "duration": entry.get("duration"),
            "episode_ordinal": top_ord,
            "source_title": src_title,
        }
    return None


def _prepare_entry(
    item: Any,
    extractor_name: str,
    source: Optional[Dict[str, "BaseSource"]] = None,
    episode: str = "Unknown",
) -> Dict[str, Any]:
    """Serialize attr object for storage."""
    full_item_data = attr.asdict(item)
    clean_item_data = {k: v for k, v in full_item_data.items() if not k.startswith("_")}
    source_arr = []
    if source:
        for ep_title, s in source.items():
            full_source_data = attr.asdict(s)
            clean_source_data = {
                k: v for k, v in full_source_data.items() if not k.startswith("_")
            }

            source_arr.append(
                {
                    "episode_title": ep_title,
                    "title": clean_source_data.pop("title", "Unknown"),
                    "url": clean_source_data.pop("url", ""),
                    "data": clean_source_data,
                }
            )

    return {
        "extractor_name": extractor_name,
        "type": item.__class__.__name__,
        "episode": episode,
        "time": None,
        "source": source_arr,
        "data": {
            "title": clean_item_data.pop("title", "Unknown"),
            "thumbnail": clean_item_data.pop("thumbnail", ""),
            "url": clean_item_data.pop("url", ""),
            "data": clean_item_data,
        },
    }


def save(
    item: Any,
    extractor_name: str,
    source: Optional[Dict[str, "BaseSource"]] = None,
    episode: str = "Unknown",
    limit: int = 50,
    *,
    time: Optional[int] = None,
    duration: Optional[int] = None,
    episode_ordinal: Optional[int] = None,
    reset_time: bool = False,
) -> None:
    """Save item to history with deduplication, per-episode progress carry-over, and limit."""
    entry = _prepare_entry(
        item,
        extractor_name,
        source,
        episode,
    )
    entry["duration"] = None
    entry["episode_ordinal"] = episode_ordinal
    entry["episodes_progress"] = {}

    title = entry["data"].get("title", "")
    url = entry["data"].get("url", "")
    history = read()

    old: Optional[Dict[str, Any]] = None
    remaining: List[Dict[str, Any]] = []
    for e in history:
        if old is None and _same_anime(e, extractor_name, title, url):
            old = e
        elif not _same_anime(e, extractor_name, title, url):
            remaining.append(e)

    episodes_progress: Dict[str, Any] = {}
    if old is not None:
        episodes_progress = dict(old.get("episodes_progress") or {})
        old_ep = clean_episode_name(old)
        if old.get("time") is not None and old_ep not in episodes_progress:
            old_sources = old.get("source") or []
            old_src_title = old_sources[0].get("title") if old_sources and isinstance(old_sources, list) else None
            episodes_progress[old_ep] = {
                "time": old.get("time"),
                "duration": old.get("duration"),
                "episode_ordinal": old.get("episode_ordinal"),
                "source_title": old_src_title,
            }

    prev_ep = episodes_progress.get(episode) or {}
    eff_duration = duration if duration is not None else prev_ep.get("duration")
    if reset_time:
        eff_time: Optional[int] = 0
    elif time is not None:
        eff_time = int(time)
    elif prev_ep.get("time") is not None:
        eff_time = prev_ep["time"]
    else:
        eff_time = None

    sources = entry.get("source") or []
    src_title = sources[0].get("title") if sources and isinstance(sources, list) else None

    entry["time"] = eff_time
    entry["duration"] = eff_duration
    episodes_progress[episode] = {
        "time": eff_time,
        "duration": eff_duration,
        "episode_ordinal": episode_ordinal,
        "source_title": src_title,
    }
    entry["episodes_progress"] = episodes_progress

    remaining.insert(0, entry)
    _write(remaining[:limit])


def update_last(element: Dict[str, Any]) -> None:
    history = read()
    if not history:
        return
    history[0].update(element)
    _write(history)


def update_progress(
    time: Optional[int],
    duration: Optional[int] = None,
    episode: Optional[str] = None,
) -> None:
    """Update top history entry's playback time, duration, and per-episode progress."""
    history = read()
    if not history:
        return
    top = history[0]
    if episode is not None:
        top["episode"] = episode
    if time is not None:
        top["time"] = int(time)
    if duration is not None:
        top["duration"] = int(duration)

    ep_name = clean_episode_name(top)
    ep_map = top.setdefault("episodes_progress", {})
    prev_ep = ep_map.get(ep_name) if isinstance(ep_map.get(ep_name), dict) else {}
    sources = top.get("source") or []
    src_title = sources[0].get("title") if sources and isinstance(sources, list) else prev_ep.get("source_title")
    ep_map[ep_name] = {
        "time": top.get("time"),
        "duration": top.get("duration") if top.get("duration") is not None else prev_ep.get("duration"),
        "episode_ordinal": top.get("episode_ordinal") if top.get("episode_ordinal") is not None else prev_ep.get("episode_ordinal"),
        "source_title": src_title,
    }
    _write(history)


def bump_to_top(index: int) -> None:
    """Move entry at index to index 0."""
    history = read()
    if 0 < index < len(history):
        entry = history.pop(index)
        history.insert(0, entry)
        _write(history)


def load_item_with_extractor(entry: Dict[str, Any]) -> Tuple[Any, Any]:
    """Restore (extractor_instance, item_obj) for a single history entry."""
    ext_name = entry["extractor_name"]
    model_type = entry["type"]
    payload = entry["data"]
    try:
        module = dynamic_load_extractor_module(ext_name)
        ext_instance = module.Extractor()
        model_cls = getattr(module, model_type)

        kwargs = {
            **payload.get("data", {}),
            **ext_instance._kwargs_http,
            **getattr(ext_instance, "_kwargs_api", {}),
        }

        obj = model_cls(
            title=payload["title"],
            thumbnail=payload["thumbnail"],
            url=payload["url"],
            **kwargs,
        )
        return ext_instance, obj
    except Exception as e:
        msg = f"Load error {ext_name} ({model_type})"
        raise RuntimeError(msg) from e


def load() -> List[Any]:
    """Restore extractor objects from history."""
    raw_data = read()
    results = []

    for entry in raw_data:
        _, obj = load_item_with_extractor(entry)
        results.append(obj)

    return results


def load_source(entry: Dict[str, Any], episode_title: str) -> "BaseSource":
    """Restore source objects from history."""
    ext_name = entry["extractor_name"]
    model_type = entry["type"]
    sources: List = entry["source"]
    payload = sources[0]
    for s in sources:
        if s["episode_title"] == episode_title:
            payload = s
            break

    try:
        module = dynamic_load_extractor_module(ext_name)
        ext_instance = module.Extractor()
        model_cls = module.Source

        kwargs = {
            **payload.get("data", {}),
            **ext_instance._kwargs_http,
            **getattr(ext_instance, "_kwargs_api", {}),
        }

        obj = model_cls(
            title=payload["title"],
            url=payload["url"],
            **kwargs,
        )

        return obj
    except Exception as e:
        msg = f"Load error {ext_name} ({model_type})"
        raise RuntimeError(msg) from e
