"""BILLING_PLAN v2 (R6) worked examples: February lengths, 31 Jan start, post-upgrade split, same-day, last day."""
import datetime as dt

from saas_core.billing import proration as p

D = dt.date
Z = dt.timezone.utc
PRICE = 300000


def at(y, m, d, h=6):  # 06:00Z = 11:30 IST, same IST day
    return dt.datetime(y, m, d, h, tzinfo=Z)


def test_february_28_day_cycle_cancel_day_1():
    q = p.cancel_quote(PRICE, D(2027, 2, 1), D(2027, 3, 1), at(2027, 2, 1))
    assert (q.cycle_days, q.days_used, q.days_unused, q.refund_minor) == (28, 1, 27, 289285)


def test_february_leap_29_day_cycle_cancel_day_1():
    q = p.cancel_quote(PRICE, D(2028, 2, 1), D(2028, 3, 1), at(2028, 2, 1))
    assert (q.cycle_days, q.days_used, q.days_unused, q.refund_minor) == (29, 1, 28, 289655)


def test_cycle_starting_31_jan_uses_stored_dates_not_month_arithmetic():
    q = p.cancel_quote(PRICE, D(2027, 1, 31), D(2027, 2, 28), at(2027, 1, 31))
    assert (q.cycle_days, q.days_used, q.days_unused, q.refund_minor) == (28, 1, 27, 289285)


def test_cancel_after_upgrade_is_priced_on_the_segment_and_split_across_two_payments():
    # upgrade on 10 Nov: segment 11 Nov to 1 Dec (20 d), held 166667; payments: base 66666 + upgrade 100001
    q = p.cancel_quote(166667, D(2026, 11, 11), D(2026, 12, 1), at(2026, 11, 15))
    assert (q.cycle_days, q.days_used, q.days_unused, q.refund_minor) == (20, 5, 15, 125000)
    from saas_core.billing import refunds

    class S(dict):
        pass
    sub = S(extra_payments='[{"payment_id": "pay_up", "amount": 100001}]')
    parts, left, unallocated = refunds.split(sub, q.refund_minor, "pay_base", 166667)
    assert parts == [("pay_up", 100001), ("pay_base", 24999)] and unallocated == 0
    assert sum(a for _, a in parts) == 125000
    assert left == []


def test_cancel_same_day_as_upgrade_clamps_days_used_to_zero():
    q = p.cancel_quote(166667, D(2026, 11, 11), D(2026, 12, 1), at(2026, 11, 10))
    assert q.days_used == 0 and q.days_unused == 20 and q.refund_minor == 166667


def test_days_used_never_negative():
    cyc = p.cycle_of(D(2026, 11, 11), D(2026, 12, 1))
    assert p.days_used(cyc, at(2026, 1, 1)) == 0


def test_plan_change_on_last_day_has_nothing_to_reprice():
    for new_price in (500000, 100000):
        q = p.change_quote(PRICE, new_price, PRICE, D(2026, 11, 1), D(2026, 12, 1), at(2026, 11, 30))
        assert q.days_remaining == 0 and q.amount_due_minor == 0 and q.credit_minor == 0
        assert q.effective_date == D(2026, 12, 1)


def test_ist_midnight_decides_the_day_for_cancel():
    # 23:59:59 IST on 1 Nov (18:29:59Z) is day 1; 00:00:00 IST on 2 Nov (18:30:00Z) is day 2
    a = p.cancel_quote(PRICE, D(2026, 11, 1), D(2026, 12, 1), dt.datetime(2026, 11, 1, 18, 29, 59, tzinfo=Z))
    b = p.cancel_quote(PRICE, D(2026, 11, 1), D(2026, 12, 1), dt.datetime(2026, 11, 1, 18, 30, 0, tzinfo=Z))
    assert (a.days_used, b.days_used) == (1, 2)
