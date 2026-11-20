"""Synthetic grouped-coverage fixtures; separate from the frozen item baseline.

Rows and represented members are different units here. Stage quotas and page sizes
count rows; a grouped row can represent several member IDs. Every fixture declares
its partial-group reservation policy explicitly where that distinction matters.
"""

from dataclasses import dataclass

from reference.model import Reservation

from .model import Plan, Record, Stage


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    plan: Plan


def targeted_scenarios() -> tuple[Scenario, ...]:
    """Sixteen small plans designed around specific coverage/ownership hazards."""
    cases = []

    def add(name, description, records, stages, reservations=()):
        cases.append(Scenario(name, description,
                              Plan(tuple(records), tuple(stages), tuple(reservations), scope=name)))

    members = (Record("A", 100, "G"), Record("X", 95, "H"),
               Record("B", 90, "G"), Record("C", 80, "G"))
    for policy in ("subtract", "defer"):
        add(f"middle_hole_{policy}",
            "B is reserved inside G={A,B,C}; X is an unrelated interleaved member. "
            + ("Early A represents A,C; owner B later represents B."
               if policy == "subtract" else
               "Early A and C defer their grouped rows; owner B later represents A,B,C."),
            members,
            (Stage("early", ("A", "C"), partial_policy=policy),
             Stage("owner", ("B",)),
             Stage("rest", ("A", "X", "B", "C"), mode="items")),
            (Reservation("owner", ("B",)),))

    for reserved, candidates, label in (("A", ("B", "C"), "head"),
                                        ("C", ("A", "B"), "tail")):
        add(f"reserved_{label}_subtract",
            f"Only {reserved} is reserved; a different candidate must not pull it into an earlier grouped row. "
            "The owner later receives the residual member; represented sets stay disjoint.",
            tuple(item for item in members if item.group == "G"),
            (Stage("early", candidates, partial_policy="subtract"),
             Stage("owner", (reserved,)),
             Stage("rest", ("A", "B", "C"), mode="items")),
            (Reservation("owner", (reserved,)),))

    add("resolver_reachability",
        "A is first selected as an item. The grouped scan then consumes unrelated B below A, "
        "but later C can resolve G={A,C}. Coverage for A must survive that raw cursor crossing, "
        "so the C row represents C alone.",
        (Record("A", 100, "G"), Record("B", 90, "H"), Record("C", 80, "G")),
        (Stage("first", ("A",), quota=1, mode="items"),
         Stage("grouped", ("B", "C")), Stage("empty", (), mode="items")))

    for completion in ("release", "omit"):
        add(f"defer_zero_{completion}",
            "An earlier scan passes all available G candidates while B is reserved. "
            f"The zero-quota owner resolves B by {completion}; a remaining grouped source can "
            + ("represent A,B,C together." if completion == "release" else
               "represent A,C, with an omission hole at B."),
            tuple(item for item in members if item.group == "G"),
            (Stage("early", ("A", "C"), partial_policy="defer"),
             Stage("owner", ("B",), quota=0),
             Stage("fallback", ("A", "B", "C"))),
            (Reservation("owner", ("B",), on_unserved=completion),))

    two_groups = tuple(Record(identity, 110 - index * 10, "G" if index < 3 else "H")
                       for index, identity in enumerate("ABCDEF"))
    for completion in ("release", "omit"):
        resolved = "released" if completion == "release" else "omitted"
        add(f"partial_owner_{completion}",
            "B,C in G and E,F in H are reserved. Earlier rows represent A and D. One owner "
            f"row covers both B,C and exhausts its one-row quota; E,F are then {resolved}. "
            "Quotas count rows, not the number of represented member IDs.",
            two_groups,
            (Stage("early", ("A", "D"), partial_policy="subtract"),
             Stage("owner", ("B", "C", "E", "F"), quota=1),
             Stage("fallback", tuple("ABCDEF"))),
            (Reservation("owner", tuple("BCEF"), on_unserved=completion),))

    overlapping = (Record("A", 100, "G"), Record("X", 95, "H"),
                   Record("B", 90, "G"), Record("C", 80, "G"), Record("D", 70, "G"))
    for policy, completion in (("subtract", "release"), ("defer", "release"), ("defer", "omit")):
        add(f"overlapping_{policy}_{completion}",
            "Earlier owner claims B,C; later owner claims C,D but loses its claim to C. "
            "Declarations are deliberately reversed. "
            + ("The first owner can represent its B,C while leaving D pending."
               if policy == "subtract" else
               f"The first owner consumes B and C but defers their rows because D is pending; "
               f"its own reservations must remain pending until exhaustion applies {completion}."),
            overlapping,
            (Stage("early", ("A",), partial_policy=policy),
             Stage("owner", ("B", "C"), partial_policy=policy),
             Stage("later_owner", ("C", "D"), quota=1, partial_policy=policy),
             Stage("rest", ("A", "X", "B", "C", "D"), mode="items")),
            (Reservation("later_owner", ("C", "D"), on_unserved="omit"),
             Reservation("owner", ("B", "C"), on_unserved=completion)))

    add("selective_yield_preemption",
        "Early grouping does not yield and represents A,B,C, including reserved B. The owner "
        "must resolve B as already covered without spending its one row, then represent D.",
        (Record("A", 100, "G"), Record("B", 90, "G"),
         Record("C", 80, "G"), Record("D", 70, "H")),
        (Stage("early", ("A",)), Stage("owner", ("B", "D"), quota=1),
         Stage("rest", tuple("ABCD"), mode="items")),
        (Reservation("owner", ("B",), yield_from=("rest",), on_unserved="omit"),))

    add("episode_holes",
        "G's fixed outer episode contains A,C around the distinct middle episode B. "
        "Selecting outer must not encode a single G span across unrepresented B; B belongs "
        "to its reserved owner. Episode membership is supplied metadata, not inferred here.",
        (Record("A", 100, "G", "outer"), Record("X", 95, "H"),
         Record("B", 90, "G", "middle"), Record("C", 80, "G", "outer")),
        (Stage("early", ("A", "C")), Stage("owner", ("B",)),
         Stage("rest", ("A", "X", "B", "C"), mode="items")),
        (Reservation("owner", ("B",)),))

    add("tied_positions",
        "All sort keys tie. ID order is X,C,B,A; reserved B is a hole between C and A. "
        "Coverage membership and span endpoints must compare the full position tuple.",
        (Record("A", 50, "G"), Record("B", 50, "G"),
         Record("C", 50, "G"), Record("X", 50, "H")),
        (Stage("early", ("C", "A"), partial_policy="subtract"),
         Stage("owner", ("B",)), Stage("rest", ("X",), mode="items")),
        (Reservation("owner", ("B",)),))

    add("candidate_scope_and_final_grouping",
        "Only A matches the priority source, but its row represents all scoped G members A,B,C. "
        "Interleaved H members X,Y remain eligible. The final grouped stage selects H once "
        "and still needs within-stage deduplication until its remaining members are passed.",
        (Record("A", 100, "G"), Record("X", 90, "H"), Record("B", 80, "G"),
         Record("Y", 70, "H"), Record("C", 60, "G")),
        (Stage("priority", ("A",), quota=1),
         Stage("last", ("A", "X", "B", "Y", "C"))))

    return tuple(cases)


