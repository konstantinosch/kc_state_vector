"""Reproducible varied plans, not an exhaustive enumeration of all group models."""

from dataclasses import replace
from random import Random

from reference.model import Reservation
from .model import Plan, Record, Stage
from .scenarios import Scenario, long_scenarios


def generated_scenarios(count=600, seed=20261119):
    """Vary source overlap, ties, episodes, quotas, owners and partial policies."""
    for case in range(count):
        rng = Random(seed + case)
        size = rng.randrange(13)
        groups = rng.randrange(1, 5)
        records = tuple(Record(f"id{i:02d}", rng.randrange(-2, 6),
                               f"g{rng.randrange(groups)}", f"e{rng.randrange(1 if case % 3 else 2)}")
                        for i in range(size))
        stages = []
        for index in range(rng.randrange(1, 5)):
            density = rng.choice((0, .2, .5, .9, 1))
            stages.append(Stage(f"s{index}", tuple(r.id for r in records if rng.random() < density),
                                quota=rng.choice((None, None, 0, 1, 2, 3)),
                                mode=rng.choice(("grouped", "grouped", "items")),
                                partial_policy=rng.choice(("subtract", "defer"))))
        declarations = []
        by_id = {r.id: r for r in records}
        for stage in stages:
            if rng.random() < .3:
                continue
            units = {(by_id[i].group, by_id[i].episode) for i in stage.candidate_ids}
            reachable = stage.candidate_ids if stage.mode == "items" else tuple(
                r.id for r in records if (r.group, r.episode) in units)
            density = rng.choice((.2, .5, 1))
            reserved = tuple(i for i in reachable if rng.random() < density)
            yielding = None if rng.random() < .5 else tuple(
                other.name for other in stages if other != stage and rng.random() < .5)
            declarations.append(Reservation(stage.name, reserved, yield_from=yielding,
                                            on_unserved=rng.choice(("release", "omit"))))
        rng.shuffle(declarations)
        name = f"seed_{seed + case}"
        yield Scenario(name, "Seeded fixed-data group/source/reservation combination.",
                       Plan(records, tuple(stages), tuple(declarations), scope=name))


def distribution_scenarios(groups=200, members=12, seed=20261119):
    """Eight long scenarios: whole, alternating, contiguous and sparse reservations."""
    originals = long_scenarios(groups, members)
    yield from originals
    base = originals[0].plan
    all_ids = tuple(record.id for record in base.records)
    rng = Random(seed)
    for label in ("contiguous_members", "sparse_30_percent"):
        reserved = tuple(record.id for record in base.records
                         if (int(record.id.rsplit("m", 1)[1]) < members // 2
                             if label == "contiguous_members" else rng.random() < .3))
        reserved_set = set(reserved)
        for policy in ("subtract", "defer"):
            name = f"long_{label}_{policy}"
            plan = replace(base, stages=(
                Stage("early", tuple(i for i in all_ids if i not in reserved_set), partial_policy=policy),
                Stage("owner", reserved, partial_policy=policy),
                Stage("last", all_ids, partial_policy=policy),
            ), reservations=(Reservation("owner", reserved),), scope=name)
            yield Scenario(name, f"{groups} groups × {members} members; {label}; {policy} policy.", plan)
