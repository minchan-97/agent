"""
fakedb.py — 시험용 가짜 Supabase.

코어 모듈이 쓰는 만큼만 흉내 낸다: select/eq/neq/in_/gte/lte/order/limit,
insert/update/upsert/delete. 한 사람 몫(Scoped)처럼 쓴다.

actions 표에는 실제 DB 의 CHECK 제약 일부를 그대로 건다.
코드가 제약을 어기면 실제 Supabase 처럼 예외가 난다.
"""
from datetime import datetime, timezone


class ConstraintError(Exception):
    pass


def _check_actions(r: dict):
    if r.get("origin") == "external" and r.get("tier") != 4:
        raise ConstraintError("external_is_strict")
    if r.get("tier") == 4 and r.get("auto_executed"):
        raise ConstraintError("t4_never_auto")
    if r.get("tier") == 4 and r.get("status") in ("done", "scheduled") and not r.get("approved_by_human"):
        raise ConstraintError("t4_needs_human")
    if r.get("tainted") and r.get("auto_executed") and (r.get("tier") or 0) >= 3:
        raise ConstraintError("tainted_never_auto")


CHECKS = {"actions": _check_actions}
DEFAULTS = {
    "actions": {"approved_by_human": False, "auto_executed": False, "tainted": False,
                "status": "proposed", "origin": "user", "result": None, "task_id": None},
    "tasks": {"status": "open", "steps": 0, "max_steps": 12, "log": [], "skill_id": None,
              "last_step_at": None},
    "action_trust": {"clean": 0, "promoted": False},
}
PKEYS = {"action_trust": ("kind",), "agent_state": ("key",)}


class _Res:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Query:
    def __init__(self, db, name, op, payload=None, count=None):
        self.db, self.name, self.op, self.payload = db, name, op, payload
        self.filters, self._order, self._limit, self._count = [], None, None, count

    def eq(self, k, v):   self.filters.append(lambda r: r.get(k) == v); return self
    def neq(self, k, v):  self.filters.append(lambda r: r.get(k) != v); return self
    def in_(self, k, vs): self.filters.append(lambda r: r.get(k) in vs); return self
    def gte(self, k, v):  self.filters.append(lambda r: r.get(k) is not None and str(r.get(k)) >= str(v)); return self
    def lte(self, k, v):  self.filters.append(lambda r: r.get(k) is not None and str(r.get(k)) <= str(v)); return self

    def order(self, k, desc=False, nullsfirst=False):
        self._order = (k, desc); return self

    def limit(self, n):
        self._limit = n; return self

    def _match(self):
        rows = [r for r in self.db.rows(self.name) if all(f(r) for f in self.filters)]
        if self._order:
            k, desc = self._order
            rows.sort(key=lambda r: (r.get(k) is None, r.get(k) or 0), reverse=desc)
        return rows

    def execute(self):
        t = self.db.rows(self.name)
        if self.op == "select":
            rows = self._match()
            n = len(rows)
            if self._limit is not None:
                rows = rows[: self._limit]
            return _Res([dict(r) for r in rows], n if self._count else None)
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for it in items:
                row = {**DEFAULTS.get(self.name, {}), **it}
                row.setdefault("id", self.db.next_id(self.name))
                row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                CHECKS.get(self.name, lambda r: None)(row)
                t.append(row)
                out.append(dict(row))
            return _Res(out)
        if self.op == "upsert":
            keys = PKEYS.get(self.name, ("id",))
            it = self.payload
            for r in t:
                if all(r.get(k) == it.get(k) for k in keys):
                    new = {**r, **it}
                    CHECKS.get(self.name, lambda x: None)(new)
                    r.update(new)
                    return _Res([dict(r)])
            self.op = "insert"
            return self.execute()
        if self.op == "update":
            out = []
            for r in self._match():
                new = {**r, **self.payload}
                CHECKS.get(self.name, lambda x: None)(new)
                r.update(new)
                out.append(dict(r))
            return _Res(out)
        if self.op == "delete":
            gone = self._match()
            self.db.tables[self.name] = [r for r in t if r not in gone]
            return _Res(gone)
        raise ValueError(self.op)


class _Table:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, cols="*", count=None): return _Query(self.db, self.name, "select", count=count)
    def insert(self, data, **k):            return _Query(self.db, self.name, "insert", data)
    def upsert(self, data, **k):            return _Query(self.db, self.name, "upsert", data)
    def update(self, data, **k):            return _Query(self.db, self.name, "update", data)
    def delete(self, **k):                  return _Query(self.db, self.name, "delete")


class FakeDB:
    """한 사람 몫의 DB 처럼 쓴다 (Scoped 대신 넘긴다)."""

    def __init__(self):
        self.tables, self._ids = {}, {}
        self.uid = "test-user"
        self.raw = self

    def rows(self, name):
        return self.tables.setdefault(name, [])

    def next_id(self, name):
        self._ids[name] = self._ids.get(name, 0) + 1
        return self._ids[name]

    def table(self, name):
        return _Table(self, name)
