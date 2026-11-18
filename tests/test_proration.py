"""Unit tests for the pure day-based proration engine (no frappe needed)."""
import datetime as dt

import pytest

from saas_core.billing import proration as p

D = dt.date
Z = dt.timezone.utc


def z(y, m, d, h=0, mi=0, s=0):
    return dt.datetime(y, m, d, h, mi, s, tzinfo=Z)


NOV18, DEC18 = D(2026, 11, 18), D(2026, 12, 18)   # 30-day cycle
CYC30 = p.cycle_of(NOV18, DEC18)


# ---------------------------------------------------------------- timezone helpers
def test_billing_date_ist_boundary():
    assert p.billing_date(z(2026, 11, 18, 18, 29, 59)) == D(2026, 11, 18)
    assert p.billing_date(z(2026, 11, 18, 18, 30, 0)) == D(2026, 11, 19)


def test_billing_date_utc_morning_is_same_ist_day():
    assert p.billing_date(z(2026, 11, 18, 0, 0, 0)) == D(2026, 11, 18)


def test_billing_date_naive_is_utc():
    assert p.billing_date(dt.datetime(2026, 11, 18, 18, 29, 59)) == D(2026, 11, 18)
    assert p.billing_date(dt.datetime(2026, 11, 18, 18, 30, 0)) == D(2026, 11, 19)


def test_billing_date_other_offset_converted():
    plus9 = dt.timezone(dt.timedelta(hours=9))
    # 2026-11-19 02:00 +09:00 == 2026-11-18 17:00Z == 22:30 IST on the 18th
    assert p.billing_date(dt.datetime(2026, 11, 19, 2, 0, tzinfo=plus9)) == D(2026, 11, 18)


def test_billing_date_plain_date_passthrough():
    assert p.billing_date(D(2026, 11, 18)) == D(2026, 11, 18)


def test_day_start_is_previous_day_1830_utc():
    assert p.day_start(D(2026, 11, 19)) == z(2026, 11, 18, 18, 30)
    assert p.day_start(D(2026, 11, 19)).tzinfo is not None


def test_end_of_billing_day():
    assert p.end_of_billing_day(z(2026, 11, 18, 18, 29, 59)) == z(2026, 11, 18, 18, 30)
    assert p.end_of_billing_day(z(2026, 11, 18, 18, 30)) == z(2026, 11, 19, 18, 30)
    assert p.end_of_billing_day(D(2026, 11, 18)) == z(2026, 11, 18, 18, 30)


def test_end_of_billing_day_month_and_year_rollover():
    assert p.end_of_billing_day(D(2026, 12, 31)) == z(2026, 12, 31, 18, 30)
    assert p.end_of_billing_day(D(2028, 2, 28)) == z(2028, 2, 28, 18, 30)
    assert p.end_of_billing_day(D(2028, 2, 29)) == z(2028, 2, 29, 18, 30)


def test_to_naive_utc_round_trip():
    n = p.to_naive_utc(z(2026, 11, 18, 18, 30))
    assert n == dt.datetime(2026, 11, 18, 18, 30) and n.tzinfo is None
    assert p.to_aware(n) == z(2026, 11, 18, 18, 30)
    # naive in -> same naive out
    assert p.to_naive_utc(n) == n
    # IST-aware converted
    ist_dt = dt.datetime(2026, 11, 19, 0, 0, tzinfo=p.IST)
    assert p.to_naive_utc(ist_dt) == dt.datetime(2026, 11, 18, 18, 30)
    # day_start -> naive -> aware -> billing_date
    assert p.billing_date(p.to_aware(p.to_naive_utc(p.day_start(D(2026, 11, 19))))) == D(2026, 11, 19)


def test_utc_now_is_aware_utc():
    assert p.utc_now().utcoffset() == dt.timedelta(0)


