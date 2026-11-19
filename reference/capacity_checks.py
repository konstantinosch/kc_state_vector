"""Local capacity closure at enforced logical-state checkpoints.

These limits are not peak RAM or every intermediate assignment. The observer
records the same checkpoints at which Pager enforces its contract; black-box
search independently verifies the minimum successful limit for each request.
"""

from dataclasses import dataclass
import json

from .model import CapacityError, Item, Pager, Plan, Reservation, Stage
from .oracle import unpaged_oracle
from .retirement import audit_boundary


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def copy_state(value):
    return json.loads(encode(value))


def dimensions(vector):
    """Independent public-vector accounting, including IDs in multiple roles."""
    if vector is None:
        return {"entries": 0, "bytes": 0}
    return {"entries": sum(len(vector[name]) for name in ("coverage", "pending", "omitted"))
            + sum(len(buffer["items"]) for buffer in vector["buffers"] if buffer is not None),
            "bytes": len(encode(vector).encode("utf-8"))}


class CapacityProbe(Pager):
    """Test-only observation; every overridden operation delegates to Pager."""
    def __init__(self, plan, **kwargs):
        super().__init__(plan, **kwargs)
        self.checkpoints = []
        self.completions = []
        self.phase = "request_start"

    def page(self, state=None, **kwargs):
        self.phase = "request_start"
        return super().page(state, **kwargs)

    def _read(self, index, cursor, limit):
        batch = super()._read(index, cursor, limit)
        if self.plan.stages[index].buffer_size is not None:
            self.phase = "prepared_refill" if cursor is not None else "prepared_initial_fill"
        else:
            self.phase = "raw_read"
        return batch

    def _finish(self, vector):
        index = vector["stage"]
        stage = self.plan.stages[index]
        declaration = next((claim for claim in self.plan.reservations if claim.owner == stage.name), None)
        before = copy_state(vector)
        super()._finish(vector)
        self.phase = "owner_completion" if declaration is not None else "stage_completion"
        if declaration is not None:
            pending = set(before["pending"]) & set(declaration.item_ids)
            covered = pending & set(before["coverage"])
            added = set(vector["omitted"]) - set(before["omitted"])
            assert not covered.intersection(added), "covered ownership became an omission"
            self.completions.append({
                "owner": stage.name, "policy": declaration.on_unserved,
                "zero_quota": stage.quota == 0,
                "by_quota": stage.quota is not None and before["selected"][index] == stage.quota,
                "pending_resolved": len(pending), "covered_pending": len(covered),
                "unserved_pending": len(pending - covered), "omissions_added": len(added),
            })

    def _check_capacity(self, vector):
        terminal = vector["stage"] == len(self.plan.stages)
        self.checkpoints.append({"phase": self.phase, "terminal_exempt": terminal,
                                 **dimensions(vector), "state": copy_state(vector)})
        self.phase = "after_candidate_or_settlement"
        return super()._check_capacity(vector)


@dataclass(frozen=True)
class CapacityFixture:
    name: str
    plan: Plan


SCHEDULES = (
    ("wide", {"page_size": 50, "batch_size": 3, "candidate_budget": 1000}),
    ("one", {"page_size": 1, "batch_size": 3, "candidate_budget": 5}),
    ("refill", {"page_size": 2, "batch_size": 3, "candidate_budget": 5}),
)


def fixtures():
    values = {identity: Item(identity, 8 - index) for index, identity in enumerate("ABCDEFGH")}
    all_items = tuple(values.values())
    reserved = tuple(values[identity] for identity in "BDFG")
    cases = []
    for quota, label in ((2, "partial"), (0, "zero")):
        for policy in ("release", "omit"):
            cases.append(CapacityFixture(f"{label}_{policy}", Plan(
                (Stage("early", all_items, 2, buffer_size=3),
                 Stage("owner", reserved, quota, buffer_size=3),
                 Stage("rest", all_items, buffer_size=3)),
                (Reservation("owner", tuple(item.id for item in reserved), on_unserved=policy),))))
    cases.append(CapacityFixture("owner_exhaustion", Plan(
        (Stage("early", all_items, 2), Stage("owner", reserved, buffer_size=2),
         Stage("rest", all_items, buffer_size=3)),
        (Reservation("owner", tuple(item.id for item in reserved)),))))
    cases.append(CapacityFixture("covered_zero_quota", Plan(
        (Stage("early", (values["A"], values["B"]), 2),
         Stage("middle", (values["C"], values["E"]), 1),
         Stage("owner", (values["B"], values["D"], values["F"]), 0),
         Stage("rest", (values["D"], values["F"], values["G"], values["H"]), buffer_size=3)),
        (Reservation("owner", ("B", "D", "F"), yield_from=("middle", "rest")),))))
    cases.append(CapacityFixture("prepared_refill", Plan((Stage("only", all_items, buffer_size=3),))))
    short, long = Item("short", 2), Item("στοιχείο-" + "x" * 80, 1)
    cases.append(CapacityFixture("unicode_owner_completion", Plan(
        (Stage("empty", (), 0), Stage("owner", (short, long), 2), Stage("rest", (short, long))),
        (Reservation("owner", (long.id,)),), scope="συνέχεια-κόσμος")))
    cases.append(CapacityFixture("zero_entry_raw", Plan((Stage("only", all_items),))))
    cases.append(CapacityFixture("terminal_empty", Plan(())))
    cases.append(CapacityFixture("terminal_zero_quota", Plan(
        (Stage("owner", (values["A"],), 0),), (Reservation("owner", ("A",)),))))
    return tuple(cases)


