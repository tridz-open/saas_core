"""Day-based billing arithmetic. Pure functions: no frappe, no clock, no I/O.

Policy (owner-approved)
-----------------------
* Plans are priced per cycle (month or year) but usage is accounted per DAY. Any day that has started is
  charged to the end of the day it is used on.
* Billing days are IST (Asia/Kolkata, UTC+05:30, no DST) calendar days. A day runs 00:00 -> 24:00 IST.
* A cycle is the span between two renewals. Its length is the real number of days between the renewal dates
  (28..31 for a month, 365/366 for a year), so leap years and month lengths need no special casing.
* Cancel: the site stays active to the end of the current IST day. Days used = days from the cycle start to
  today inclusive (today counts in full). Unused prepaid days are refunded pro rata.
* Plan change (upgrade or downgrade): the new plan applies from the START OF THE NEXT IST DAY; today stays on
  the current plan. The remaining days of the cycle are re-priced: an upgrade owes the difference, a downgrade
  is credited (refunded) the difference. This is the simpler of the two allowed options because today is never
  billed at two rates and no "higher of the two" lookup is needed.

Timezones: aware datetimes are converted to IST. NAIVE datetimes are interpreted as UTC (that is how the
gateway layer stores them, see gateways.base.from_ts). Callers pass `utc_now()`, never Frappe's
`now_datetime()` (which is the site timezone).

Money: integer minor units (paise/cents). Rounding is always integer arithmetic:
* amounts we give back (refund, credit) round DOWN, amounts we charge round UP, so a customer is never
  over-refunded or under-charged; the difference is below one minor unit per transaction.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass

BILLING_TZ_NAME = "Asia/Kolkata"
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")
UTC = _dt.timezone.utc


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(UTC)


def to_aware(dt: _dt.datetime) -> _dt.datetime:
    """Naive -> UTC-aware; aware unchanged."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def billing_date(dt) -> _dt.date:
    """IST calendar date of a datetime (a plain `date` is returned as is)."""
    if isinstance(dt, _dt.datetime):
        return to_aware(dt).astimezone(IST).date()
    return dt


def day_start(d: _dt.date) -> _dt.datetime:
    """Start (00:00 IST) of billing day `d`, as an aware UTC datetime."""
    return _dt.datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(UTC)


def end_of_billing_day(dt) -> _dt.datetime:
    """The instant the billing day containing `dt` ends (= start of the next IST day), aware UTC."""
    return day_start(billing_date(dt) + _dt.timedelta(days=1))


def to_naive_utc(dt: _dt.datetime) -> _dt.datetime:
    """How saas_core stores datetimes in the database."""
    return to_aware(dt).astimezone(UTC).replace(tzinfo=None)


