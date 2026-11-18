"""Bench-only tests (FrappeTestCase). Files are named bench_*.py so pytest never collects them.

Runner note: from the bench directory run
    bench --site <site> run-tests --app saas_core --module saas_core.tests.frappe_tests.bench_webhooks
(repeat per bench_* module, or use --app saas_core with the module names). Requires saas_core installed on <site>.
"""