# ---------------------------------------------------------------- cycle_of
@pytest.mark.parametrize("s,e,n", [
    (D(2026, 11, 18), D(2026, 12, 18), 30),
    (D(2026, 12, 18), D(2027, 1, 18), 31),
    (D(2026, 2, 1), D(2026, 3, 1), 28),
    (D(2028, 2, 1), D(2028, 3, 1), 29),
    (D(2026, 3, 1), D(2027, 3, 1), 365),
    (D(2027, 3, 1), D(2028, 3, 1), 366),
])
def test_cycle_lengths(s, e, n):
    c = p.cycle_of(s, e)
    assert (c.start, c.end, c.days) == (s, e, n)
    assert c.last_day == e - dt.timedelta(days=1)


def test_cycle_of_datetimes_use_ist_dates():
    # 18:30Z on 17 Nov is 00:00 IST on 18 Nov
    c = p.cycle_of(z(2026, 11, 17, 18, 30), z(2026, 12, 17, 18, 30))
    assert (c.start, c.end, c.days) == (NOV18, DEC18, 30)


def test_cycle_of_short_span_raises():
    with pytest.raises(ValueError):
        p.cycle_of(NOV18, NOV18)
    with pytest.raises(ValueError):
        p.cycle_of(NOV18, D(2026, 11, 17))
    # same IST day although different instants
    with pytest.raises(ValueError):
        p.cycle_of(z(2026, 11, 18, 0), z(2026, 11, 18, 12))


def test_cycle_of_exactly_one_day_ok():
    assert p.cycle_of(NOV18, D(2026, 11, 19)).days == 1


# ---------------------------------------------------------------- days_used
def test_days_used_edges():
    assert p.days_used(CYC30, D(2026, 11, 18)) == 1
    assert p.days_used(CYC30, D(2026, 11, 19)) == 2
    assert p.days_used(CYC30, D(2026, 12, 17)) == 30      # last day
    assert p.days_used(CYC30, D(2026, 12, 18)) == 30      # renewal day clamps
    assert p.days_used(CYC30, D(2027, 6, 1)) == 30
    assert p.days_used(CYC30, D(2026, 11, 17)) == 0
    assert p.days_used(CYC30, D(2020, 1, 1)) == 0


def test_days_used_ist_boundary():
    assert p.days_used(CYC30, z(2026, 11, 18, 18, 29)) == 1
    assert p.days_used(CYC30, z(2026, 11, 18, 18, 30)) == 2
    # 17 Nov 18:30Z is the first instant of 18 Nov IST
    assert p.days_used(CYC30, z(2026, 11, 17, 18, 29)) == 0
    assert p.days_used(CYC30, z(2026, 11, 17, 18, 30)) == 1


# ---------------------------------------------------------------- cancel_quote
def test_cancel_same_day():
    q = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 10))
    assert (q.days_used, q.days_unused, q.cycle_days) == (1, 29, 30)
    assert (q.paid_minor, q.refund_minor, q.charge_minor) == (30000, 29000, 1000)
    assert q.cycle_start == NOV18 and q.cycle_end == DEC18


def test_cancel_last_day_refund_zero():
    q = p.cancel_quote(30000, NOV18, DEC18, z(2026, 12, 17, 10))
    assert (q.days_used, q.days_unused) == (30, 0)
    assert (q.refund_minor, q.charge_minor) == (0, 30000)


def test_cancel_mid_cycle():
    q = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 27, 10))   # day 10
    assert (q.days_used, q.days_unused) == (10, 20)
    assert (q.refund_minor, q.charge_minor) == (20000, 10000)


def test_cancel_paid_zero_trial():
    q = p.cancel_quote(0, NOV18, DEC18, z(2026, 11, 20))
    assert (q.paid_minor, q.refund_minor, q.charge_minor) == (0, 0, 0)
    assert q.days_used == 3


