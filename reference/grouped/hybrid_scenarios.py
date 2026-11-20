"""Admission-time byte choices and fixed-descriptor lifetime counterexamples.

These fixtures extend the local grouped experiments without changing their
earlier scenarios or the frozen item/reservation baseline.
"""

from reference.model import Reservation

from .model import Plan, Record, Stage
from .scenarios import Scenario


def hybrid_scenarios() -> tuple[Scenario, ...]:
    """Six plans distinguishing exact coverage, byte cost and retirement cost."""
    cases = []
    run = tuple(Record(f"selected-{index:02d}-" + "payload" * 3, 1000 - index, "G")
                for index in range(20))
    hole = Record("reserved-middle", 980, "G")
    isolated = Record("isolated-" + "x" * 30, 979, "G")
    records = run + (hole, isolated)
    for policy in ("subtract", "defer"):
        name = f"hybrid_mixture_{policy}"
        cases.append(Scenario(
            name,
            "One group contains a long consecutive run, a reserved hole and an isolated member. "
            + ("The early row represents 21 IDs using one span plus one bare ID; its owner later gets the hole."
               if policy == "subtract" else
               "Earlier candidates defer; the owner later represents all 22 IDs together."),
            Plan(records,
                 (Stage("early", (run[0].id, isolated.id), partial_policy=policy),
                  Stage("owner", (hole.id,), partial_policy=policy),
                  Stage("sweep", tuple(record.id for record in records), mode="items")),
                 (Reservation("owner", (hole.id,)),), scope=name)))

    group = "a-very-long-domain-group-identifier-" * 6
    records = tuple(Record(identity, 100 - index, group) for index, identity in enumerate("cba"))
    cases.append(Scenario(
        "hybrid_short_ids_long_group",
        "Three one-character IDs are cheaper than a span repeating a long group key. "
        "That byte choice occupies three entries, so an entry cap can reject hybrid while a span fits.",
        Plan(records,
             (Stage("first", ("c",), quota=1), Stage("sweep", ("c", "b", "a"), mode="items")),
             scope="hybrid_short_ids_long_group")))

    records = tuple(Record(f"μήνυμα-{index:02d}-😀", 42, "ομάδα-東") for index in range(20))
    reserved = records[10].id
    for policy in ("subtract", "defer"):
        name = f"hybrid_unicode_ties_{policy}"
        cases.append(Scenario(
            name,
            "Twenty Unicode IDs tie on the timestamp; full tuple order breaks ties. The middle "
            "ID is reserved. Span admission compares canonical escaped JSON bytes, including "
            f"the Unicode group key and endpoints. Partial-group policy: {policy}.",
            Plan(records,
                 (Stage("early", (records[-1].id, records[0].id), partial_policy=policy),
                  Stage("owner", (reserved,), partial_policy=policy),
                  Stage("sweep", tuple(record.id for record in records), mode="items")),
                 (Reservation("owner", (reserved,)),), scope=name)))

    run = tuple(Record(f"archive-message-{index:02d}-" + "q" * 20, 1000 - index, "G")
                for index in range(20))
    records = run + (Record("tail", 0, "other"),)
    cases.append(Scenario(
        "hybrid_fixed_span_lifetime",
        "The first row represents 20 IDs with a cheaper span. A later item scan gradually "
        "passes them: with one member still needed, that unchanged span costs more than its "
        "remaining explicit ID. The admitted descriptor must retain its original bounds, "
        "then retire whole after the last represented member is passed.",
        Plan(records,
             (Stage("first", (run[0].id,), quota=1),
              Stage("sweep", tuple(record.id for record in records), mode="items")),
             scope="hybrid_fixed_span_lifetime")))
    return tuple(cases)
