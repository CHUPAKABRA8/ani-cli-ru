import importlib
import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, List, Protocol, Type, cast

if TYPE_CHECKING:
    from anicli_api.base import (
        BaseAnime,
        BaseEpisode,
        BaseExtractor,
        BaseOngoing,
        BaseSearch,
        BaseSource,
    )

BUILTIN_SOURCES_PKG = "anicli.sources"


class ExtractorLikeModule(Protocol):
    Extractor: Type["BaseExtractor"]
    Search: Type["BaseSearch"]
    Ongoing: Type["BaseOngoing"]
    Anime: Type["BaseAnime"]
    Episode: Type["BaseEpisode"]
    Source: Type["BaseSource"]


def _scan_package_modules(package_name: str) -> List[str]:
    spec = importlib.util.find_spec(package_name)
    if not spec or not spec.submodule_search_locations:
        return []
    package_path = Path(spec.submodule_search_locations[0])
    return [
        p.stem.replace("_", "-")
        for p in package_path.glob("*.py")
        if not p.name.startswith("__")
    ]


@lru_cache(maxsize=1)
def _get_builtin_sources() -> List[str]:
    return _scan_package_modules(BUILTIN_SOURCES_PKG)


# guaranteed not will be updated in runtime, cache it for slightly increase speed
@lru_cache(maxsize=1)
def get_extractor_modules(package_name: str = "anicli_api.source") -> List[str]:
    # dynamically get available source extractors from anicli-api + bundled anicli.sources
    api_modules = _scan_package_modules(package_name)
    builtin_modules = _get_builtin_sources() if package_name == "anicli_api.source" else []
    return sorted(set(api_modules) | set(builtin_modules))


def dynamic_load_extractor_module(source_name: str) -> ExtractorLikeModule:
    if source_name not in get_extractor_modules():
        msg = f"Extractor module {source_name} not exists in anicli-api or anicli.sources"
        raise NameError(msg)
    mod_name = source_name.replace("-", "_")
    if source_name in _get_builtin_sources():
        module = importlib.import_module(f"{BUILTIN_SOURCES_PKG}.{mod_name}")
    else:
        module = importlib.import_module(f"anicli_api.source.{mod_name}")
    module = cast(ExtractorLikeModule, module)
    return module