def prorate_down(amount: int, num: int, den: int) -> int:
    """floor(amount * num / den) in integers; used for refunds and credits."""
    if den <= 0:
        raise ValueError("denominator must be positive")
    return max(0, int(amount) * max(0, int(num)) // int(den))


def prorate_up(amount: int, num: int, den: int) -> int:
    """ceil(amount * num / den) in integers; used for charges."""
    if den <= 0:
        raise ValueError("denominator must be positive")
    return max(0, -(-(int(amount) * max(0, int(num))) // int(den)))


@dataclass(frozen=True)
class Cycle:
    start: _dt.date          # first billing day of the paid cycle
    end: _dt.date            # renewal date: first day of the NEXT cycle (exclusive)
    days: int                # len of the cycle in days (>= 1)

    @property
    def last_day(self) -> _dt.date:
        return self.end - _dt.timedelta(days=1)


def cycle_of(period_start, period_end) -> Cycle:
    """Cycle between two datetimes (or dates). Raises ValueError when the span is shorter than one day."""
    s, e = billing_date(period_start), billing_date(period_end)
    days = (e - s).days
    if days < 1:
        raise ValueError("billing cycle must span at least one day")
    return Cycle(s, e, days)


def days_used(cycle: Cycle, now) -> int:
    """Days started so far, today included, clamped to 0..cycle.days. 0 only before the cycle begins."""
    today = billing_date(now)
    if today < cycle.start:
        return 0
    return max(0, min(cycle.days, (today - cycle.start).days + 1))


@dataclass(frozen=True)
class CancelQuote:
    cycle_start: _dt.date
    cycle_end: _dt.date       # renewal date that will NOT be charged
    cycle_days: int
    days_used: int
    days_unused: int
    paid_minor: int
    refund_minor: int
    charge_minor: int         # paid_minor - refund_minor: what the used days cost
    stops_at: _dt.datetime    # aware UTC: end of the current IST day
    timezone: str = BILLING_TZ_NAME

    def as_dict(self) -> dict:
        d = asdict(self)
        d["cycle_start"], d["cycle_end"] = self.cycle_start.isoformat(), self.cycle_end.isoformat()
        d["stops_at"] = self.stops_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return d


def cancel_quote(paid_minor: int, period_start, period_end, now) -> CancelQuote:
    """What cancelling `now` costs/refunds. `paid_minor` is what the customer holds for THIS cycle."""
    cyc = cycle_of(period_start, period_end)
    paid = max(0, int(paid_minor or 0))
    used = days_used(cyc, now)
    unused = cyc.days - used
    refund = prorate_down(paid, unused, cyc.days)
    return CancelQuote(cyc.start, cyc.end, cyc.days, used, unused, paid, refund, paid - refund,
                       end_of_billing_day(now))


@dataclass(frozen=True)
class ChangeQuote:
    direction: str            # "upgrade" | "downgrade" | "same"
    cycle_start: _dt.date
    cycle_end: _dt.date       # renewal date
    cycle_days: int
    effective_date: _dt.date  # first day on the new plan (next IST day, or the renewal date on the last day)
    effective_at: _dt.datetime  # aware UTC start of effective_date
    days_remaining: int       # days of the cycle that move to the new plan (0 on the last day)
    old_share_minor: int      # what the customer holds for those days on the old plan
    new_share_minor: int      # what those days cost on the new plan
    amount_due_minor: int     # upgrade: payable now
    credit_minor: int         # downgrade: refunded now
    new_held_minor: int       # amount held for the days [effective_date, cycle_end) after the change
    timezone: str = BILLING_TZ_NAME

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("cycle_start", "cycle_end", "effective_date"):
            d[k] = getattr(self, k).isoformat()
        d["effective_at"] = self.effective_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return d


def change_quote(held_minor: int, new_price_minor: int, old_price_minor: int, period_start, period_end, now) -> ChangeQuote:
    """Plan change `now`. `held_minor` = amount actually paid and still held for the current cycle;
    `new_price_minor` / `old_price_minor` = list price of one full cycle of the new / old plan (same interval).
    Direction is by list price. The old share comes from `held_minor`, not the list price, so discounts and
    earlier proration stay consistent."""
    cyc = cycle_of(period_start, period_end)
    today = billing_date(now)
    eff = min(max(today + _dt.timedelta(days=1), cyc.start), cyc.end)
    rem = (cyc.end - eff).days
    held = max(0, int(held_minor or 0))
    old_share = prorate_down(held, rem, cyc.days)
    new_share = prorate_up(new_price_minor, rem, cyc.days)
    if new_price_minor == old_price_minor:
        direction = "same"
    else:
        direction = "upgrade" if new_price_minor > old_price_minor else "downgrade"
    due = max(0, new_share - old_share) if direction == "upgrade" else 0
    credit = max(0, old_share - new_share) if direction == "downgrade" else 0
    new_held = new_share if rem and direction != "same" else old_share
    return ChangeQuote(direction, cyc.start, cyc.end, cyc.days, eff, day_start(eff), rem, old_share, new_share,
                       due, credit, new_held)
