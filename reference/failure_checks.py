"""Local read-failure/retry sweep for the fixed-data core/reservation contract.

Every actual read in each healthy request is failed in isolation. Retrying the
unchanged incoming state must reproduce the same logical Page. This checks an
atomic function result, not exactly-once delivery across a network.
Source call counters and other external read side effects are not rolled back;
Page work counters describe only the successful retry's own request.
"""

from collections import Counter
from dataclasses import dataclass, replace
import json

from .model import Item, MemorySource, Pager, Plan, ReadError, Reservation, Stage
from .oracle import unpaged_oracle
from .retirement import audit_boundary


def _copy(value):
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


@dataclass(frozen=True)
class Fixture:
    name: str
    plan: Plan


@dataclass(frozen=True)
class Schedule:
    name: str
    pages: tuple[int, ...]
    batches: tuple[int, ...]
    budgets: tuple[int, ...]

    def arguments(self, request):
        return dict(page_size=self.pages[request % len(self.pages)],
                    batch_size=self.batches[request % len(self.batches)],
                    candidate_budget=self.budgets[request % len(self.budgets)])


SCHEDULES = (
    Schedule("wide_50", (50,), (2,), (1000,)),
    Schedule("crossing_pages", (2, 1, 3), (3, 2), (4, 2, 5)),
)
FAULT_MODES = ("exception", "incomplete")


def fixtures():
    values = {identity: Item(identity, 20 - index) for index, identity in enumerate("ABCDEFGHI")}

    def stage(name, ids, quota=None, prepared=False):
        return Stage(name, tuple(values[value] for value in ids), quota, 2 if prepared else None)

    result = []
    for name, quota, policy, prepared in (
        ("quota_release_raw", 2, "release", False),
        ("quota_omit_prepared", 2, "omit", True),
        ("exhaustion_prepared", None, "omit", True),
        ("zero_release", 0, "release", False),
        ("zero_omit", 0, "omit", True),
    ):
        stages = (stage("early", "ABCDEFGHI", 2, prepared),
                  stage("owner", "BDFH", quota, prepared),
                  stage("rest", "ABCDEFGHI", prepared=prepared))
        result.append(Fixture(name, Plan(stages, (Reservation("owner", tuple("BDFH"),
                                                            on_unserved=policy),), scope=name)))
    for policy in ("release", "omit"):
        name = f"covered_zero_{policy}"
        stages = (stage("early", "ABC", 2, True), stage("owner", "BDF", 0, True),
                  stage("rest", "ABCDEFGHI", prepared=True))
        result.append(Fixture(name, Plan(stages, (Reservation("owner", tuple("BDF"),
                                                            yield_from=("rest",), on_unserved=policy),), scope=name)))
    name = "covered_partial_prepared"
    stages = (stage("early", "ABC", 2, True), stage("owner", "BDFH", 2, True),
              stage("rest", "ABCDEFGHI", prepared=True))
    result.append(Fixture(name, Plan(stages, (Reservation("owner", tuple("BDFH"),
                                                        yield_from=("rest",), on_unserved="release"),), scope=name)))
    name = "overlapping_owners"
    stages = (stage("early", "ABCDEFGHI", 2, True), stage("owner", "BDF", 1, True),
              stage("later_owner", "BDFH", 0, True), stage("rest", "ABCDEFGHI", prepared=True))
    claims = (Reservation("later_owner", tuple("BDFH"), on_unserved="omit"),
              Reservation("owner", tuple("BDF"), on_unserved="release"))
    result.append(Fixture(name, Plan(stages, claims, scope=name)))
    return tuple(result)


