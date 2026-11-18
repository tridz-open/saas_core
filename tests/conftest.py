import sys, pathlib, pytest
sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import frappe_stub


@pytest.fixture(autouse=True)
def frappe(request):
    """Fresh in-memory fake frappe per test; evicts saas_core modules so they re-bind to it."""
    for m in [m for m in sys.modules if m == "saas_core" or m.startswith("saas_core.")]:
        if m != "saas_core": del sys.modules[m]
    pkg = sys.modules.get("saas_core")
    for a in [a for a, v in vars(pkg).items() if isinstance(v, type(sys)) and a not in ("config",)] if pkg else []:
        delattr(pkg, a)  # `from saas_core import jobs` must not return a module bound to the previous frappe
    f = frappe_stub.build()
    if not request.node.get_closest_marker("strict_experimental"):
        f.conf["saas_core_experimental"] = "all,fake_gateway"  # `all` excludes fake_gateway; existing suites exercise experimental code; gate tests opt out
    return f
