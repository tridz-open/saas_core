"""Experimental-feature gate.

Features that are not live-verified are off unless explicitly enabled. Enable with site_config
`saas_core_experimental` (comma-separated names, or `all`) or the per-feature key `saas_core_experimental_<name>`.
See docs/STATUS.md for the verified / experimental / removed matrix.
"""
from __future__ import annotations

from saas_core.config import get_setting

FEATURES = {
    "stripe": "Stripe gateway (built from docs, no refunds, never run live)",
    "razorpay_subscription": "Razorpay subscription checkout mode (checkout_mode=subscription)",
    "dunning": "Failed-payment dunning stages, auto suspend/resume",
    "expiry_sweep": "Daily expired-subscription sweep",
    "invoice_renewals": "Invoice-mode renewals via payment links",
    "restore": "Backup restore, download links and bucket-upload restore",
    "trust_tier_preflight": "FC trust-tier capacity preflight",
    "local_bench": "Local bench provisioning mode (development only)",
    "sse": "Server-sent events stream",
    "api_tokens": "Personal API tokens",
    "team": "Tenant team invites and roles",
    "support": "Support tickets",
}


class ExperimentalDisabled(Exception):
    code = "experimental_disabled"

    def __init__(self, feature: str):
        super().__init__(f"'{feature}' is experimental and disabled: {FEATURES.get(feature, feature)}. "
                         f"Enable with site_config saas_core_experimental={feature}")
        self.feature = feature


def enabled(feature: str) -> bool:
    assert feature in FEATURES, feature
    v = get_setting(f"experimental_{feature}")
    if v not in (None, "", 0, False, "0"):
        return True
    raw = get_setting("experimental")
    if not raw:
        return False
    names = {x.strip().lower() for x in str(raw).split(",")}
    return "all" in names or feature in names


def require(feature: str) -> None:
    if not enabled(feature):
        raise ExperimentalDisabled(feature)