def _caps(dimension, limit):
    return {"max_state_entries": limit if dimension == "entries" else None,
            "max_state_bytes": limit if dimension == "bytes" else None}


def _attempt(plan, incoming, arguments, *, pager=None, **caps):
    supplied = copy_state(incoming)
    before = encode(supplied)
    try:
        return (Pager(plan, **caps) if pager is None else pager).page(supplied, **arguments)
    finally:
        assert encode(supplied) == before, "capacity attempt mutated its supplied continuation"


def _minimum_cap(plan, incoming, arguments, dimension, upper):
    """Search observable pass/fail behavior without consulting checkpoint values."""
    low, high, attempts = 0, upper, 0
    while low < high:
        middle = (low + high) // 2
        attempts += 1
        try:
            _attempt(plan, incoming, arguments, **_caps(dimension, middle))
        except CapacityError:
            low = middle + 1
        else:
            high = middle
    return low, attempts


def _finish_retry(plan, page, prefix, arguments):
    history = list(prefix) + list(page.rows)
    audit_boundary(plan, page.state, history)
    state = page.state
    bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
    for _ in range(bound):
        if state is None:
            assert history == unpaged_oracle(plan), "retried traversal differs from oracle"
            return
        page = _attempt(plan, state, arguments, max_state_entries=None, max_state_bytes=None)
        history.extend(page.rows)
        audit_boundary(plan, page.state, history)
        state = page.state
    raise AssertionError("retry continuation did not terminate")


