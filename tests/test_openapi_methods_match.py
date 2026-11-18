"""Every GET operation in the OpenAPI spec (which the SDK is generated from) must be served over GET.

Regression: tenants.slug_availability was GET in the spec but POST-only on the server, so every
onboarding address check failed with 403 in the browser while unit tests (which call the function) passed.
READ_ENDPOINTS is read from source with ast: the test fakes replace saas_core.api.v1._common at import time.
"""
import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "openapi" / "v1.yaml"
COMMON = ROOT / "saas_core" / "api" / "v1" / "_common.py"
PREFIX = "/api/method/saas_core.api.v1."


def _read_endpoints() -> set:
    for node in ast.walk(ast.parse(COMMON.read_text())):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "READ_ENDPOINTS" for t in node.targets):
            return {e.value for e in node.value.args[0].elts}
    raise AssertionError("READ_ENDPOINTS not found")


def test_spec_get_operations_are_read_endpoints():
    paths = yaml.safe_load(SPEC.read_text())["paths"]
    gets = {p[len(PREFIX):] for p, ops in paths.items() if p.startswith(PREFIX) and "get" in ops}
    assert gets, "no GET operations found in spec"
    # top-level ops (config, me, ...) are re-exported from auth in saas_core/api/v1/__init__.py
    served = {g for g in gets if g in _read_endpoints() or f"auth.{g}" in _read_endpoints()}
    missing = sorted(gets - served)
    assert not missing, f"GET in OpenAPI but POST-only on the server: {missing}"
