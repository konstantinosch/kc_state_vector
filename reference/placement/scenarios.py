"""Small counterexamples and reused public distributions for placement policies."""

from dataclasses import dataclass

from reference.model import Item, Plan, Reservation, Stage
from reference.long_scenarios import make_scenarios
from reference.grouped.generated import distribution_scenarios as grouped_distributions
from reference.grouped.scenarios import targeted_scenarios

from .adapters import Adapter


@dataclass(frozen=True)
class Scenario:
    name: str
    adapter: Adapter
    description: str


def small_scenarios():
    """Seven hand-selected plans, expanding grouped plans into three encodings."""
    items = {str(value): Item(str(value), value) for value in range(48, 53)}
    core = (
        ("priority_duplicate", Plan((
            Stage("priority", tuple(items[value] for value in ("51", "49", "48")), quota=2),
            Stage("rest", tuple(items.values()))), scope="placement-priority-duplicate"),
         "After rows 51,49,52, resuming below representative 52 repeats previously selected 51 and 49."),
        ("priority_omission", Plan((
            Stage("priority", (items["48"],), quota=1),
            Stage("rest", tuple(items.values()))), scope="placement-priority-omission"),
         "After priority 48, raw continuation below 48 loses all higher remaining items."),
        ("tied_plain", Plan((Stage("plain", tuple(Item(value, 7) for value in ("A", "B", "C", "D"))),),
                            scope="placement-ties"),
         "Full tuple order is D,C,B,A despite equal sort keys; raw cursor recovery can match a plain suffix."),
    )
    result = [Scenario(name, Adapter(plan), description) for name, plan, description in core]
    selected = {"middle_hole_subtract", "middle_hole_defer", "defer_zero_release", "defer_zero_omit"}
    for case in targeted_scenarios():
        if case.name in selected:
            for representation in ("ids", "spans", "hybrid"):
                result.append(Scenario(f"{case.name}_{representation}",
                                       Adapter(case.plan, representation), case.description))
    return tuple(result)


def distribution_scenarios(size=3000, groups=200, members=12, seed=20261119):
    """Three core distributions and eight grouped distributions in each encoding.

    The two core reservation cases retain their independently tested prepared
    sources. Grouped sources remain raw; this does not combine grouping with
    persistent prepared buffers.
    """
    selected = {"core_growth_control", "partial_owner_release", "partial_owner_omit"}
    result = [Scenario(case.name, Adapter(case.plan), case.description)
              for case in make_scenarios(size, seed) if case.name in selected]
    for case in grouped_distributions(groups, members, seed):
        for representation in ("ids", "spans", "hybrid"):
            result.append(Scenario(f"{case.name}_{representation}",
                                   Adapter(case.plan, representation), case.description))
    return tuple(result)


def core_two_growth_cycles():
    """Existing raw-stage witness: grow, retire, then repeat in a second domain."""
    first = tuple(Item(f'cycle1-{index:04d}', 2400 - index) for index in range(600))
    second = tuple(Item(f'cycle2-{index:04d}', 1200 - index) for index in range(600))
    plan = Plan((Stage('grow_first', first), Stage('sweep_first', first),
                 Stage('grow_second', second), Stage('sweep_second', second)),
                scope='two-way-two-growth-cycles-1200')
    return Scenario(
        'core_two_growth_cycles', Adapter(plan),
        'Two disjoint 600-ID domains, each repeated in two raw stages. '
        'Coverage grows while the first source is selected, retires through its duplicate '
        'sweeper, then grows again in the second domain. No artificial token padding.')
