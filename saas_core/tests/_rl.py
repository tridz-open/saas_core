"""Bench-test helper: clear saas_core rate-limit counters (raw, site-prefixed redis keys)."""
import frappe


def clear_rate_limits():
    cache = frappe.cache()
    pattern = f"{frappe.local.site}:saas_core:rl:*"
    for k in cache.scan_iter(match=pattern):
        cache.delete(k)
