"""Regression: cache_lock must acquire and release with the SAME raw redis key
(found on a real bench: release used prefixed get_value/delete_value and never released)."""
import sys
import types

import pytest


class RawRedis:
    connection_pool = object()

    def __init__(self):
        self.d = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return None
        self.d[k] = v.encode()
        return True

    def get(self, k):
        return self.d.get(k)

    def delete(self, k):
        self.d.pop(k, None)

    # prefixed API (what Frappe's wrapper adds) must NOT be used for locks
    def get_value(self, k):
        raise AssertionError("prefixed get_value used for lock release")

    def delete_value(self, k):
        raise AssertionError("prefixed delete_value used for lock release")


def test_lock_released_and_reacquirable(monkeypatch):
    from saas_core import audit
    r = RawRedis()
    monkeypatch.setattr(audit.frappe, "cache", lambda: r, raising=False)
    with audit.cache_lock("saas_core:job:X", ttl=60, wait=0):
        assert ":saas_core:job:X" in "".join(r.d)
        with pytest.raises(audit.LockTimeout):
            with audit.cache_lock("saas_core:job:X", ttl=60, wait=0):
                pass
    assert not r.d
    with audit.cache_lock("saas_core:job:X", ttl=60, wait=0):
        pass


def test_lock_key_prefixed_with_site(monkeypatch):
    from saas_core import audit
    r = RawRedis()
    monkeypatch.setattr(audit.frappe, "cache", lambda: r, raising=False)
    audit.frappe.local.site = "a.example.com"
    with audit.cache_lock("saas_core:job:X", ttl=60, wait=0):
        assert list(r.d) == ["a.example.com:saas_core:job:X"]
