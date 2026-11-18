"""Minimal in-memory fake of the `frappe` API for pure-python tests. Installed by conftest."""
import sys, types, uuid, datetime as _dt, json


class _dict(dict):
    __getattr__ = dict.get
    def __setattr__(s, k, v): s[k] = v
    def copy(s): return _dict(s)

class _Row(dict):
    """Child row like a real frappe Document: attribute/.set() ok, item assignment rejected."""
    def __getattr__(s, k): return dict.get(s, k)
    def __setattr__(s, k, v): dict.__setitem__(s, k, v)
    def __setitem__(s, k, v): raise TypeError("'ProvisionJobStep' object does not support item assignment")
    def set(s, k, v): dict.__setitem__(s, k, v)
    def as_dict(s): return _dict(s)

class ValidationError(Exception): pass
class PermissionError(Exception): pass          # noqa: A001
class DoesNotExistError(Exception): pass
class DuplicateEntryError(Exception): pass
class AuthenticationError(Exception): pass


class Store:
    def __init__(self): self.docs = {}; self.cache = {}; self.enqueued = []; self.realtime = []; self.commits = 0; self.messages = []


def build():
    st = Store()
    f = types.ModuleType("frappe")
    f._store = st
    f._dict = _dict
    f.ValidationError, f.PermissionError, f.DoesNotExistError = ValidationError, PermissionError, DoesNotExistError
    f.DuplicateEntryError, f.AuthenticationError = DuplicateEntryError, AuthenticationError
    f.session = _dict(user="Administrator")
    f.conf = _dict()
    f.local = _dict(site="test.local", request_id="req-1")
    f.form_dict = _dict()
    f.flags = _dict()
    def _append_hook(target, key, value):  # mirrors frappe.utils.append_hook / append_to_list
        if isinstance(value, dict):
            target.setdefault(key, {})
            for k, v in value.items():
                _append_hook(target[key], k, v)
        else:
            target.setdefault(key, [])
            target[key].extend(value if isinstance(value, list) else [value])

    def get_hooks(key=None, default=None, app_name=None):
        """Real Frappe shape: dict hooks become {name: [paths...]}, scalars become lists (one entry per app)."""
        merged: dict = {}
        raw = f.conf.get('_test_hooks', {})
        for k, v in raw.items():
            for part in (v if isinstance(v, list) and v and all(isinstance(x, dict) for x in v) else [v]):
                _append_hook(merged, k, part)
        return merged.get(key, default if default is not None else []) if key else merged
    f.get_hooks = get_hooks
    f.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    f.generate_hash = lambda *a, **k: uuid.uuid4().hex[: k.get("length", 10)]
    f.as_json = lambda o, **k: json.dumps(o, default=str)
    f.parse_json = lambda s: json.loads(s) if isinstance(s, str) else s
    f.get_roles = lambda user=None: list(getattr(f, "_roles", ["System Manager"]))
    f.log_error = lambda *a, **k: st.messages.append(("error", a, k))
    f.msgprint = lambda *a, **k: st.messages.append(("msg", a, k))
    def throw(msg, exc=ValidationError, **k): raise (exc or ValidationError)(msg)
    f.throw = throw
    f.enqueue = lambda method, **kw: st.enqueued.append((method, kw))
    f.publish_realtime = lambda event=None, message=None, **kw: st.realtime.append((event, message, kw))

    class Doc(_dict):
        def insert(self, **k):
            self.setdefault("name", self.get("name") or uuid.uuid4().hex[:10])
            key = (self["doctype"], self["name"])
            if key in st.docs: raise DuplicateEntryError(f"{key} exists")
            self._check_unique()
            if callable(getattr(type(self), "validate", None)): self.validate()
            st.docs[key] = self; return self
        def _check_unique(self): pass
        def save(self, **k):
            if callable(getattr(type(self), "validate", None)): self.validate()
            st.docs[(self["doctype"], self["name"])] = self; return self
        def reload(self): return self
        def delete(self, **k): st.docs.pop((self["doctype"], self["name"]), None)
        def get(self, k, d=None): return dict.get(self, k, d)
        def db_set(self, k, v=None, **_):
            if isinstance(k, dict): self.update(k)
            else: self[k] = v
        def append(self, field, row): self.setdefault(field, []).append(_Row(row)); return self.get(field)[-1]
        def as_dict(self): return _dict(self)
        def check_permission(self, *a, **k): return True
        def add_comment(self, *a, **k): pass
        def run_method(self, m, *a, **k): return getattr(self, m)(*a, **k)

    f._Doc = Doc
    def new_doc(doctype, **kw): return Doc(doctype=doctype, **kw)
    def get_doc(a, b=None, **kw):
        if isinstance(a, dict): return Doc(a)
        d = st.docs.get((a, b))
        if not d: raise DoesNotExistError(f"{a} {b}")
        return d
    f.new_doc, f.get_doc = new_doc, get_doc
    def _match(d, filters):
        if not filters: return True
        if isinstance(filters, str): return d.get("name") == filters
        items = filters.items() if isinstance(filters, dict) else [(x[0], x[-1]) if len(x) < 4 else (x[1], x[3]) for x in filters]
        for k, v in items:
            dv = d.get(k)
            if isinstance(v, (list, tuple)) and v and v[0] in ("in", "!=", ">", "<", ">=", "<=", "like"):
                op, val = v[0], v[1]
                if op == "in" and dv not in val: return False
                if op == "!=" and dv == val: return False
                if op == ">" and not dv > val: return False
                if op == "<" and not dv < val: return False
                if op == ">=" and not dv >= val: return False
                if op == "<=" and not dv <= val: return False
            elif dv != v: return False
        return True
    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None, limit=None, pluck=None, **kw):
        rows = [d for (dt, _), d in st.docs.items() if dt == doctype and _match(d, filters)]
        if order_by:
            col, _, dr = order_by.replace("`", "").partition(" ")
            rows.sort(key=lambda d: (d.get(col) is None, d.get(col)), reverse=dr.strip().lower() == "desc")
        lim = limit or limit_page_length
        if lim: rows = rows[: int(lim)]
        if pluck: return [r.get(pluck) for r in rows]
        if fields and fields != ["*"]: rows = [_dict({k: r.get(k) for k in fields}) for r in rows]
        return rows
    f.get_all = f.get_list = get_all
    db = types.SimpleNamespace()
    db.exists = lambda dt, name=None: (any(True for (d, n), v in st.docs.items() if d == dt and _match(v, name) ) if isinstance(name, (dict, list)) else (dt, name) in st.docs)
    def get_value(dt, name, field=None, as_dict=False, **k):
        rows = get_all(dt, name if isinstance(name, (dict, list)) else {"name": name})
        if not rows: return None
        r = rows[0]
        if isinstance(field, (list, tuple)): return _dict({x: r.get(x) for x in field}) if as_dict else tuple(r.get(x) for x in field)
        return r.get(field or "name")
    db.get_value = get_value
    def set_value(dt, name, field, value=None, **k):
        d = st.docs[(dt, name)]
        d.update(field) if isinstance(field, dict) else d.__setitem__(field, value)
    db.set_value = set_value
    db.count = lambda dt, filters=None: len(get_all(dt, filters))
    db.commit = lambda: setattr(st, "commits", st.commits + 1)
    db.rollback = lambda: None
    db.sql = lambda *a, **k: []
    f.db = db
    class Cache:
        def get_value(s, k): return st.cache.get(k)
        def set_value(s, k, v, expires_in_sec=None): st.cache[k] = v
        def delete_value(s, k): st.cache.pop(k, None)
        def setnx(s, k, v): return st.cache.setdefault(k, v) is v
    f.cache = lambda: Cache()
    # utils
    u = types.ModuleType("frappe.utils")
    u.now_datetime = lambda: _dt.datetime.utcnow()
    u.now = lambda: _dt.datetime.utcnow().isoformat(sep=" ")
    u.get_datetime = lambda v: v if isinstance(v, _dt.datetime) else _dt.datetime.fromisoformat(str(v))
    u.getdate = lambda v=None: (v or _dt.date.today()) if not isinstance(v, str) else _dt.date.fromisoformat(v[:10])
    u.add_to_date = lambda d=None, years=0, months=0, days=0, hours=0, minutes=0, seconds=0, **k: (d or _dt.datetime.utcnow()) + _dt.timedelta(days=days + 30 * months + 365 * years, hours=hours, minutes=minutes, seconds=seconds)
    u.cint = lambda v, d=0: int(v) if v not in (None, "") else d
    u.flt = lambda v, p=None: float(v or 0)
    u.cstr = lambda v: "" if v is None else str(v)
    u.get_url = lambda *a, **k: "https://control.test"
    u.time_diff_in_seconds = lambda a, b: (u.get_datetime(a) - u.get_datetime(b)).total_seconds()
    f.utils = u
    m = types.ModuleType("frappe.model"); md = types.ModuleType("frappe.model.document")
    md.Document = Doc; m.document = md; f.model = m
    sys.modules.update({"frappe": f, "frappe.utils": u, "frappe.model": m, "frappe.model.document": md})
    return f