class TracingPager(Pager):
    """Record transition witnesses without changing the production page loop."""

    def __init__(self, plan, incoming):
        super().__init__(plan)
        self.read_trace = []
        self.finished_owners = []
        self.vector = None
        self.initial_selected = sum(incoming["selected"]) if incoming is not None else 0
        self.saved_tails = {index for index, buffer in enumerate(incoming["buffers"])
                            if buffer is not None and buffer["items"]} if incoming is not None else set()
        self.stage_reads = Counter()
        declarations = {claim.owner: claim for claim in plan.reservations}
        self.claims = {}
        for index, stage in enumerate(plan.stages):
            declaration = declarations.get(stage.name)
            if declaration is not None:
                for identity in declaration.item_ids:
                    self.claims.setdefault(identity, (index, declaration.on_unserved))

    def _check_capacity(self, vector):
        self.vector = vector
        return super()._check_capacity(vector)

    def _finish(self, vector):
        index = vector["stage"]
        stage = self.plan.stages[index]
        declarations = [claim for claim in self.plan.reservations if claim.owner == stage.name]
        if declarations:
            pending = {identity for identity in vector["pending"] if self.claims[identity][0] == index}
            covered = pending & set(vector["coverage"])
            reason = "zero_quota" if stage.quota == 0 else (
                "quota" if stage.quota is not None and vector["selected"][index] >= stage.quota else "exhaustion")
            self.finished_owners.append(dict(owner=stage.name, reason=reason,
                                             policy=declarations[0].on_unserved,
                                             covered=len(covered), unserved=len(pending - covered)))
        return super()._finish(vector)

    def _read(self, index, cursor, limit):
        stage = self.plan.stages[index]
        prepared = stage.buffer_size is not None
        witnesses = {"prepared_read" if prepared else "raw_read"}
        if self.vector is not None and sum(self.vector["selected"]) > self.initial_selected:
            witnesses.add("after_tentative_selection")
        if prepared and cursor is not None:
            witnesses.add("prepared_refill")
            if self.stage_reads[index]:
                witnesses.add("refill_after_same_request_consumption")
            if index in self.saved_tails:
                witnesses.add("refill_after_saved_tail")
        for event in self.finished_owners:
            witnesses.add("after_owner_completion")
            witnesses.add(f"after_owner_{event['reason']}")
            if event["unserved"]:
                witnesses.add(f"after_{event['policy']}")
            if event["covered"]:
                witnesses.add("after_covered_owner_resolution")
        self.read_trace.append({"stage": stage.name,
                                "cursor": None if cursor is None else list(cursor),
                                "limit": limit, "witnesses": sorted(witnesses)})
        self.stage_reads[index] += 1
        return super()._read(index, cursor, limit)


class OneShotFailure(MemorySource):
    """Fail precisely one ordinal read, then behave as the healthy source."""

    def __init__(self, read_number, mode):
        self.read_number, self.mode = read_number, mode
        self.calls = self.failures = 0

    def read(self, stage, cursor, limit):
        self.calls += 1
        batch = super().read(stage, cursor, limit)
        if self.calls == self.read_number:
            self.failures += 1
            if self.mode == "exception":
                raise OSError("injected one-shot read failure")
            if self.mode == "incomplete":
                return replace(batch, complete=False)
            raise ValueError(f"Unknown fault mode: {self.mode}")
        return batch


def _check_page(plan, page, history, expected):
    _require(page.rows == tuple(expected[len(history):len(history) + len(page.rows)]),
             "output prefix differs from independent oracle")
    history = [*history, *page.rows]
    audit_boundary(plan, page.state, history)
    return history


def _finish_recovery(plan, schedule, request, page, prefix, expected):
    """Verify the resumed traversal, including the successful retry boundary."""
    history = _check_page(plan, page, prefix, expected)
    audited = 1
    bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
    for offset in range(bound):
        if page.state is None:
            _require(history == expected, "recovered traversal did not reproduce whole result")
            return audited
        incoming = _copy(page.state)
        saved = _copy(incoming)
        page = Pager(plan).page(incoming, **schedule.arguments(request + offset + 1))
        _require(incoming == saved, "recovery mutated its continuation")
        history = _check_page(plan, page, history, expected)
        audited += 1
    raise AssertionError("recovered traversal did not terminate")