def test_cancel_paid_none_or_negative_treated_as_zero():
    assert p.cancel_quote(None, NOV18, DEC18, NOV18).paid_minor == 0
    q = p.cancel_quote(-500, NOV18, DEC18, NOV18)
    assert (q.paid_minor, q.refund_minor, q.charge_minor) == (0, 0, 0)


def test_cancel_rounding_refund_floors():
    # 100 paise over 30 days: day 1 -> 100*29/30 = 96.67 -> 96, charge 4
    q = p.cancel_quote(100, NOV18, DEC18, NOV18)
    assert (q.refund_minor, q.charge_minor) == (96, 4)
    # day 2 -> 100*28/30 = 93.33 -> 93, charge 7
    q = p.cancel_quote(100, NOV18, DEC18, D(2026, 11, 19))
    assert (q.refund_minor, q.charge_minor) == (93, 7)
    # 1 paise: refund always 0 until... 1*29/30 -> 0
    q = p.cancel_quote(1, NOV18, DEC18, NOV18)
    assert (q.refund_minor, q.charge_minor) == (0, 1)


def test_cancel_invariants_over_all_days():
    for paid in (0, 1, 7, 99, 100, 29999, 30000, 123457):
        for off in range(-3, 35):
            now = NOV18 + dt.timedelta(days=off)
            q = p.cancel_quote(paid, NOV18, DEC18, now)
            assert 0 <= q.refund_minor <= q.paid_minor == paid
            assert q.charge_minor == q.paid_minor - q.refund_minor >= 0
            assert q.days_used + q.days_unused == q.cycle_days


def test_cancel_before_cycle_start_full_refund():
    q = p.cancel_quote(30000, NOV18, DEC18, D(2026, 11, 10))
    assert (q.days_used, q.days_unused, q.refund_minor) == (0, 30, 30000)


def test_cancel_after_cycle_end_no_refund():
    q = p.cancel_quote(30000, NOV18, DEC18, D(2027, 1, 5))
    assert (q.days_used, q.days_unused, q.refund_minor) == (30, 0, 0)


def test_cancel_ist_midnight_edge():
    a = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 18, 29))
    b = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 18, 30))
    assert (a.days_used, a.refund_minor) == (1, 29000)
    assert (b.days_used, b.refund_minor) == (2, 28000)


def test_cancel_stops_at_end_of_ist_day():
    a = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 18, 29))
    b = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 18, 30))
    assert a.stops_at == z(2026, 11, 18, 18, 30)
    assert b.stops_at == z(2026, 11, 19, 18, 30)
    assert p.cancel_quote(1, NOV18, DEC18, z(2026, 11, 18, 3)).stops_at == z(2026, 11, 18, 18, 30)


def test_cancel_naive_now_is_utc():
    q = p.cancel_quote(30000, NOV18, DEC18, dt.datetime(2026, 11, 18, 18, 30))
    assert q.days_used == 2


def test_cancel_datetime_period_bounds():
    q = p.cancel_quote(30000, dt.datetime(2026, 11, 17, 18, 30), dt.datetime(2026, 12, 17, 18, 30),
                       z(2026, 11, 27, 10))
    assert (q.cycle_start, q.cycle_end, q.cycle_days, q.days_used) == (NOV18, DEC18, 30, 10)


def test_cancel_leap_and_year_cycles():
    # Feb 2028 = 29 days; 2900 paid; day 1 -> refund 2900*28/29 = 2800
    q = p.cancel_quote(2900, D(2028, 2, 1), D(2028, 3, 1), D(2028, 2, 1))
    assert (q.cycle_days, q.refund_minor) == (29, 2800)
    # 366-day year, 36600 paid, day 1 -> 36600*365/366 = 36500
    q = p.cancel_quote(36600, D(2027, 3, 1), D(2028, 3, 1), D(2027, 3, 1))
    assert (q.cycle_days, q.refund_minor) == (366, 36500)


