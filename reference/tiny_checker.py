"""Execution checks shared by the bounded exhaustive tiny-plan runner.

Profiles vary the machinery around the same semantic plan. Every request starts a
new Pager and receives only JSON-round-tripped logical continuation. The complete
result oracle and retirement auditor remain independent of the page loop.
"""

from dataclasses import dataclass, replace
import json

from .model import Pager
from .oracle import unpaged_oracle
from .retirement import audit_boundary


@dataclass(frozen=True)
class Profile:
    name: str
    buffer_sizes: tuple[int | None, ...]
    page_sizes: tuple[int, ...]
    batch_sizes: tuple[int, ...]
    candidate_budgets: tuple[int, ...]


EXECUTION_PROFILES = (
    Profile("raw_step", (None, None, None), (1,), (1,), (1,)),
    Profile("prepared_wide", (2, 2, 2), (50,), (50,), (1000,)),
    Profile("mixed_boundary", (None, 2, 1), (1, 2, 50), (3, 1, 2), (2, 1, 4)),
)


def _encode(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def validate_case(plan, profile, *, replay=False):
    """Check one complete traversal and return primary-request counters.

The supplied plan is semantic input: the profile replaces only buffer sizes.
``replay=True`` independently repeats every request and compares the whole Page;
replays do not increment the returned counters. Exhaustive callers can leave it
off and keep replay testing explicit in their separately reported scope.
"""
    if len(profile.buffer_sizes) != len(plan.stages):
        raise ValueError("A profile must specify a buffer choice for each stage.")
    for choices in (profile.page_sizes, profile.batch_sizes, profile.candidate_budgets):
        if not choices or any(type(value) is not int or value < 1 for value in choices):
            raise ValueError("Profile request limits must be nonempty positive integer sequences.")
    profiled = replace(plan, stages=tuple(
        replace(stage, buffer_size=size)
        for stage, size in zip(plan.stages, profile.buffer_sizes)
    ))
    expected = unpaged_oracle(plan)
    history, seen = [], set()
    state = None
    counters = dict(requests=0, full_pages=0, empty_pages=0,
                    selected_rows=0, audited_boundaries=0)
    bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
    for index in range(bound):
        page_size = profile.page_sizes[index % len(profile.page_sizes)]
        batch_size = profile.batch_sizes[index % len(profile.batch_sizes)]
        budget = profile.candidate_budgets[index % len(profile.candidate_budgets)]
        arguments = dict(page_size=page_size, batch_size=batch_size, candidate_budget=budget)
        before = _encode(state)
        incoming = json.loads(before)
        try:
            page = Pager(profiled).page(incoming, **arguments)
            _require(_encode(incoming) == before, "page mutated its incoming continuation")
            if replay:
                retry_input = json.loads(before)
                repeated = Pager(profiled).page(retry_input, **arguments)
                _require(_encode(retry_input) == before, "replay mutated its incoming continuation")
                _require(repeated == page, "replaying the same request changed its Page")
            _require(len(page.rows) <= page_size, "page exceeded its output limit")
            _require(len(page.rows) <= page.consumed <= budget, "candidate budget violated")
            _require(page.fetched >= 0 and page.reads >= 0, "negative source-work counter")
            ids = [identity for _, identity in page.rows]
            _require(len(ids) == len(set(ids)) and not seen.intersection(ids),
                     "duplicate ID within or across pages")
            _require(page.rows == tuple(expected[len(history):len(history) + len(page.rows)]),
                     "page differs from the ordered oracle prefix")
            history.extend(page.rows)
            seen.update(ids)
            audit_boundary(profiled, page.state, history)
            if page.state is not None:
                _require(_encode(page.state) != before, "nonterminal continuation made no progress")
                if state is not None:
                    _require(page.state["stage"] >= state["stage"], "active stage moved backwards")
                    for old, new in zip(state["cursors"], page.state["cursors"]):
                        _require(old is None or (new is not None and tuple(new) <= tuple(old)),
                                 "consumed cursor moved backwards")
            counters["requests"] += 1
            counters["full_pages"] += len(page.rows) == page_size
            counters["empty_pages"] += not page.rows
            counters["selected_rows"] += len(page.rows)
            counters["audited_boundaries"] += 1
            if page.state is None:
                _require(history == expected, "terminal traversal differs from the full oracle result")
                return counters
            state = json.loads(_encode(page.state))
        except Exception as exc:
            raise AssertionError(f"{profile.name}, request {index + 1}: {exc}") from exc
    raise AssertionError(f"{profile.name}: traversal did not terminate within {bound} requests")
