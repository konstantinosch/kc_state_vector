"""Selection adapters for the local placement experiment.

Exact selection delegates to the published pagers and independent auditors. The
best-effort policy is deliberately different: a plain descending item stream
over the same fixed eligible scope, below the client's last representative. It
does not reconstruct priorities, reservations, grouping or earlier coverage.
"""

import hashlib
import json

from reference.model import Plan as CorePlan, Pager as CorePager
from reference.oracle import unpaged_oracle as core_oracle
from reference.retirement import audit_boundary as core_audit
from reference.grouped.model import Plan as GroupedPlan, Pager as GroupedPager
from reference.grouped.oracle import unpaged_oracle as grouped_oracle
from reference.grouped.audit import audit_boundary as grouped_audit


DEFAULT_PARAMS = {"page_size": 50, "batch_size": 50, "candidate_budget": 1000}


def parameters(values):
    """Return complete positive request limits without mutating the caller."""
    if type(values) is not dict or not set(values) <= set(DEFAULT_PARAMS):
        raise ValueError("Request parameters must name page, batch or candidate limits.")
    result = {**DEFAULT_PARAMS, **values}
    if any(type(value) is not int or value < 1 for value in result.values()):
        raise ValueError("Page, batch and candidate limits must be positive integers.")
    return result


class Adapter:
    """Normalize core and grouped rows to [stage, representative, members]."""

    def __init__(self, plan, representation="ids", source=None):
        if not isinstance(plan, (CorePlan, GroupedPlan)):
            raise ValueError("Adapter requires a published core or grouped Plan.")
        self.grouped = isinstance(plan, GroupedPlan)
        if representation not in ("ids", "spans", "hybrid") or (
                not self.grouped and representation != "ids"):
            raise ValueError("Core uses IDs; grouped supports IDs, spans or hybrid.")
        self.plan, self.representation, self.source = plan, representation, source
        records = (plan.records if self.grouped else
                   tuple({item.id: item for stage in plan.stages for item in stage.items}.values()))
        self.records = tuple(sorted(records, key=lambda item: item.position, reverse=True))
        self._positions = {item.id: [item.sort_key, item.id] for item in self.records}
        identity = {"adapter_version": 1, "plan": plan.fingerprint, "scope": plan.scope,
                    "grouped": self.grouped, "representation": representation}
        self.binding = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def position(self, identity):
        try:
            return list(self._positions[identity])
        except (KeyError, TypeError) as exc:
            raise ValueError("Unknown scoped identity.") from exc

    def validate_cursor(self, cursor):
        if cursor is None:
            return
        if (type(cursor) is not list or len(cursor) != 2
                or type(cursor[0]) is not int or type(cursor[1]) is not str
                or self._positions.get(cursor[1]) != cursor):
            raise ValueError("Cursor must be an exact full position in this fixed scope.")

    @staticmethod
    def _result(page, grouped):
        rows = ([[stage, identity, list(members)] for stage, identity, members in page.rows]
                if grouped else [[stage, identity, [identity]] for stage, identity in page.rows])
        return {"rows": rows, "state": page.state,
                "work": {"consumed": page.consumed, "fetched": page.fetched, "reads": page.reads}}

    def select(self, state, params):
        options = parameters(params)
        pager = (GroupedPager(self.plan, self.representation, source=self.source)
                 if self.grouped else CorePager(self.plan, source=self.source))
        return self._result(pager.page(state, **options), self.grouped)

    def expected_rows(self):
        if self.grouped:
            return [[stage, identity, list(members)]
                    for stage, identity, members in grouped_oracle(self.plan)]
        return [[stage, identity, [identity]] for stage, identity in core_oracle(self.plan)]

    def audit(self, state, rows_so_far):
        """Audit exact selection only; approximate rows must not use this contract."""
        if self.grouped:
            return grouped_audit(self.plan, state, rows_so_far)
        return core_audit(self.plan, state,
                          [(stage, identity) for stage, identity, members in rows_so_far
                           if self._check_single_member(identity, members)])

    @staticmethod
    def _check_single_member(identity, members):
        if members != [identity] and members != (identity,):
            raise AssertionError("Core rows must represent exactly their own identity.")
        return True

    def best_effort(self, cursor, params):
        """Return a raw keyset page; its changed semantics are intentional and explicit.

        The first cursor is the last delivered representative, not the discarded
        vector's consumed cursor. Follow-ups pass state['cursor']. This scope can
        include already represented members or formerly omitted reservations.
        """
        options = parameters(params)
        self.validate_cursor(cursor)
        remaining = [item for item in self.records
                     if cursor is None or item.position < tuple(cursor)]
        count = min(options["page_size"], options["candidate_budget"], len(remaining))
        chosen = remaining[:count]
        reads = (count + options["batch_size"] - 1) // options["batch_size"] if count else 1
        fetched = min(len(remaining), options["candidate_budget"], reads * options["batch_size"])
        state = None if count == len(remaining) else {"cursor": self.position(chosen[-1].id)}
        return {"rows": [["best_effort", item.id, [item.id]] for item in chosen],
                "state": state, "work": {"consumed": count, "fetched": fetched, "reads": reads}}
