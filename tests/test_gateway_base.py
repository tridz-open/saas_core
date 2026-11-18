import pytest

from saas_core.gateways.base import CheckoutResult, Gateway, NormalizedEvent


class _Min(Gateway):
    name = "min"

    def create_checkout_session(self, params):
        return CheckoutResult(url="u", session_ref="s")

    def portal_link(self, customer_ref, return_url):
        return return_url

    def cancel_subscription(self, sub_ref, at_period_end):
        return None

    def verify_webhook(self, headers, raw_body):
        return True

    def parse_event(self, payload, headers=None):
        return NormalizedEvent(event_id="e", gateway="min", kind="ignored")


def test_gateway_base_supports_default_empty():
    assert _Min().supports == set()
    assert isinstance(_Min.supports, (set, frozenset))


def test_gateway_base_client_payload_default():
    assert _Min().client_payload(object()) == {"provider": "min"}


def test_gateway_base_verify_client_callback_not_implemented():
    with pytest.raises(NotImplementedError):
        _Min().verify_client_callback({"a": 1})


def test_gateway_base_override_supports():
    class G(_Min):
        supports = frozenset({"order"})

    assert "order" in G().supports
    assert "order" not in _Min().supports