def test_cancel_short_cycle_raises():
    with pytest.raises(ValueError):
        p.cancel_quote(100, NOV18, NOV18, NOV18)


def test_cancel_as_dict_iso():
    d = p.cancel_quote(30000, NOV18, DEC18, z(2026, 11, 18, 10)).as_dict()
    assert d["cycle_start"] == "2026-11-18" and d["cycle_end"] == "2026-12-18"
    assert d["stops_at"] == "2026-11-18T18:30:00Z"
    assert d["timezone"] == "Asia/Kolkata"
    assert d["refund_minor"] == 29000 and d["charge_minor"] == 1000
    assert d["cycle_days"] == 30 and d["days_used"] == 1 and d["days_unused"] == 29


def test_cancel_quote_is_frozen():
    q = p.cancel_quote(100, NOV18, DEC18, NOV18)
    with pytest.raises(Exception):
        q.refund_minor = 5


# ---------------------------------------------------------------- change_quote
def chg(held, new, old, now, s=NOV18, e=DEC18):
    return p.change_quote(held, new, old, s, e, now)


def test_change_upgrade_day1():
    q = chg(30000, 60000, 30000, z(2026, 11, 18, 10))
    assert q.direction == "upgrade"
    assert q.effective_date == D(2026, 11, 19)
    assert q.effective_at == z(2026, 11, 18, 18, 30)
    assert q.days_remaining == 29
    assert (q.old_share_minor, q.new_share_minor) == (29000, 58000)
    assert (q.amount_due_minor, q.credit_minor) == (29000, 0)
    assert q.new_held_minor == 58000
    assert (q.cycle_start, q.cycle_end, q.cycle_days) == (NOV18, DEC18, 30)


def test_change_downgrade_day1():
    q = chg(30000, 15000, 30000, z(2026, 11, 18, 10))
    assert q.direction == "downgrade"
    assert (q.old_share_minor, q.new_share_minor) == (29000, 14500)
    assert (q.amount_due_minor, q.credit_minor) == (0, 14500)
    assert q.new_held_minor == 14500


def test_change_same_price():
    q = chg(30000, 30000, 30000, z(2026, 11, 18, 10))
    assert q.direction == "same"
    assert (q.amount_due_minor, q.credit_minor) == (0, 0)
    assert q.new_held_minor == q.old_share_minor == 29000
    assert q.days_remaining == 29


def test_change_mid_cycle_upgrade():
    q = chg(30000, 45000, 30000, z(2026, 11, 27, 5))   # day 10 -> eff Nov 28, rem 20
    assert q.effective_date == D(2026, 11, 28) and q.days_remaining == 20
    assert (q.old_share_minor, q.new_share_minor, q.amount_due_minor) == (20000, 30000, 10000)


def test_change_last_day_nothing_moves():
    q = chg(30000, 60000, 30000, z(2026, 12, 17, 10))
    assert q.days_remaining == 0
    assert q.effective_date == DEC18
    assert q.effective_at == z(2026, 12, 17, 18, 30)
    assert (q.old_share_minor, q.new_share_minor) == (0, 0)
    assert (q.amount_due_minor, q.credit_minor) == (0, 0)
    assert q.new_held_minor == 0
    assert q.direction == "upgrade"


def test_change_last_day_downgrade_no_credit():
    q = chg(30000, 15000, 30000, z(2026, 12, 17, 10))
    assert (q.direction, q.days_remaining, q.credit_minor, q.amount_due_minor) == ("downgrade", 0, 0, 0)


def test_change_after_cycle_end_clamps_to_renewal():
    q = chg(30000, 60000, 30000, D(2027, 1, 10))
    assert q.effective_date == DEC18 and q.days_remaining == 0
    assert (q.amount_due_minor, q.credit_minor) == (0, 0)


def test_change_before_cycle_start_effective_at_start():
    q = chg(30000, 60000, 30000, D(2026, 11, 1))
    assert q.effective_date == NOV18 and q.days_remaining == 30
    assert (q.old_share_minor, q.new_share_minor, q.amount_due_minor) == (30000, 60000, 30000)


