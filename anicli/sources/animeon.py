from __future__ import annotations

import re
from typing import Any

from attrs import define

from anicli_api.base import BaseAnime, BaseEpisode, BaseExtractor, BaseOngoing, BaseSearch, BaseSource

# Local custom source for https://animeon.cc (JSON API). Players: kodik (and any other supported by link).
BASE = "https://animeon.cc"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _abs(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def _items(data: dict) -> list[dict]:
    return [i for i in data.get("items", []) if not i.get("disabled")]


class Extractor(BaseExtractor):
    BASE_URL = BASE

    def _mk(self, cls, item: dict):
        return cls(
            title=item.get("title") or item.get("title_orig") or item["anime_url"],
            thumbnail=_abs(item.get("poster") or item.get("anime_poster") or ""),
            url=item["anime_url"],
            **self._kwargs_http,
        )

    def search(self, query: str) -> list["Search"]:
        r = self.http.get(f"{BASE}/api/anime", params={"search": query, "limit": 20}, headers=_HEADERS)
        r.raise_for_status()
        return [self._mk(Search, i) for i in _items(r.json())]

    async def a_search(self, query: str) -> list["Search"]:
        r = await self.http_async.get(f"{BASE}/api/anime", params={"search": query, "limit": 20}, headers=_HEADERS)
        r.raise_for_status()
        return [self._mk(Search, i) for i in _items(r.json())]

    def ongoing(self) -> list["Ongoing"]:
        r = self.http.get(f"{BASE}/api/anime", params={"status": "ongoing", "limit": 20}, headers=_HEADERS)
        r.raise_for_status()
        return [self._mk(Ongoing, i) for i in _items(r.json())]

    async def a_ongoing(self) -> list["Ongoing"]:
        r = await self.http_async.get(f"{BASE}/api/anime", params={"status": "ongoing", "limit": 20}, headers=_HEADERS)
        r.raise_for_status()
        return [self._mk(Ongoing, i) for i in _items(r.json())]


def _to_anime(self, d: dict) -> "Anime":
    return Anime(
        title=d.get("title") or self.title,
        thumbnail=self.thumbnail,
        description=d.get("description") or "",
        data=d,
        **self._kwargs_http,
    )


@define(kw_only=True)
class Search(BaseSearch):
    def get_anime(self) -> "Anime":
        r = self.http.get(f"{BASE}/api/anime/{self.url}", headers=_HEADERS)
        r.raise_for_status()
        return _to_anime(self, r.json())

    async def a_get_anime(self) -> "Anime":
        r = await self.http_async.get(f"{BASE}/api/anime/{self.url}", headers=_HEADERS)
        r.raise_for_status()
        return _to_anime(self, r.json())


@define(kw_only=True)
class Ongoing(BaseOngoing):
    def get_anime(self) -> "Anime":
        r = self.http.get(f"{BASE}/api/anime/{self.url}", headers=_HEADERS)
        r.raise_for_status()
        return _to_anime(self, r.json())

    async def a_get_anime(self) -> "Anime":
        r = await self.http_async.get(f"{BASE}/api/anime/{self.url}", headers=_HEADERS)
        r.raise_for_status()
        return _to_anime(self, r.json())


def _ep_key(s: str):
    m = re.match(r"\d+", s)
    return (int(m.group()) if m else 10**9, s)


@define(kw_only=True)
class Anime(BaseAnime):
    data: dict

    def get_episodes(self) -> list["Episode"]:
        trs: dict[str, Any] = self.data.get("translations") or {}
        nums = sorted({str(k) for t in trs.values() for k in (t.get("episodes") or {})}, key=_ep_key)
        out = []
        for i, n in enumerate(nums, 1):
            links = {
                name: _abs(t["episodes"][n]["link"])
                for name, t in trs.items()
                if n in (t.get("episodes") or {}) and t["episodes"][n].get("link")
            }
            out.append(Episode(title=f"Episode {n}", ordinal=i, links=links, **self._kwargs_http))
        return out

    async def a_get_episodes(self) -> list["Episode"]:
        return self.get_episodes()


@define(kw_only=True)
class Episode(BaseEpisode):
    links: dict

    def get_sources(self) -> list["Source"]:
        return [Source(title=name, url=url, **self._kwargs_http) for name, url in self.links.items()]

    async def a_get_sources(self) -> list["Source"]:
        return self.get_sources()


@define(kw_only=True)
class Source(BaseSource):
    pass


if __name__ == "__main__":
    from anicli_api.tools.dummy_cli import cli

    cli(Extractor())