def run_failure_checks():
    """Sweep every read in every selected request; return countable local evidence."""
    summary = dict(fixture_count=0, baseline_traversals=0, baseline_requests=0,
                   read_positions=0, injected_failures=0, fresh_source_retries=0,
                   same_source_retries=0, complete_recovery_traversals=0,
                   audited_recovery_boundaries=0, fault_modes=list(FAULT_MODES),
                   witness_counts={}, runs=[])
    witnesses = Counter()
    for fixture in fixtures():
        summary["fixture_count"] += 1
        plan = fixture.plan
        expected = unpaged_oracle(plan)
        for schedule in SCHEDULES:
            state, history = None, []
            requests = reads = failures = 0
            bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
            for request in range(bound):
                incoming = _copy(state)
                saved = _copy(incoming)
                tracing = TracingPager(plan, incoming)
                arguments = schedule.arguments(request)
                healthy = tracing.page(incoming, **arguments)
                _require(incoming == saved, "healthy request mutated incoming continuation")
                _require(healthy.reads == len(tracing.read_trace), "read trace missed a source call")
                for read_number, trace in enumerate(tracing.read_trace, 1):
                    witnesses.update(trace["witnesses"])
                    reads += 1
                    for mode in FAULT_MODES:
                        label = f"{fixture.name}/{schedule.name} request {request + 1} read {read_number} {mode}"
                        failed_input = _copy(saved)
                        source = OneShotFailure(read_number, mode)
                        pager = Pager(plan, source=source)
                        try:
                            pager.page(failed_input, **arguments)
                        except ReadError:
                            pass
                        else:
                            raise AssertionError(f"{label}: failure returned a Page")
                        _require(source.failures == 1 and source.calls == read_number,
                                 f"{label}: requested injection point was not reached exactly once")
                        _require(failed_input == saved, f"{label}: failure changed incoming state")

                        fresh_input = _copy(saved)
                        fresh = Pager(plan).page(fresh_input, **arguments)
                        _require(fresh_input == saved and fresh == healthy,
                                 f"{label}: fresh-source retry differs from healthy Page")
                        retry = pager.page(failed_input, **arguments)
                        _require(failed_input == saved and retry == healthy,
                                 f"{label}: recovered same-source retry differs from healthy Page")
                        _require(source.failures == 1, f"{label}: source failed more than once")
                        audited = _finish_recovery(plan, schedule, request, retry, history, expected)
                        summary["audited_recovery_boundaries"] += audited
                        failures += 1
                history = _check_page(plan, healthy, history, expected)
                requests += 1
                if healthy.state is None:
                    _require(history == expected, "healthy traversal ended early")
                    break
                _require(healthy.state != saved, "healthy traversal made no progress")
                state = _copy(healthy.state)
            else:
                raise AssertionError(f"{fixture.name}/{schedule.name}: healthy traversal did not terminate")
            summary["baseline_traversals"] += 1
            summary["baseline_requests"] += requests
            summary["read_positions"] += reads
            summary["injected_failures"] += failures
            summary["fresh_source_retries"] += failures
            summary["same_source_retries"] += failures
            summary["complete_recovery_traversals"] += failures
            summary["runs"].append(dict(fixture=fixture.name, schedule=schedule.name,
                                        requests=requests, read_positions=reads,
                                        injected_failures=failures, expected_rows=[list(row) for row in expected]))
    required = {"raw_read", "prepared_read", "prepared_refill", "refill_after_same_request_consumption",
                "refill_after_saved_tail", "after_tentative_selection", "after_owner_completion",
                "after_owner_quota", "after_owner_zero_quota", "after_owner_exhaustion",
                "after_release", "after_omit", "after_covered_owner_resolution"}
    _require(required <= witnesses.keys(), f"missing requested failure witnesses: {sorted(required - witnesses.keys())}")
    _require(summary["injected_failures"] == summary["read_positions"] * len(FAULT_MODES),
             "failure sweep skipped a read/mode combination")
    summary["witness_counts"] = dict(sorted(witnesses.items()))
    summary["witness_unit"] = "healthy read positions, each injected once per fault mode"
    summary["passed"] = True
    return summary


if __name__ == "__main__":
    print(json.dumps(run_failure_checks(), indent=2))