def test_change_effective_date_is_next_ist_day():
    a = chg(30000, 60000, 30000, z(2026, 11, 18, 18, 29))
    b = chg(30000, 60000, 30000, z(2026, 11, 18, 18, 30))
    assert a.effective_date == D(2026, 11, 19) and a.days_remaining == 29
    assert b.effective_date == D(2026, 11, 20) and b.days_remaining == 28
    assert b.effective_at == z(2026, 11, 19, 18, 30)


def test_change_discounted_held_below_list():
    # held 20000 against a 30000 list price; old share comes from held
    q = chg(20000, 60000, 30000, z(2026, 11, 18, 10))
    assert q.old_share_minor == 19333          # floor(20000*29/30)
    assert q.new_share_minor == 58000
    assert q.amount_due_minor == 38667
    assert q.old_share_minor + q.amount_due_minor == q.new_share_minor


def test_change_discounted_downgrade_credit_from_held():
    q = chg(20000, 15000, 30000, z(2026, 11, 18, 10))
    assert q.old_share_minor == 19333
    assert q.new_share_minor == 14500
    assert q.credit_minor == 4833


def test_change_downgrade_where_new_share_exceeds_held_share():
    # heavy discount: held share below new plan share -> credit clamps to 0, never negative
    q = chg(5000, 15000, 30000, z(2026, 11, 18, 10))
    assert q.direction == "downgrade"
    assert q.old_share_minor == 4833 and q.new_share_minor == 14500
    assert q.credit_minor == 0 and q.amount_due_minor == 0


def test_change_upgrade_where_held_share_exceeds_new_share():
    q = chg(60000, 40000, 30000, z(2026, 11, 18, 10))   # overpaid vs new list: no negative due
    assert q.direction == "upgrade"
    assert q.amount_due_minor == 0 and q.credit_minor == 0


def test_change_rounding_directions():
    # held 100 / old 100, day 1, rem 29, cycle 30
    up = chg(100, 101, 100, NOV18)
    assert up.old_share_minor == 96                 # floor(96.67)
    assert up.new_share_minor == 98                 # ceil(97.63)
    assert up.amount_due_minor == 2 and up.credit_minor == 0
    down = chg(100, 50, 100, NOV18)
    assert down.new_share_minor == 49               # ceil(48.33)
    assert down.credit_minor == 47 and down.amount_due_minor == 0


def test_change_never_both_due_and_credit():
    for held in (0, 1, 99, 100, 20000, 30000):
        for new in (0, 1, 50, 100, 30000, 60000):
            for old in (0, 100, 30000):
                for off in range(-2, 33):
                    q = chg(held, new, old, NOV18 + dt.timedelta(days=off))
                    assert q.amount_due_minor >= 0 and q.credit_minor >= 0
                    assert not (q.amount_due_minor > 0 and q.credit_minor > 0)
                    if q.direction == "upgrade":
                        assert q.credit_minor == 0
                    if q.direction == "downgrade":
                        assert q.amount_due_minor == 0


def test_change_new_held_consistency():
    for held in (100, 20000, 30000, 29999):
        for off in range(0, 29):
            now = NOV18 + dt.timedelta(days=off)
            up = chg(held, 90000, 30000, now)
            if up.amount_due_minor:
                assert up.old_share_minor + up.amount_due_minor == up.new_share_minor == up.new_held_minor
            dn = chg(held, 10000, 30000, now)
            if dn.credit_minor:
                assert dn.old_share_minor - dn.credit_minor == dn.new_share_minor == dn.new_held_minor
            same = chg(held, 30000, 30000, now)
            assert same.new_held_minor == same.old_share_minor


def test_change_zero_held_none():
    q = chg(None, 60000, 30000, NOV18)
    assert q.old_share_minor == 0 and q.amount_due_minor == q.new_share_minor == 58000


