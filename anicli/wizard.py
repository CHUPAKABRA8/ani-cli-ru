"""Interactive arrow-key wizard: source -> title -> episode -> player/dub -> play (+ post-play & continue watching)."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich import get_console

from anicli.common import history
from anicli.common.discord_rpc import get_rpc
from anicli.common.extractors import dynamic_load_extractor_module, get_extractor_modules
from anicli.common.mpv import MPVController, is_mpv_installed

CONSOLE = get_console()
PAGE = 14
DEFAULT_QUALITY = 2060


async def pick(title: str, options: Sequence[Tuple[Any, str]], default: int = 0, hint: str = "") -> Any:
    """Arrow-key list with scrolling and type-to-filter. Returns key or None (Esc)."""
    all_opts = list(options)
    st = {"i": default if 0 <= default < len(all_opts) else 0, "q": ""}

    def visible() -> List[Tuple[Any, str]]:
        q = st["q"].lower()
        return [o for o in all_opts if q in o[1].lower()] if q else all_opts

    kb = KeyBindings()

    def clamp():
        v = visible()
        st["i"] = max(0, min(st["i"], len(v) - 1)) if v else 0

    @kb.add("up")
    def _(e):
        v = visible()
        if v:
            st["i"] = (st["i"] - 1) % len(v)

    @kb.add("down")
    def _(e):
        v = visible()
        if v:
            st["i"] = (st["i"] + 1) % len(v)

    @kb.add("pageup")
    def _(e):
        st["i"] = max(0, st["i"] - PAGE)

    @kb.add("pagedown")
    def _(e):
        st["i"] = min(max(0, len(visible()) - 1), st["i"] + PAGE)

    @kb.add("home")
    def _(e):
        st["i"] = 0

    @kb.add("end")
    def _(e):
        st["i"] = max(0, len(visible()) - 1)

    @kb.add("enter")
    def _(e):
        v = visible()
        e.app.exit(result=v[st["i"]][0] if v else None)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    @kb.add("backspace")
    def _(e):
        st["q"] = st["q"][:-1]
        clamp()

    @kb.add("<any>")
    def _(e):
        ch = e.data
        if ch and ch.isprintable():
            st["q"] += ch
            st["i"] = 0

    def render():
        v = visible()
        out: List[Tuple[str, str]] = [("bold", title + "\n")]
        if st["q"]:
            out.append(("fg:ansiyellow", f"filter: {st['q']}\n"))
        if not v:
            out.append(("fg:ansired", "  nothing found\n"))
        start = max(0, min(st["i"] - PAGE // 2, max(0, len(v) - PAGE)))
        for n in range(start, min(len(v), start + PAGE)):
            label = v[n][1]
            if n == st["i"]:
                out.append(("reverse bold", f" > {label} \n"))
            else:
                out.append(("", f"   {label} \n"))
        if len(v) > PAGE:
            out.append(("italic", f"  [{st['i'] + 1}/{len(v)}]\n"))
        out.append(("italic", hint or "arrows: move | Enter: select | Esc: back | type: filter"))
        return FormattedText(out)

    app = Application(
        layout=Layout(Window(FormattedTextControl(render), always_hide_cursor=True)),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
    )
    return await app.run_async()


async def ask_text(message: str) -> Optional[str]:
    try:
        text = await PromptSession().prompt_async(message)
    except (EOFError, KeyboardInterrupt):
        return None
    text = text.strip()
    return text or None


def _ep_label(ep) -> str:
    t = (ep.title or "").strip()
    return t if str(ep.ordinal) in t else f"{ep.ordinal}. {t}"


def _fmt_progress(time_sec: Optional[int], dur_sec: Optional[int] = None) -> str:
    t_str = history.format_time(time_sec)
    if dur_sec is not None and dur_sec > 0:
        return f"{t_str} / {history.format_time(dur_sec)}"
    return t_str


def _find_episode_index(episodes: Sequence[Any], entry: dict) -> int:
    if not episodes:
        return 0
    target_ord = entry.get("episode_ordinal")
    if target_ord is not None:
        for i, ep in enumerate(episodes):
            if ep.ordinal == target_ord:
                return i

    ep_name = history.clean_episode_name(entry)
    for i, ep in enumerate(episodes):
        candidates = {
            _ep_label(ep),
            f"{ep.ordinal} {ep.title}".strip(),
            (ep.title or "").strip(),
        }
        if ep_name in candidates:
            return i

    m = re.search(r"\d+", ep_name)
    if m:
        num = int(m.group())
        for i, ep in enumerate(episodes):
            if ep.ordinal == num:
                return i
        if 0 <= num - 1 < len(episodes):
            return num - 1

    return 0


def _episode_options(
    name: str,
    item: Any,
    anime: Any,
    episodes: Sequence[Any],
) -> Tuple[List[Tuple[int, str]], int]:
    entry = history.find_entry(name, item, anime)
    opts: List[Tuple[int, str]] = []
    for i, ep in enumerate(episodes):
        ep_lbl = _ep_label(ep)
        prog = history.get_episode_progress_from_entry(entry, ep_lbl, ep.ordinal)
        t = prog.get("time") if prog else None
        dur = prog.get("duration") if prog else None
        if t is not None and t > 0:
            opts.append((i, f"{ep_lbl}  [{_fmt_progress(t, dur)}]"))
        else:
            opts.append((i, ep_lbl))
    default_idx = _find_episode_index(episodes, entry) if entry else 0
    return opts, default_idx


def _make_extractor(name: str):
    return dynamic_load_extractor_module(name).Extractor()


def _resolve_page_url(name: str, item: Any) -> Optional[str]:
    raw = getattr(item, "url", "") or ""
    if raw.startswith(("http://", "https://")):
        return raw
    if name == "animeon" and raw and raw != "_":
        return f"https://animeon.cc/anime/{raw}"
    return None


def _build_rpc_meta(
    name: str,
    item: Any,
    anime: Any,
    ep: Any,
    ep_idx: int,
    episodes: Sequence[Any],
    src: Any,
    video: Any,
) -> Dict[str, Any]:
    return {
        "anime_title": anime.title,
        "episode": _ep_label(ep),
        "episode_num": ep_idx + 1,
        "total_episodes": len(episodes),
        "dub": getattr(src, "title", None),
        "source_name": name,
        "thumbnail": getattr(anime, "thumbnail", None) or getattr(item, "thumbnail", None),
        "quality": getattr(video, "quality", None),
        "page_url": _resolve_page_url(name, item),
    }


async def _close(extractor) -> None:
    try:
        extractor.http.close()
        await extractor.http_async.aclose()
    except Exception:  # noqa: BLE001
        pass


async def _choose_source_or_continue(current: Optional[str], allow_continue: bool = False) -> Optional[str]:
    names = sorted(get_extractor_modules())
    options: List[Tuple[str, str]] = []
    if allow_continue:
        entries = history.read()
        if entries:
            latest = entries[0]
            latest_title = (latest.get("data") or {}).get("title") or "Unknown"
            latest_ep = history.clean_episode_name(latest)
            latest_t = latest.get("time")
            time_part = f", {history.format_time(latest_t)}" if latest_t is not None and latest_t > 0 else ""
            options.append(("__continue__", f"Continue watching ({latest_title} — {latest_ep}{time_part})"))
    options.extend((n, n) for n in names)
    default = 0
    if not allow_continue and current in names:
        default = names.index(current)
    elif allow_continue and current in names:
        for idx, (k, _) in enumerate(options):
            if k == current:
                default = idx
                break
    return await pick("Select source (site)", options, default)


async def _play_loop(
    extractor,
    name: str,
    item,
    anime,
    episodes,
    ep_idx: int,
    opts: str,
    *,
    start_time: Optional[int] = None,
    preferred_source_title: Optional[str] = None,
    auto_pick_source: bool = False,
    back_label: str = "Back to results",
) -> None:
    from anicli_api.tools.helpers import get_video_by_quality

    force_pick = not auto_pick_source
    last_src_title: Optional[str] = preferred_source_title
    last_src_idx = 0
    while True:
        ep = episodes[ep_idx]
        ep_lbl = _ep_label(ep)

        if start_time is None:
            entry = history.find_entry(name, item, anime)
            prog = history.get_episode_progress_from_entry(entry, ep_lbl, ep.ordinal)
            saved_t = prog.get("time") if prog else None
            saved_dur = prog.get("duration") if prog else None
            if saved_t is not None and saved_t > 0:
                t_str = _fmt_progress(saved_t, saved_dur)
                prompt_opts: List[Tuple[str, str]] = [
                    ("resume", f"Continue {ep_lbl} from {t_str}"),
                ]
                if ep_idx + 1 < len(episodes):
                    prompt_opts.append(("next", f"Next episode ({_ep_label(episodes[ep_idx + 1])})"))
                prompt_opts.append(("restart", "Start from beginning (00:00)"))
                prompt_opts.append(("back", "Back"))
                act = await pick(
                    f"{anime.title} — {ep_lbl}",
                    prompt_opts,
                    0,
                    "arrows: move | Enter: select | Esc: back",
                )
                if act in (None, "back"):
                    return
                if act == "resume":
                    start_time = saved_t
                    if last_src_title is None and prog and prog.get("source_title"):
                        last_src_title = prog.get("source_title")
                elif act == "restart":
                    start_time = 0
                elif act == "next":
                    ep_idx += 1
                    start_time = None
                    continue
            else:
                start_time = 0

        try:
            sources = list(await ep.a_get_sources())
        except Exception as ex:  # noqa: BLE001
            CONSOLE.print(f"[red]sources error: {ex}[/red]")
            return
        if not sources:
            CONSOLE.print("[red]no sources for this episode[/red]")
            return

        src = None
        if not force_pick and last_src_title is not None:
            src = next((s for s in sources if s.title == last_src_title), None)
        if src is None:
            labels = [(i, str(s)) for i, s in enumerate(sources)]
            default_src = min(last_src_idx, len(sources) - 1)
            if last_src_title is not None:
                matched_idx = next((i for i, s in enumerate(sources) if s.title == last_src_title), None)
                if matched_idx is not None:
                    default_src = matched_idx
            idx = await pick(f"{ep_lbl}: select player / dub", labels, default_src)
            if idx is None:
                return
            src = sources[idx]
            last_src_idx = idx
        last_src_title = src.title
        force_pick = False

        try:
            videos = await src.a_get_videos()
        except Exception as ex:  # noqa: BLE001
            CONSOLE.print(f"[red]video error: {ex}[/red]")
            videos = []
        if not videos:
            CONSOLE.print("[red]video not found for this player. Choose another.[/red]")
            force_pick = True
            continue

        video = get_video_by_quality(videos, DEFAULT_QUALITY)
        title = f"{anime.title} - {ep_lbl}"
        eff_start = start_time if (start_time is not None and start_time > 0) else None
        try:
            history.save(
                item,
                name,
                {ep_lbl: src},
                ep_lbl,
                time=eff_start,
                episode_ordinal=ep.ordinal,
                reset_time=(start_time == 0),
            )
        except Exception:  # noqa: BLE001
            pass

        if eff_start:
            CONSOLE.print(f"[bold]Playing:[/bold] {title} ({src}) from {history.format_time(eff_start)}")
        else:
            CONSOLE.print(f"[bold]Playing:[/bold] {title} ({src})")

        rpc_meta = _build_rpc_meta(name, item, anime, ep, ep_idx, episodes, src, video)
        ctrl = MPVController(
            [video],
            [title],
            mpv_opts=opts,
            save_time=True,
            start_time=eff_start,
            rpc_meta=rpc_meta,
        )
        await ctrl.play()
        await get_rpc().a_set_browsing(name)

        last_t = ctrl.last_time
        last_dur = ctrl.last_duration
        if last_t is None or last_dur is None:
            entries_now = history.read()
            if entries_now:
                if last_t is None:
                    last_t = entries_now[0].get("time")
                if last_dur is None:
                    last_dur = entries_now[0].get("duration")

        while True:
            has_next = ep_idx + 1 < len(episodes)
            has_prev = ep_idx > 0
            menu: List[Tuple[str, str]] = []
            if has_next:
                menu.append(("n", f"Next episode ({_ep_label(episodes[ep_idx + 1])})"))
            if last_t is not None and last_t > 0:
                menu.append(("c", f"Continue from {history.format_time(last_t)}"))
            menu.append(("r", "Replay from beginning (00:00)"))
            if has_prev:
                menu.append(("p", f"Previous episode ({_ep_label(episodes[ep_idx - 1])})"))
            menu += [("e", "Choose episode"), ("d", "Change player / dub"), ("m", back_label)]

            header = title
            if last_t is not None and last_t > 0:
                header = f"{title}  [{_fmt_progress(last_t, last_dur)}]"
            ch = await pick(header, menu, 0, f"arrows: move | Enter: select | Esc: {back_label.lower()}")
            if ch in (None, "m"):
                return
            if ch == "n":
                ep_idx += 1
                start_time = None
            elif ch == "c":
                start_time = last_t
            elif ch == "r":
                start_time = 0
            elif ch == "p":
                ep_idx -= 1
                start_time = None
            elif ch == "d":
                force_pick = True
                start_time = last_t if (last_t is not None and last_t > 0) else 0
            elif ch == "e":
                ep_opts, _ = _episode_options(name, item, anime, episodes)
                new = await pick("Select episode", ep_opts, ep_idx)
                if new is None:
                    continue
                ep_idx = new
                start_time = None
            break


async def _watch(extractor, name: str, item, opts: str) -> None:
    CONSOLE.print("loading...")
    try:
        anime = await item.a_get_anime()
        episodes = list(await anime.a_get_episodes())
    except Exception as ex:  # noqa: BLE001
        CONSOLE.print(f"[red]error: {ex}[/red]")
        return
    if not episodes:
        CONSOLE.print("[red]no episodes[/red]")
        return
    ep_opts, default_idx = _episode_options(name, item, anime, episodes)
    ep_idx = await pick(anime.title + ": select episode", ep_opts, default_idx)
    if ep_idx is None:
        return
    await _play_loop(extractor, name, item, anime, episodes, ep_idx, opts, back_label="Back to results")


async def _direct_source_fallback(entry: dict, index_in_history: int, opts: str) -> None:
    from anicli_api.tools.helpers import get_video_by_quality

    ep_name = history.clean_episode_name(entry)
    title_anime = (entry.get("data") or {}).get("title") or "Unknown"
    saved_t = entry.get("time")
    saved_dur = entry.get("duration")

    start_time: Optional[int] = None
    if saved_t is not None and saved_t > 0:
        t_str = _fmt_progress(saved_t, saved_dur)
        act = await pick(
            f"{title_anime} — {ep_name}",
            [
                ("resume", f"Continue {ep_name} from {t_str}"),
                ("restart", f"Watch {ep_name} from beginning (00:00)"),
                ("back", "Back"),
            ],
            0,
            "arrows: move | Enter: select | Esc: back",
        )
        if act in (None, "back"):
            return
        start_time = saved_t if act == "resume" else 0
    else:
        start_time = 0

    src = history.load_source(entry, ep_name)
    videos = await src.a_get_videos()
    if not videos:
        CONSOLE.print("[red]video not found[/red]")
        return
    video = get_video_by_quality(list(videos), DEFAULT_QUALITY)
    history.bump_to_top(index_in_history)
    if start_time == 0:
        history.update_progress(0, duration=saved_dur)

    title = f"{title_anime} - {ep_name}"
    ext_name = entry.get("extractor_name") or ""
    data_obj = entry.get("data") or {}
    raw_url = data_obj.get("url") or ""
    if raw_url.startswith(("http://", "https://")):
        page_url: Optional[str] = raw_url
    elif ext_name == "animeon" and raw_url and raw_url != "_":
        page_url = f"https://animeon.cc/anime/{raw_url}"
    else:
        page_url = None
    rpc_meta: Dict[str, Any] = {
        "anime_title": title_anime,
        "episode": ep_name,
        "dub": getattr(src, "title", None),
        "source_name": ext_name or None,
        "thumbnail": data_obj.get("thumbnail") or None,
        "quality": getattr(video, "quality", None),
        "page_url": page_url,
    }
    while True:
        eff_start = start_time if (start_time is not None and start_time > 0) else None
        ctrl = MPVController(
            [video],
            [title],
            mpv_opts=opts,
            save_time=True,
            start_time=eff_start,
            rpc_meta=rpc_meta,
        )
        await ctrl.play()
        await get_rpc().a_set_browsing(ext_name or None)
        last_t = ctrl.last_time
        last_dur = ctrl.last_duration
        menu: List[Tuple[str, str]] = []
        if last_t is not None and last_t > 0:
            menu.append(("c", f"Continue from {history.format_time(last_t)}"))
        menu += [("r", "Replay from beginning (00:00)"), ("m", "Back to history")]
        header = f"{title}  [{_fmt_progress(last_t, last_dur)}]" if (last_t and last_t > 0) else title
        ch = await pick(header, menu, 0, "arrows: move | Enter: select | Esc: back to history")
        if ch in (None, "m"):
            return
        if ch == "c":
            start_time = last_t
        elif ch == "r":
            start_time = 0
            history.update_progress(0, duration=last_dur)


async def _history_flow(opts: str) -> None:
    while True:
        entries = history.read()
        if not entries:
            CONSOLE.print("[yellow]History is empty[/yellow]")
            return

        items_opts: List[Tuple[int, str]] = []
        for idx, entry in enumerate(entries):
            title = (entry.get("data") or {}).get("title") or "Unknown"
            ep_name = history.clean_episode_name(entry)
            t = entry.get("time")
            dur = entry.get("duration")
            sources = entry.get("source") or []
            dub = sources[0].get("title") if sources and isinstance(sources, list) else None
            ext_name = entry.get("extractor_name") or ""
            label = f"{title} — {ep_name}"
            if t is not None and t > 0:
                label += f"  [{_fmt_progress(t, dur)}]"
            meta = " | ".join(p for p in (dub, ext_name) if p)
            if meta:
                label += f"  ({meta})"
            items_opts.append((idx, label))

        sel = await pick("Continue watching (history)", items_opts, 0, "arrows: move | Enter: select | Esc: back | type: filter")
        if sel is None:
            return

        entry = entries[sel]
        ext_name = entry.get("extractor_name") or ""
        try:
            ext_inst, item = history.load_item_with_extractor(entry)
        except Exception as ex:  # noqa: BLE001
            CONSOLE.print(f"[red]history load error: {ex}[/red]")
            continue

        try:
            CONSOLE.print("loading...")
            try:
                anime = await item.a_get_anime()
                episodes = list(await anime.a_get_episodes())
            except Exception as ex:  # noqa: BLE001
                CONSOLE.print(f"[yellow]anime fetch failed ({ex}), trying saved link...[/yellow]")
                if entry.get("source"):
                    try:
                        await _direct_source_fallback(entry, sel, opts)
                    except Exception as ex2:  # noqa: BLE001
                        CONSOLE.print(f"[red]fallback error: {ex2}[/red]")
                continue

            if not episodes:
                CONSOLE.print("[red]no episodes[/red]")
                continue

            ep_idx = _find_episode_index(episodes, entry)
            ep_cur = episodes[ep_idx]
            ep_lbl = _ep_label(ep_cur)

            prog = history.get_episode_progress_from_entry(entry, ep_lbl, ep_cur.ordinal)
            saved_t = prog.get("time") if prog else entry.get("time")
            saved_dur = prog.get("duration") if prog else entry.get("duration")
            sources_list = entry.get("source") or []
            saved_src_title = (
                (prog.get("source_title") if prog else None)
                or (sources_list[0].get("title") if sources_list and isinstance(sources_list, list) else None)
            )

            while True:
                action_opts: List[Tuple[str, str]] = []
                if saved_t is not None and saved_t > 0:
                    action_opts.append(("resume", f"Continue {ep_lbl} from {_fmt_progress(saved_t, saved_dur)}"))
                if ep_idx + 1 < len(episodes):
                    action_opts.append(("next", f"Next episode ({_ep_label(episodes[ep_idx + 1])})"))
                action_opts.append(("restart", f"Watch {ep_lbl} from beginning (00:00)"))
                action_opts.append(("choose", "Choose episode"))
                action_opts.append(("back", "Back"))

                header = f"{anime.title} — {ep_lbl}"
                if saved_src_title:
                    header += f"  ({saved_src_title})"
                act = await pick(header, action_opts, 0, "arrows: move | Enter: select | Esc: back")
                if act in (None, "back"):
                    break
                if act == "resume":
                    await _play_loop(
                        ext_inst,
                        ext_name,
                        item,
                        anime,
                        episodes,
                        ep_idx,
                        opts,
                        start_time=saved_t,
                        preferred_source_title=saved_src_title,
                        auto_pick_source=bool(saved_src_title),
                        back_label="Back to history",
                    )
                    break
                if act == "next":
                    await _play_loop(
                        ext_inst,
                        ext_name,
                        item,
                        anime,
                        episodes,
                        ep_idx + 1,
                        opts,
                        start_time=0,
                        preferred_source_title=saved_src_title,
                        auto_pick_source=bool(saved_src_title),
                        back_label="Back to history",
                    )
                    break
                if act == "restart":
                    await _play_loop(
                        ext_inst,
                        ext_name,
                        item,
                        anime,
                        episodes,
                        ep_idx,
                        opts,
                        start_time=0,
                        preferred_source_title=saved_src_title,
                        auto_pick_source=bool(saved_src_title),
                        back_label="Back to history",
                    )
                    break
                if act == "choose":
                    ep_opts, _ = _episode_options(ext_name, item, anime, episodes)
                    chosen_ep = await pick(anime.title + ": select episode", ep_opts, ep_idx)
                    if chosen_ep is None:
                        continue
                    await _play_loop(
                        ext_inst,
                        ext_name,
                        item,
                        anime,
                        episodes,
                        chosen_ep,
                        opts,
                        start_time=None,
                        preferred_source_title=saved_src_title,
                        auto_pick_source=False,
                        back_label="Back to history",
                    )
                    break
        finally:
            await _close(ext_inst)


async def _search_flow(extractor, name: str, opts: str, query: Optional[str] = None, ongoing: bool = False) -> None:
    while True:
        if ongoing:
            try:
                results = list(await extractor.a_ongoing())
            except Exception as ex:  # noqa: BLE001
                CONSOLE.print(f"[red]error: {ex}[/red]")
                return
        else:
            if query is None:
                query = await ask_text(f"[{name}] anime title (empty = back): ")
                if query is None:
                    return
            try:
                results = list(await extractor.a_search(query))
            except Exception as ex:  # noqa: BLE001
                CONSOLE.print(f"[red]search error: {ex}[/red]")
                results = []
        if not results:
            CONSOLE.print("[yellow]nothing found[/yellow]")
            if ongoing:
                return
            query = None
            continue
        idx = await pick(
            "Ongoing" if ongoing else f"Results for '{query}'",
            [(i, str(r.title)) for i, r in enumerate(results)],
        )
        if idx is None:
            if ongoing:
                return
            query = None  # back to the title prompt
            continue
        await _watch(extractor, name, results[idx], opts)
        # after watching go back to the results list (same query)


async def _run_classic(name: str, cmd: Optional[str]) -> None:
    from anicli.cli.main import APP

    APP.context._data.update(
        {"extractor_name": name, "quality": DEFAULT_QUALITY, "mpv_opts": "", "m3u_size": 6, "timeout": 60, "headers": {}}
    )
    await APP.run(cmd_key=cmd, raw_args="" if cmd else None)


async def wizard(source: Optional[str] = None) -> None:
    if not is_mpv_installed():
        CONSOLE.print("[red]mpv not found in PATH[/red]")
        return

    opts = ""
    extractor = None
    try:
        name = source
        if name is None:
            await get_rpc().a_set_browsing(None)
            while True:
                picked = await _choose_source_or_continue(None, allow_continue=True)
                if picked is None:
                    return
                if picked == "__continue__":
                    await _history_flow(opts)
                    await get_rpc().a_set_browsing(None)
                    continue
                name = picked
                break

        extractor = _make_extractor(name)
        await get_rpc().a_set_browsing(name)
        while True:
            rpc_label = f"Discord RPC: {'ON' if get_rpc().is_enabled() else 'OFF'}"
            choice = await pick(
                f"anicli-ru   source: {name}",
                [
                    ("s", "Search anime"),
                    ("o", "Ongoing"),
                    ("h", "Continue watching (history)"),
                    ("c", "Change source"),
                    ("k", "Classic command console"),
                    ("rpc", rpc_label),
                    ("x", "Exit"),
                ],
                0,
                "arrows: move | Enter: select | Esc: exit",
            )
            if choice in (None, "x"):
                return
            if choice == "s":
                await _search_flow(extractor, name, opts)
                await get_rpc().a_set_browsing(name)
            elif choice == "o":
                await _search_flow(extractor, name, opts, ongoing=True)
                await get_rpc().a_set_browsing(name)
            elif choice == "h":
                await _history_flow(opts)
                await get_rpc().a_set_browsing(name)
            elif choice == "k":
                await _run_classic(name, None)
                await get_rpc().a_set_browsing(name)
            elif choice == "c":
                new = await _choose_source_or_continue(name, allow_continue=False)
                if new and new != name:
                    await _close(extractor)
                    name = new
                    extractor = _make_extractor(name)
                    await get_rpc().a_set_browsing(name)
            elif choice == "rpc":
                enabled = get_rpc().toggle()
                if enabled:
                    await get_rpc().a_set_browsing(name)
                else:
                    await get_rpc().a_clear()
    finally:
        if extractor is not None:
            await _close(extractor)
        await get_rpc().a_close()


def run_wizard(source: Optional[str] = None) -> None:
    asyncio.run(wizard(source))
