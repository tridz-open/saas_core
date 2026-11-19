"""One-time: older releases stamped some billing datetimes with Frappe's site-local now_datetime(); they are now
naive UTC (like period_start/period_end/plan_change_at, which were always UTC and are not touched here).
Convert the existing rows from the system timezone to naive UTC. No-op when the system timezone is UTC.
Frappe's patch log guarantees this runs once; a re-run would double-shift, so do not register it twice."""
import datetime as _dt

# doctype -> datetime fields that were written site-local
FIELDS = {
    "Subscription": ["first_failure_at", "next_retry_at", "suspend_date", "last_event_at"],
    "Invoice": ["issued_at", "paid_at"],
    "Payment Event": ["next_retry_at"],
}


def _to_utc(value, tz):
    if isinstance(value, str):
        value = _dt.datetime.fromisoformat(value.strip().replace("Z", "")[:26])
    if not isinstance(value, _dt.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def execute():
    import zoneinfo

    import frappe
    name = frappe.utils.get_system_timezone() or "UTC"
    try:
        tz = zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return
    if _dt.datetime.now(tz).utcoffset() == _dt.timedelta(0) and name.upper() in ("UTC", "ETC/UTC", "GMT"):
        return
    for doctype, fields in FIELDS.items():
        for row in frappe.get_all(doctype, fields=["name", *fields]):
            vals = {}
            for f in fields:
                try:
                    v = _to_utc(row.get(f), tz)
                except ValueError:
                    v = None
                if v is not None:
                    vals[f] = v
            if vals:
                frappe.db.set_value(doctype, row["name"], vals, update_modified=False)