def long_scenarios(groups: int = 200, members: int = 12) -> tuple[Scenario, ...]:
    """Four plans with many real 50-row pages and explicitly known member coverage.

    Default size is 2,400 member IDs. Partial/subtract produces 400 grouped rows;
    partial/defer and both whole-group reservation policies produce 200 rows.
    Candidate positions interleave groups so that many coverage sets coexist.
    """
    if type(groups) is not int or groups < 2 or type(members) is not int or members < 4:
        raise ValueError("Long grouped fixtures require at least two groups and four members each")
    records = tuple(
        Record(f"g{group:04d}-m{member:03d}",
               ((members - 1 - member) * groups + groups - 1 - group) // 3,
               f"G{group:04d}")
        for member in range(members) for group in range(groups)
    )
    all_ids = tuple(record.id for record in records)
    result = []
    for distribution in ("alternating_members", "whole_groups"):
        if distribution == "alternating_members":
            reserved = tuple(record.id for member in range(members) if member % 2 == 1
                             for record in records[member * groups:(member + 1) * groups])
            reserved_set = set(reserved)
            early = tuple(identity for identity in all_ids if identity not in reserved_set)
            description = (f"{groups} groups × {members} members; odd-ranked members of every group "
                           "are reserved. Earlier candidate IDs are unreserved, but every resolved "
                           "group contains reservation holes.")
        else:
            reserved = tuple(record.id for record in records if int(record.group[1:]) % 2 == 1)
            early = all_ids
            description = (f"{groups} groups × {members} members; all members of odd-numbered groups "
                           "are reserved. Whole-group coverage is contiguous within each group, "
                           "even though other groups interleave in the overall scan.")
        for policy in ("subtract", "defer"):
            name = f"long_{distribution}_{policy}"
            plan = Plan(records,
                        (Stage("early", early, partial_policy=policy),
                         Stage("owner", reserved, partial_policy=policy),
                         Stage("last", all_ids, partial_policy=policy)),
                        (Reservation("owner", reserved),), scope=name)
            result.append(Scenario(name, description + f" Partial-group policy: {policy}.", plan))
    return tuple(result)
