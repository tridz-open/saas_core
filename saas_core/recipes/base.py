"""Base Recipe: subclass and override what you need; defaults are no-ops."""
from __future__ import annotations

from typing import Callable


class Recipe:
    slug: str = ""

    def template_defaults(self) -> dict:
        return {}

    def hooks(self) -> dict[str, Callable]:
        """Map 'name@version' -> callable."""
        return {}

    def tenant_config(self, tenant, plan, fields: dict) -> dict:
        """Site config dict applied to the new tenant site."""
        return {}

    def signup_schema(self) -> dict:
        return {}
