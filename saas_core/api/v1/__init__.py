"""saas_core API v1. Top-level ops (no module segment) are lazily re-exported from auth."""
import importlib

_TOP = {"signup", "config","login", "logout", "me"}
_SUBMODULES = {"checkout_verify"}


def __getattr__(name):
    if name in _TOP:
        return getattr(importlib.import_module("saas_core.api.v1.auth"), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"saas_core.api.v1.{name}")
    raise AttributeError(name)