def run_capacity_checks():
    records = []
    witnesses = {name: None for name in (
        "pending_to_coverage", "omit_completion", "release_completion",
        "covered_zero_quota", "exhausted_owner", "refill_exceeds_returned_size",
        "failure_at_owner_completion", "zero_entry_success", "terminal_zero_limits", "unicode_bytes",
    )}
    totals = dict(requests_checked=0, exact_fit_checks=0, one_below_failures=0,
                  healthy_retry_checks=0, black_box_search_attempts=0, checkpoints=0)
    for fixture in fixtures():
        for schedule, arguments in SCHEDULES:
            plan, state, history = fixture.plan, None, []
            bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
            for request in range(1, bound + 1):
                label = {"fixture": fixture.name, "schedule": schedule, "request": request}
                probe = CapacityProbe(plan, max_state_entries=None, max_state_bytes=None)
                baseline = _attempt(plan, state, arguments, pager=probe)
                active = [point for point in probe.checkpoints if not point["terminal_exempt"]]
                peaks = {dimension: max((point[dimension] for point in active), default=0)
                         for dimension in ("entries", "bytes")}
                returned = dimensions(baseline.state)
                totals["requests_checked"] += 1
                totals["checkpoints"] += len(probe.checkpoints)
                for dimension in ("entries", "bytes"):
                    exact = _attempt(plan, state, arguments, **_caps(dimension, peaks[dimension]))
                    assert exact == baseline, f"{label}: exact-fit {dimension} changed the page"
                    totals["exact_fit_checks"] += 1
                    minimum, attempts = _minimum_cap(plan, state, arguments, dimension, peaks[dimension])
                    totals["black_box_search_attempts"] += attempts
                    assert minimum == peaks[dimension], f"{label}: measured {dimension} threshold differs from black-box minimum"
                    if minimum > 0:
                        constrained = CapacityProbe(plan, **_caps(dimension, minimum - 1))
                        for _ in range(2):
                            try:
                                _attempt(plan, state, arguments, pager=constrained)
                            except CapacityError:
                                totals["one_below_failures"] += 1
                            else:
                                raise AssertionError(f"{label}: one-below {dimension} unexpectedly succeeded")
                        failed = constrained.checkpoints[-1]
                        if failed["phase"] == "owner_completion":
                            witnesses["failure_at_owner_completion"] = {
                                **label, "dimension": dimension, "failed_limit": minimum - 1,
                                "required_at_checkpoint": failed[dimension]}
                        retry = _attempt(plan, state, arguments, **_caps(dimension, minimum))
                        assert retry == baseline, f"{label}: raised-capacity retry changed the Page"
                        _finish_retry(plan, retry, history, arguments)
                        totals["healthy_retry_checks"] += 1
                both = _attempt(plan, state, arguments, max_state_entries=peaks["entries"], max_state_bytes=peaks["bytes"])
                assert both == baseline, f"{label}: combined exact limits changed the Page"
                totals["exact_fit_checks"] += 1
                for previous, following in zip(active, active[1:]):
                    promoted = (set(previous["state"]["pending"]) - set(following["state"]["pending"])) \
                               & (set(following["state"]["coverage"]) - set(previous["state"]["coverage"]))
                    if promoted:
                        witnesses["pending_to_coverage"] = {**label, "promoted_ids": sorted(promoted)}
                for event in probe.completions:
                    if event["policy"] == "omit" and event["omissions_added"]:
                        witnesses["omit_completion"] = {**label, **event}
                    if event["policy"] == "release" and event["unserved_pending"]:
                        witnesses["release_completion"] = {**label, **event}
                    if event["zero_quota"] and event["covered_pending"] and event["unserved_pending"]:
                        witnesses["covered_zero_quota"] = {**label, **event}
                    if not event["by_quota"]:
                        witnesses["exhausted_owner"] = {**label, **event}
                for point in active:
                    if (baseline.state is not None and point["phase"] == "prepared_refill"
                            and point["entries"] > returned["entries"] and point["bytes"] > returned["bytes"]
                            and all(dimensions(state)[key] <= returned[key] for key in ("entries", "bytes"))):
                        # The incoming state fits a returned-size budget. Require
                        # that both dimensions actually fail at refill, not at
                        # request entry or some earlier, unrelated checkpoint.
                        failed_phases = {}
                        for dimension in ("entries", "bytes"):
                            returned_cap = CapacityProbe(plan, **_caps(dimension, returned[dimension]))
                            try:
                                _attempt(plan, state, arguments, pager=returned_cap)
                            except CapacityError:
                                failed_phases[dimension] = returned_cap.checkpoints[-1]["phase"]
                            else:
                                raise AssertionError("returned-size budget accepted a larger required refill checkpoint")
                        if all(phase == "prepared_refill" for phase in failed_phases.values()):
                            witnesses["refill_exceeds_returned_size"] = {
                                **label, "checkpoint": {key: point[key] for key in ("phase", "entries", "bytes")},
                                "returned": returned, "incoming": dimensions(state),
                                "failed_phases_at_returned_limits": failed_phases,
                                "enforced_peaks": peaks}
                if peaks["entries"] == 0 and baseline.rows:
                    witnesses["zero_entry_success"] = {**label, "selected_rows": len(baseline.rows)}
                if not active:
                    assert baseline.state is None
                    witnesses["terminal_zero_limits"] = {**label, "enforced_peaks": peaks}
                if fixture.name == "unicode_owner_completion":
                    for point in active:
                        raw_utf8 = len(json.dumps(point["state"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                        if point["bytes"] > raw_utf8:
                            witnesses["unicode_bytes"] = {**label, "canonical_ascii_escaped_bytes": point["bytes"],
                                                           "raw_utf8_json_bytes": raw_utf8}
                records.append({**label, "enforced_checkpoint_peaks": peaks, "returned_state": returned,
                                "checkpoints": len(probe.checkpoints), "owner_completions": probe.completions})
                history.extend(baseline.rows)
                audit_boundary(plan, baseline.state, history)
                if baseline.state is None:
                    assert history == unpaged_oracle(plan)
                    break
                state = copy_state(baseline.state)
            else:
                raise AssertionError(f"{fixture.name}/{schedule}: healthy traversal did not terminate")
    missing = [name for name, value in witnesses.items() if value is None]
    assert not missing, f"capacity coverage witnesses were not exercised: {missing}"
    return {"status": "passed", "fixtures": len(fixtures()), "schedules": len(SCHEDULES),
            "scope": "Enforced logical-state checkpoints, not peak RAM or every intermediate assignment; exact-fit and one-below tests isolate each dimension.",
            **totals, "witnesses": witnesses, "request_records": records}