def test_change_leap_month():
    # Feb 2028, 29 days; day 1 -> rem 28
    q = p.change_quote(2900, 5800, 2900, D(2028, 2, 1), D(2028, 3, 1), D(2028, 2, 1))
    assert (q.cycle_days, q.days_remaining) == (29, 28)
    assert (q.old_share_minor, q.new_share_minor, q.amount_due_minor) == (2800, 5600, 2800)


def test_change_non_leap_feb():
    q = p.change_quote(2800, 5600, 2800, D(2026, 2, 1), D(2026, 3, 1), D(2026, 2, 1))
    assert (q.cycle_days, q.days_remaining) == (28, 27)
    assert (q.old_share_minor, q.new_share_minor, q.amount_due_minor) == (2700, 5400, 2700)


def test_change_yearly_leap_cycle():
    q = p.change_quote(36600, 73200, 36600, D(2027, 3, 1), D(2028, 3, 1), D(2027, 3, 1))
    assert (q.cycle_days, q.days_remaining) == (366, 365)
    assert (q.old_share_minor, q.new_share_minor, q.amount_due_minor) == (36500, 73000, 36500)


def test_change_month_end_effective_rollover():
    q = p.change_quote(31000, 62000, 31000, D(2026, 12, 18), D(2027, 1, 18), D(2026, 12, 31))
    assert q.effective_date == D(2027, 1, 1) and q.days_remaining == 17
    assert q.old_share_minor == 17000 and q.amount_due_minor == 17000


def test_change_short_cycle_raises():
    with pytest.raises(ValueError):
        p.change_quote(1, 2, 1, NOV18, NOV18, NOV18)


def test_change_as_dict_iso():
    d = chg(30000, 60000, 30000, z(2026, 11, 18, 10)).as_dict()
    assert d["cycle_start"] == "2026-11-18" and d["cycle_end"] == "2026-12-18"
    assert d["effective_date"] == "2026-11-19"
    assert d["effective_at"] == "2026-11-18T18:30:00Z"
    assert d["direction"] == "upgrade" and d["amount_due_minor"] == 29000
    assert d["timezone"] == "Asia/Kolkata"


# ---------------------------------------------------------------- prorate_down / prorate_up
def test_prorate_basic():
    assert p.prorate_down(100, 29, 30) == 96
    assert p.prorate_up(100, 29, 30) == 97
    assert p.prorate_down(300, 1, 3) == p.prorate_up(300, 1, 3) == 100


def test_prorate_zero_numerator():
    assert p.prorate_down(12345, 0, 30) == 0
    assert p.prorate_up(12345, 0, 30) == 0


def test_prorate_negative_inputs_clamped():
    assert p.prorate_down(100, -5, 30) == 0
    assert p.prorate_up(100, -5, 30) == 0
    assert p.prorate_down(-100, 5, 30) == 0
    assert p.prorate_up(-100, 5, 30) == 0


def test_prorate_full_and_over():
    assert p.prorate_down(100, 30, 30) == p.prorate_up(100, 30, 30) == 100


@pytest.mark.parametrize("fn", [p.prorate_down, p.prorate_up])
@pytest.mark.parametrize("den", [0, -1, -30])
def test_prorate_bad_denominator_raises(fn, den):
    with pytest.raises(ValueError):
        fn(100, 1, den)


def test_prorate_exactness_property():
    for amount in range(0, 400, 7):
        for den in (1, 7, 28, 29, 30, 31, 365, 366):
            for num in range(0, den + 1, max(1, den // 9)):
                lo, hi = p.prorate_down(amount, num, den), p.prorate_up(amount, num, den)
                assert lo * den <= amount * num <= hi * den
                assert 0 <= hi - lo <= 1
                if (amount * num) % den == 0:
                    assert lo == hi == amount * num // den
