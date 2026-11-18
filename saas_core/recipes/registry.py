"""Recipe registry: entry-point group 'saas_core.recipes' plus explicit register() (tests/apps)."""
from __future__ import annotations

from importlib.metadata import entry_points

from .base import Recipe

GROUP = "saas_core.recipes"
_registered: dict[str, Recipe] = {}
_loaded: dict[str, Recipe] | None = None


def _eps():
    eps = entry_points()
    return eps.select(group=GROUP) if hasattr(eps, "select") else eps.get(GROUP, [])


def _load() -> dict[str, Recipe]:
    global _loaded
    if _loaded is None:
        found: dict[str, Recipe] = {}
        for ep in _eps():
            try:
                obj = ep.load()
                inst = obj() if isinstance(obj, type) else obj
                found[getattr(inst, "slug", None) or ep.name] = inst
            except Exception:  # a broken plugin must not break the platform
                continue
        _loaded = found
    return _loaded


def register(recipe: Recipe) -> Recipe:
    if not getattr(recipe, "slug", ""):
        raise ValueError("recipe needs a slug")
    _registered[recipe.slug] = recipe
    return recipe


def unregister(slug: str) -> None:
    _registered.pop(slug, None)


def reload() -> None:
    global _loaded
    _loaded = None


def all() -> dict[str, Recipe]:  # noqa: A001
    return {**_load(), **_registered}


def get(slug: str | None) -> Recipe | None:
    return all().get(slug) if slug else None
